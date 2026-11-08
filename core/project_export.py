"""Export a project as a zip file for offline/agent analysis.

This module provides functionality to bundle a project's contents (metadata,
items, and all uploaded files) into a single zip archive. The exported zip
contains:
- manifest.json: Project metadata and item descriptions
- files/: Directory containing all uploaded files for items in the project

#556 (owner decision 2026-10-18, "Export a card from its page, in a choice of formats: zip or HTML,
and later pick an export template once templates exist"): the card page's "Export this card…" dialog
offers the formats in `FORMATS` (the picker is built from it, so a new format or template is one more
entry here plus its route). `zip` is `export_project` below; `html` is `export_card_site`, a small
static site of just this card (core/site_export.build_site into a temp folder), zipped, with a
README.txt saying what was left out and why. Nothing is ever left out silently: `left_out()` counts
the files that didn't go in, by reason, and both formats carry it (manifest.json's `left_out`, the
README).
"""

import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from core import card_rules, db, policy, roles, storage, timeline
from core.errors import AppError, NotFound


# --- #556: the formats the card page's export picker offers -----------------------------------------
# key -> what the picker shows and the door it calls ({id} = the card's id). `role` is the door's own
# role label (web/routes/cards.py says the same), so the picker can grey a format the user can't use
# (#779) instead of hiding it. `templates` (html only): the export templates the format can render
# with; one today. A new template = one more entry here and a folder under web/export_templates/.
FORMATS = (
    {"key": "zip", "label": "Zip: the files and a manifest",
     "description": "Every file on the card plus manifest.json (titles, descriptions, tags, dates), "
                    "for offline review or for an agent to read.",
     "role": roles.VIEWER, "url": "/api/projects/{id}/export.zip", "templates": ()},
    {"key": "html", "label": "HTML: a small website of this card",
     "description": "The card's page as the static site renders it (write-up, files, thumbnails), "
                    "zipped; open index.html in any browser.",
     "role": roles.EDITOR, "url": "/api/projects/{id}/export.html.zip",
     "templates": ({"key": "default", "label": "Default (the site's own look)"},)},
)
HTML_TEMPLATES = {t["key"] for f in FORMATS if f["key"] == "html" for t in f["templates"]}


def formats_for(card):
    """The picker's formats for one card: FORMATS with the url filled in."""
    return [{**f, "url": f["url"].format(id=card["id"]), "templates": list(f["templates"])} for f in FORMATS]


def left_out(card_id, *, outsider=False):
    """#556: the card's files that an export leaves out, by reason, never silently. -> (included, notes)
    where notes = [{reason, count, names}] ("names" only for files the asker can see). `outsider`:
    also leave out what an outsider can't see (private cards / hobbies), as the static site does."""
    rows = db.list_project_items(card_id)
    visible = policy.filter_visible(rows)
    hidden = len(rows) - len(visible)
    exportable = policy.filter_exportable(visible)
    exportable_slugs = {i["slug"] for i in exportable}
    sensitive = [i for i in visible if i["slug"] not in exportable_slugs]
    older = db.superseded_slugs()
    superseded = [i for i in exportable if i["slug"] in older]
    included = [i for i in exportable if i["slug"] not in older]
    private = []
    if outsider:
        acc = policy.outsider_access()
        hidden_slugs = acc.hidden_slugs() if not acc.empty else frozenset()
        private = [i for i in included if i["slug"] in hidden_slugs]
        included = [i for i in included if i["slug"] not in hidden_slugs]
    from core import item_title  # lazy, like the other helpers here
    notes = []
    if hidden:
        notes.append({"reason": "not visible to you", "count": hidden, "names": []})
    for reason, group in (("sensitive or a restricted type: never leaves the install", sensitive),
                          ("an older revision: only the current one goes in", superseded),
                          ("under a private card or hobby: not published", private)):
        if group:
            notes.append({"reason": reason, "count": len(group), "names": [item_title.title_of(i) for i in group]})
    return included, notes


def left_out_lines(notes):
    """The notes as plain sentences (README.txt, the dialog)."""
    out = []
    for n in notes:
        names = f": {', '.join(n['names'][:20])}" + (f" and {len(n['names']) - 20} more" if len(n["names"]) > 20 else "") \
            if n["names"] else ""
        out.append(f"{n['count']} file(s) left out ({n['reason']}){names}.")
    return out


def export_project(project_id_or_slug):
    """Export a project as a zip file containing manifest and all files.

    Args:
        project_id_or_slug: Numeric project id or slug string

    Returns:
        bytes: The zip file contents as bytes (suitable for streaming)

    Raises:
        ValueError: If the project is not found
    """
    project = db.get_project(project_id_or_slug)
    if project is None or not policy.can_view_card(project):  # #604 step 3: a hidden card = missing
        raise ValueError(f"Project not found: {project_id_or_slug}")

    # Fetch all items in this project
    # #443: never hand restricted items (private keys/certs) to whatever
    # reads this zip (it's meant for offline/agent analysis).
    # #477: current revisions only; superseded older revisions stay out of the zip.
    # #557: what may leave the install is core/policy.py's call (filter_exportable). #556: what didn't
    # go in is counted by reason in the manifest's `left_out`, never dropped silently.
    items, notes = left_out(project["id"])

    # Fetch writeup body if writeup_slug is set
    writeup_body = None
    if project.get("writeup_slug"):
        writeup_doc = db.get_by_slug(project["writeup_slug"])
        if writeup_doc:
            writeup_body = writeup_doc.get("type_metadata", {}).get("body", "")

    # Build the manifest
    manifest = {
        "project": {
            "id": project["id"],
            "slug": project["slug"],
            "title": project["title"],
            "description": project.get("description"),
            # #545: the live status (the frozen v1 `status` word is retired)
            "kind": project.get("kind") or "project",
            "stage": project.get("stage"),
            "stage_label": card_rules.stage_label(project.get("stage"), project.get("kind")),
            "stop_reason": project.get("stop_reason"),
            "writeup_body": writeup_body,
        },
        "items": [],
        "left_out": notes,
    }

    # Collect item data for manifest and prepare files to include
    files_to_include = []  # List of (arcname, file_path) tuples

    for item in items:
        item_data = {
            "slug": item["slug"],
            "media_type": item["media_type"],
            "description": item.get("description"),
            "content_description": item.get("content_description"),
            "tags": [tag["name"] for tag in db.list_tags_for_post(item["slug"])],
            "type_metadata": item.get("type_metadata"),
            "timestamp": item.get("timestamp"),  # the upload time ("Imported"), never the item's date (#725)
            "content_date": item.get("content_date"),
            "effective_date": timeline.resolve_item_date(item),  # #725: the REAL date, or null = undated
            "source_modified_at": item.get("source_modified_at"),
            "external_url": item.get("external_url"),
        }

        # Include filename for uploaded files
        if item.get("stored_filename"):
            item_data["filename"] = item["filename"]
            item_data["stored_filename"] = item["stored_filename"]

            # Add file to the list to include in zip
            file_path = storage.path_for(item["stored_filename"])
            if file_path.exists():
                # Archive name: files/<slug>_<original_filename>
                arcname = f"files/{item['slug']}_{item['filename']}"
                files_to_include.append((arcname, file_path))

        manifest["items"].append(item_data)

    # Create zip in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add manifest
        manifest_json = json.dumps(manifest, indent=2)
        zf.writestr("manifest.json", manifest_json)

        # Add files
        for arcname, file_path in files_to_include:
            zf.write(file_path, arcname=arcname)

    zip_buffer.seek(0)
    return zip_buffer.getvalue()


# --- #556: the HTML format: a small static site of one card, zipped --------------------------------

def export_card_site(project_id_or_slug, template="default"):
    """Builds the static site (core/site_export.build_site) for just this card into a fresh temp
    folder and zips it, with a README.txt saying what is in and what was left out and why.
    -> {"path": the zip on disk, "folder": the temp folder to remove afterwards, "filename",
    "report": build_site's report plus left_out}. The caller removes `folder` (the web route does it
    once the download is sent). Refusals (nothing built): not_found (missing or hidden card),
    bad_template, card_private (a private card never goes into a static site, which is meant to be
    shared; the zip format includes it for the people who may see it)."""
    from core import site_export  # lazy: site_export pulls in jinja and the export templates
    project = db.get_project(project_id_or_slug)
    if project is None or not policy.can_view_card(project):
        raise NotFound(f"Project not found: {project_id_or_slug}")
    template = (template or "default").strip().lower()
    if template not in HTML_TEMPLATES:
        raise AppError("bad_template", f"Unknown export template {template!r}: use "
                       f"{', '.join(sorted(HTML_TEMPLATES))}.", status=400)
    if policy.outsider_access().card_hidden(project["id"]):
        raise AppError("card_private", f"{project['title']!r} is private (or inside a private card or "
                       f"hobby), so it can't go into an HTML site, which is meant to be shared. Nothing was "
                       f"built. The zip export includes it, for you.", status=409)
    included, notes = left_out(project["id"], outsider=True)
    folder = Path(tempfile.mkdtemp(prefix="constructicon-card-html-"))
    try:
        site = folder / "site"
        report = site_export.build_site({"project_slugs": [project["slug"]], "blog_entry_slugs": [],
                                         "site": {"title": project["title"], "tagline": ""}}, out_dir=site)
        lines = [f"{project['title']}: an HTML export from Constructicon.",
                 "", "Open index.html in a browser; the card's own page is projects/"
                 f"{project['slug']}.html. Everything links relatively, so the folder works anywhere.",
                 "", f"{len(included)} file(s) went in."]
        lines += left_out_lines(notes) or ["Nothing on the card was left out."]
        if report.get("warnings"):
            lines += ["", "Warnings from the build:"] + [f"- {w}" for w in report["warnings"]]
        filename = f"{project['slug']}-site.zip"
        zip_path = folder / filename
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", "\n".join(lines) + "\n")
            for p in sorted(site.rglob("*")):
                if p.is_file():
                    zf.write(p, arcname=str(p.relative_to(site)).replace("\\", "/"))
        shutil.rmtree(site)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"path": zip_path, "folder": folder, "filename": filename,
            "report": {**report, "left_out": notes, "included": len(included)}}
