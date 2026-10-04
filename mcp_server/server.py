"""constructicon-mcp — MCP server for the Constructicon media gallery.

Uses the mcp package's v2 MCPServer API
(FastMCP was renamed/restructured in mcp 2.x — see the SDK migration guide).
Streamable-HTTP transport, host/port/stateless_http passed to run().

Exposes MCP tools for uploading, managing, tagging, and organizing media
in a Constructicon instance. Runs as a sidecar alongside constructicon-web.
"""

import base64
import io
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer

from core import backup, card_rules, cards, curation_queue, curator_needs, db, decisions, ingest, object_types, ocr, physical_piece, provenance_options, revisions, storage, timeline
from core import version as version_info

BASE_URL = os.environ.get("CONSTRUCTICON_BASE_URL", "http://constructicon-web:8000")

# #433 part 3: a read-only bind-mounted inbox (host: Media/constructicon/import,
# reachable over SMB) so large files are ingested by server-side path instead of
# base64 through one MCP message; never written to or deleted from by the app.
IMPORT_DIR = Path(os.getenv("CONSTRUCTICON_IMPORT_DIR", "/app/import"))

# #433: base64 inflates ~33% and the whole payload rides one MCP message;
# a 500 MB file would be ~670 MB of JSON
DOWNLOAD_INLINE_MAX_BYTES = 25 * 1024 * 1024

mcp = MCPServer(name="constructicon-mcp", version=version_info.get_version())  # #508: version in server info (read at start; restart picks up a deploy)


def _resolve_import_path(rel):
    """Resolve a relative path within IMPORT_DIR, rejecting absolute paths
    and escapes (../ or symlinks outside IMPORT_DIR). Returns Path | None."""
    # Reject absolute paths and path traversal attempts
    if Path(rel).is_absolute():
        return None
    candidate = (IMPORT_DIR / rel).resolve()
    # Ensure the resolved path stays within IMPORT_DIR bounds
    try:
        candidate.relative_to(IMPORT_DIR.resolve())
    except ValueError:
        # candidate is outside IMPORT_DIR
        return None
    # Verify it's a file and exists
    if not candidate.is_file():
        return None
    return candidate


def _run_in_thread(fn, *args):
    """Run a function in a background daemon thread."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _ingest(filename, fileobj, file_size, description, tags, uploaded_by, source_modified_at) -> dict:
    """Private helper for file ingestion: delegates to core/ingest.py.

    All post-validation logic is now centralized in the ingest module — this tool
    simply wraps its result for the MCP response format.

    Returns {"slug": ..., ..._to_public fields..., "duplicate": False} on success,
    or {"error": "..."} on failure. May include "pending_decision_id" if the type's
    pre_store_fn deferred to the owner (#448).
    """
    result = ingest.ingest_file(
        fileobj,
        filename,
        size=file_size,
        source=uploaded_by,
        run_background=_run_in_thread,
        description=description,
        tags=tags,
        source_modified_at=source_modified_at,
    )

    if result.error:
        return {"error": result.error}
    elif result.duplicate:
        response = {**_to_public(result.row), "duplicate": True}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response
    else:
        response = {**_to_public(result.row), "duplicate": False}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response


def _to_public(row):
    spec = object_types.get_object_type(row.get("media_type"))
    return {
        "slug": row["slug"],
        "url": f"{BASE_URL}/f/{row['slug']}",
        "filename": row["filename"],
        # display_name/icon (#11) — per-object overrides, falling back to
        # the same filename/content_description/slug and spec.badge_icon
        # chain the web app uses (see web/app.py's _to_public/_to_object_detail).
        "display_name": row.get("display_name") or row["filename"] or row.get("content_description") or row["slug"],
        "icon": row.get("icon") or spec.badge_icon,
        "media_type": row.get("media_type") or "image",
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        # #261: per-type metadata bag (auto_caption/auto_caption_status,
        # YouTube view/like/comment counts, ID3 tags, ...) -- previously
        # invisible through every MCP tool. Same "always a dict, never
        # None/missing" contract as web/app.py's _to_object_detail;
        # core/db.py's _row_to_dict already parses the JSON column, so
        # this is only a guard against a stored JSON null.
        "type_metadata": row.get("type_metadata") or {},
        "artifact_link": f"{BASE_URL}{row['artifact_link']}" if row["artifact_link"] else None,
        "timestamp": row["timestamp"],
        "content_date": row.get("content_date"),
        "display_date_override": row.get("display_date_override"),
        "effective_date": timeline.resolve_item_date(row),
        # Curator (#341): provenance classification + highlight flag, so every
        # MCP object read (get_project items, search, list) carries them.
        "provenance": row.get("provenance"),
        "highlight": bool(row.get("highlight")),
        # Brand assets (#350): is_brand_asset flag + optional brand_role label,
        # so every MCP object read carries brand kit metadata.
        "is_brand_asset": bool(row.get("is_brand_asset")),
        "brand_role": row.get("brand_role"),
        # #448: per-type actions available on this object (e.g. YouTube's "fetch real date" moves here in PR 2).
        # The type file owns the handler; actions are declared in ObjectTypeSpec.actions.
        "actions": [a.key for a in spec.actions],
    }


def _to_public_project(project):
    items = db.list_project_items(project["id"])
    effective_start, effective_end = timeline.resolve_project_span(project, items)
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        # Legacy v1 status: frozen (the static export still reads it). The live
        # status is kind / activity / stage / stop_reason below.
        "status": project["status"],
        **cards.status_fields(project),
        **cards.whereabouts_fields(project),
        "cover_slug": project.get("cover_slug"),
        "writeup_slug": project.get("writeup_slug"),
        "parent_id": project.get("parent_id"),
        "created_at": project["created_at"],
        "start_date_override": project.get("start_date_override"),
        "end_date_override": project.get("end_date_override"),
        "effective_start": effective_start,
        "effective_end": effective_end,
    }


def _to_public_blog_entry(entry):
    """Full detail shape for a blog entry, including hydrated projects and items."""
    projects = db.list_entry_projects(entry["id"])
    items = db.list_entry_items(entry["id"])
    return {
        "id": entry["id"],
        "slug": entry["slug"],
        "title": entry["title"],
        "subtitle": entry.get("subtitle", ""),
        "body": entry.get("body", ""),
        "status": entry["status"],
        "cover_slug": entry.get("cover_slug"),
        "content_date": entry.get("content_date"),
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
        # Carry the per-attachment note + sort_order (the reshaping helpers
        # drop them, but they're the point of the attachment).
        "projects": [{**_to_public_project(p), "note": p.get("note", ""), "sort_order": p.get("sort_order")} for p in projects],
        "items": [{**_to_public(i), "note": i.get("note", ""), "sort_order": i.get("sort_order")} for i in items],
    }


@mcp.tool()
def constructicon_version() -> dict:
    """Report which Constructicon build is running (#508): {version, commit,
    deployed_at, env}. Version is CalVer (YYYY.M.D, .N for repeats the same day),
    suffixed -dev on the test instance; "dev" means no deploy.sh version file."""
    return version_info.get_version_info()


@mcp.tool()
def constructicon_upload(filename: str, content_base64: str, description: str = "", tags: list[str] | None = None,
                      uploaded_by: str = db.SOURCE_AUTHORED,
                      source_modified_at: float | None = None) -> dict:
    """Upload an image, document, or other file to Constructicon.

    Returns a JSON object with the new object's metadata, including a stable hotlink URL.

    filename: original filename; extension (.png/.jpg/.pdf/.stl/.psd/.svg/.eps/.mp3/.wav, etc.)
      determines the media type.
    content_base64: raw file bytes, base64-encoded.
    tags: optional list of tag names to attach to the object.
    description: optional metadata description for the object.
    uploaded_by: the Source string to record (capture_events.tech). Defaults to
      "Claude — authored" (this tool call created the content). For content migrated from
      an external source, pass db.source_migrated_from("<source>") instead.
    source_modified_at: the source file's own last-modified time (unix seconds), if known.
      Used for duplicate detection; omit to skip checking for re-uploads of the same file.

    If filename, file size, and source_modified_at all match an existing upload, returns
    the existing object instead with "duplicate": true.

    OCR (for OCR-capable types) runs in the background, same as the web app's
    /api/upload: the returned object has ocr_status "pending" -- call
    constructicon_get on the slug later to read extracted_text once it's "done".

    An audio file's own tags (#255) are read synchronously, same as /api/upload:
    the tag title becomes the object's display_name/content_description and
    artist/album/track/year/genre land in type_metadata, so the returned object
    already reflects them. A tagless file simply gets none of that.
    """
    content = base64.b64decode(content_base64)
    return _ingest(filename, io.BytesIO(content), len(content), description, tags, uploaded_by, source_modified_at)


@mcp.tool()
def constructicon_search(query: str | None = None, tags: list[str] | None = None) -> list[dict]:
    """Search objects by description, filename, or tags.

    Returns a JSON list of matching objects. If both query and tags are
    provided, filters by both (AND logic). Redacted objects are excluded
    (#282) -- use constructicon_list_redacted to see those.
    """
    # #477: old revisions are still found, marked with `superseded_by` (the current revision's slug) and `rev`.
    return revisions.decorate([_to_public(r) for r in db.search(query=query, tags=tags, client=None)])


@mcp.tool()
def constructicon_get(slug: str) -> dict | None:
    """Get one object's metadata and hotlink URL by its slug.

    Returns None if the object is not found. `superseded_by` (the current revision's slug, or null)
    and `rev` (position in its revision chain, or null) say where it sits in a chain (#477).
    """
    row = db.get_by_slug(slug)
    return revisions.decorate([_to_public(row)])[0] if row else None


@mcp.tool()
def constructicon_download(slug: str) -> dict | None:
    """Download a file's actual bytes from Constructicon.

    Takes a slug and returns the file's content base64-encoded, along with
    filename and media_type for round-trip upload/download cycles.

    For files larger than the inline limit (~25MB), returns metadata + a stable
    hotlink URL instead of base64 — fetch it from the URL to avoid inflating
    the MCP message payload beyond practical limits (#433).

    Returns None if the object is not found, or an error dict if the object
    has no uploaded file (e.g., a YouTube link or other content-only object).
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if not row.get("stored_filename"):
        return {"error": "This object has no uploaded file to download"}
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        return {"error": "File missing on disk"}
    size = path.stat().st_size
    if size > DOWNLOAD_INLINE_MAX_BYTES:
        # Return URL instead of base64 for large files
        return {
            "slug": row["slug"],
            "filename": row["filename"],
            "media_type": row.get("media_type") or "image",
            "size_bytes": size,
            "url": _to_public(row)["url"],
            "content_base64": None,
            "note": f"File is {size // (1024*1024)} MB, over the {DOWNLOAD_INLINE_MAX_BYTES // (1024*1024)} MB inline limit; fetch it from url instead.",
        }
    content = path.read_bytes()
    return {
        "slug": row["slug"],
        "filename": row["filename"],
        "media_type": row.get("media_type") or "image",
        "content_base64": base64.b64encode(content).decode("utf-8"),
    }


@mcp.tool()
def constructicon_import(path: str, description: str = "", tags: list[str] | None = None,
                        uploaded_by: str = db.SOURCE_AUTHORED,
                        source_modified_at: float | None = None) -> dict:
    """Import a file already sitting in the server-side import inbox.

    Use this for large files (up to the server's upload limit, e.g. hundreds of MB)
    that are impractical to send base64 via constructicon_upload. Path is relative
    to the inbox (e.g. "drawings/set-A.pdf"); anything resolving outside the inbox
    is refused. The file is COPIED into storage and left in place in the inbox.

    The stored filename is the file's basename. source_modified_at defaults to the
    file's own mtime, so re-importing the same unchanged file returns the existing
    object with duplicate: true. Same OCR/metadata behavior and return shape as
    constructicon_upload.
    """
    if not IMPORT_DIR.is_dir():
        return {"error": f"Import inbox {IMPORT_DIR} is not mounted on this server"}
    p = _resolve_import_path(path)
    if p is None:
        return {"error": f"Not a file inside the import inbox: {path}"}
    st = p.stat()
    with p.open("rb") as f:
        return _ingest(p.name, f, st.st_size, description, tags, uploaded_by,
                      source_modified_at if source_modified_at is not None else st.st_mtime)


@mcp.tool()
def constructicon_list_import() -> dict:
    """List files waiting in the import inbox (recursive).

    Returns {"inbox": str(IMPORT_DIR), "max_upload_mb": storage.MAX_MB,
    "files": [{"path": <posix path relative to inbox>, "size_bytes": n,
    "modified_at": mtime, "supported": bool(detect_media_type)}]} sorted by path,
    skipping hidden files/dirs (name starting with ".") and anything that resolves
    outside the inbox. If the inbox isn't mounted return {"error": ...}.

    Caps the listing at 1000 files and adds "truncated": True if more.
    """
    if not IMPORT_DIR.is_dir():
        return {"error": f"Import inbox {IMPORT_DIR} is not mounted on this server"}

    files = []
    try:
        for p in sorted(IMPORT_DIR.rglob("*")):
            rel = p.relative_to(IMPORT_DIR)
            if any(part.startswith(".") for part in rel.parts):
                continue
            # Same check constructicon_import applies: only list what it would
            # accept (regular files, symlinks resolving inside the inbox).
            if _resolve_import_path(rel) is None:
                continue
            st = p.stat()
            files.append({
                "path": rel.as_posix(),
                "size_bytes": st.st_size,
                "modified_at": st.st_mtime,
                "supported": object_types.detect_media_type(p.name) is not None,
            })
            if len(files) >= 1000:
                break
    except Exception as e:
        return {"error": f"Error listing import inbox: {e}"}

    truncated = len(files) >= 1000
    result = {
        "inbox": str(IMPORT_DIR),
        "max_upload_mb": storage.MAX_MB,
        "files": files,
    }
    if truncated:
        result["truncated"] = True
    return result


@mcp.tool()
def constructicon_update(slug: str, description: str | None = None, tags: list[str] | None = None,
                   display_name: str | None = None, icon: str | None = None,
                   type_metadata: dict | None = None, display_date: float | None = None,
                   reset_display_date: bool = False) -> dict | None:
    """Update an object's metadata: description, tags, display name, icon, type-specific fields,
    and/or its timeline display date.

    Pass None for any field you don't want to change. type_metadata is replaced wholesale, not
    merged — read the object's current type_metadata first if you only want to change one key.
    (The web app's POST /api/image/{slug} merges instead, via db.update_content_metadata.)
    content_description (e.g. a YouTube video's title) isn't exposed through this tool yet —
    db.update_content_metadata / POST /api/image/{slug} can change it, this tool just doesn't
    take that parameter.

    display_date sets a manual override for this object's position on the Constructicon
    timeline (unix timestamp, e.g. what time.time() or a datetime's .timestamp() returns).
    reset_display_date=True clears the override, reverting to the computed default
    (content_date, falling back to the upload timestamp) — it wins over display_date if both
    are passed.

    Returns the updated object, or None if not found.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if description is not None or tags is not None:
        row = db.update_tags(slug, description=description, tags=tags, client=None)
    if display_name is not None or icon is not None:
        row = db.rename_object(slug, display_name=display_name, icon=icon)
    if type_metadata is not None:
        # #563: merge (top-level keys), same as the web route; replacing wholesale wiped
        # captions / YouTube stats / ID3 / rotation. #425: clean the physical-piece keys.
        cleaned = physical_piece.clean_fields(dict(type_metadata))
        db.update_content_metadata(slug, type_metadata=cleaned)
        row = db.get_by_slug(slug)
    if reset_display_date:
        db.set_display_date_override(slug, None)
        row = db.get_by_slug(slug)
    elif display_date is not None:
        db.set_display_date_override(slug, display_date)
        row = db.get_by_slug(slug)
    return _to_public(row) if row else None


@mcp.tool()
def constructicon_redact(slug: str) -> dict | None:
    """Delete a file while keeping its metadata (for sensitive content cleanup).

    Irreversible — the file itself cannot be recovered. Metadata (description,
    tags, etc.) is preserved.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if not row.get("stored_filename"):
        raise ValueError("This object has no uploaded file to redact")
    storage.delete_files(slug, row["stored_filename"])
    return _to_public(db.mark_redacted(slug))


@mcp.tool()
def constructicon_unredact(slug: str) -> dict | None:
    """Reverse of constructicon_redact (#282): clear the redacted flag so the
    object shows up in searches, project listings and tag walks again.

    Does NOT bring the file back -- redaction deleted it permanently. The
    object stays a file-less metadata record; it's just findable again.
    Returns the updated object, or None if the slug doesn't exist; raises if
    the object isn't redacted.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if not row["redacted"]:
        raise ValueError("This object isn't redacted")
    return _to_public(db.unmark_redacted(slug))


@mcp.tool()
def constructicon_list_redacted() -> list[dict]:
    """List every redacted object (#282).

    Redacted objects are hidden from constructicon_search,
    constructicon_get_project and constructicon_get_posts_for_tag; this is
    the only listing that includes them. constructicon_get still works for
    one by slug.
    """
    return [_to_public(r) for r in db.list_redacted()]


@mcp.tool()
def constructicon_list_restricted() -> list[dict]:
    """List every restricted object: private keys, certificates, CSRs (#443).

    Restricted objects are the owner's private reference. They're kept out
    of constructicon_search and tag walks and never exported to the public
    site, but DO appear in constructicon_get_project for projects they're
    attached to (e.g. a repo's deploy key). This is the full list. Each
    entry adds "projects" (titles and slugs it's attached to).
    """
    return [
        {**_to_public(r), "projects": [{"title": p["title"], "slug": p["slug"]} for p in db.list_projects_for_post(r["slug"])]}
        for r in db.list_restricted()
    ]


@mcp.tool()
def constructicon_delete(slug: str) -> bool:
    """Fully delete an object — file and all metadata. Irreversible."""
    row = db.get_by_slug(slug)
    if row is None:
        return False
    if row.get("stored_filename"):
        storage.delete_files(slug, row["stored_filename"])
    db.delete_upload(slug)
    return True


@mcp.tool()
def constructicon_delete_multiple(slugs: list[str]) -> dict:
    """Delete multiple objects by slug without touching tags or projects.

    Returns {"deleted": count}. Unknown slugs are silently skipped.
    Irreversible.
    """
    deleted = 0
    for slug in slugs:
        row = db.get_by_slug(slug)
        if row is None:
            continue
        if row.get("stored_filename"):
            storage.delete_files(slug, row["stored_filename"])
        db.delete_upload(slug)
        deleted += 1
    return {"deleted": deleted}


@mcp.tool()
def constructicon_delete_all() -> dict:
    """Wipe every object, tag, and project — a full reset. Irreversible.

    Call constructicon_backup first if you want to preserve the current content.
    """
    # include_redacted (#282) / include_brand (#417): search() hides redacted
    # rows and brand assets by default; a full reset has to take them too or
    # they'd survive as orphaned rows + storage files.
    rows = db.search(limit=100000, include_redacted=True, include_brand=True)
    for row in rows:
        if row.get("stored_filename"):
            storage.delete_files(row["slug"], row["stored_filename"])
        db.delete_upload(row["slug"])
    conn = db.get_conn()
    conn.execute("DELETE FROM post_tags")
    conn.execute("DELETE FROM project_items")
    for _t in ("project_relations", "family_members", "project_hobbies", "blog_entry_projects"):
        conn.execute(f"DELETE FROM {_t}")
    conn.execute("DELETE FROM projects")
    conn.execute("DELETE FROM blog_tags")
    conn.commit()
    conn.close()
    return {"deleted": len(rows)}


@mcp.tool()
def constructicon_backup() -> dict:
    """Create a timestamped backup of the entire database and storage.

    Returns {"filename": ..., "path": ..., "size": ..., "created_at": ...}
    (see core.backup.create_backup).
    """
    return backup.create_backup()


@mcp.tool()
def constructicon_add_content(media_type: str, external_url: str | None = None, content_description: str | None = None,
                           description: str = "", tags: list[str] | None = None,
                           uploaded_by: str = db.SOURCE_AUTHORED) -> dict:
    """Create an object with no uploaded file (e.g., a YouTube link or external document).

    Use constructicon_upload instead for file-backed content.

    For OCR-capable types, OCR runs in the background (#225) -- the returned
    object has ocr_status "pending"; read it back with constructicon_get later.

    Returns a JSON object with the new object's metadata, or {"error": "..."} on failure.
    Unknown media_type values are rejected (issue #448).
    """
    result = ingest.ingest_content(
        source=uploaded_by,
        run_background=_run_in_thread,
        media_type=media_type,
        external_url=external_url,
        content_description=content_description,
        description=description,
        tags=tags,
    )

    if result.error:
        error_msg = result.error
        if error_msg.endswith("require a file upload"):
            error_msg += " — use constructicon_upload"
        return {"error": error_msg}
    else:
        return _to_public(result.row)


@mcp.tool()
def constructicon_add_related(slug: str, related_slug: str) -> list[dict]:
    """Link two objects as related (bidirectional).

    Also merges tags and project membership between the two.
    Returns the updated list of related objects.
    """
    if db.get_by_slug(slug) is None or db.get_by_slug(related_slug) is None:
        raise ValueError("one or both slugs not found")
    db.add_relation(slug, related_slug)
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_remove_related(slug: str, related_slug: str) -> list[dict]:
    """Remove a related-object link.

    Returns the updated list of related objects.
    """
    db.remove_relation(slug, related_slug)
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_get_related(slug: str) -> list[dict]:
    """Get all objects related to this one.

    Returns a list of related objects, both manually linked and auto-detected.
    """
    if db.get_by_slug(slug) is None:
        raise ValueError("slug not found")
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_list_projects(kind: str | None = None, activity: str | None = None,
                                stage: str | None = None) -> list[dict]:
    """List all projects (cards), most-recently-updated first.

    Optional filters on the live V2 status: kind (project|thing|action|family|
    collection|event), activity (active|inactive), stage (in_progress|in_use|idea|
    paused|done|stopped). Each card carries kind, activity, stage, stop_reason,
    their labels, and needs_input (true while a question about it is still open).
    """
    return [_to_public_project(p) for p in db.list_projects(kind=kind, activity=activity, stage=stage)]


def _card_error_result(e):
    """The shared error shape (spec section 5): same code as the HTTP routes."""
    return {"ok": False, "error": e.to_dict()}


@mcp.tool()
def constructicon_create_project(title: str, description: str = "", cover_slug: str | None = None, parent_id: int | None = None,
                                 kind: str | None = None, stage: str | None = None, stop_reason: str | None = None) -> dict:
    """Create a new project (card).

    Also creates a root-level tag with the same name and links it, so tagged
    objects surface through both project and tag browsing.

    parent_id optionally makes this card "part of" a parent card (#133). Nesting
    rules apply (a family or collection can't be a parent or be nested; the parent
    must exist): a violation returns {"ok": false, "error": {"code": "nest_group_kind"
    | "not_found", ...}}.

    kind: project (default) | thing | action | family | collection | event.
    stage: in_progress (default) | in_use | idea | paused | done | stopped
    (stopped needs stop_reason failed|abandoned). Invalid combinations return
    {"ok": false, "error": {"code": "bad_status", ...}}.
    """
    title = title.strip()
    if not title:
        raise ValueError("Project name can't be empty")
    try:
        card_rules.validate_status(card_rules.validate_kind(kind or card_rules.DEFAULT_KIND),
                                   stage or card_rules.DEFAULT_STAGE, stop_reason)
        if parent_id is not None:
            # Nest rules (3.7) guard creation too; checked before the tag is minted.
            parent_row = db.get_project(parent_id)
            if parent_row is None:
                raise card_rules.CardError("not_found", f"No such parent card: {parent_id!r}")
            card_rules.validate_nest({"id": None, "kind": kind or card_rules.DEFAULT_KIND, "title": title,
                                      "parent_id": None}, parent_row, ())
        tag = db.get_or_create_tag(title, parent_id=None)
        project = db.create_project(title, description=description, cover_slug=cover_slug, tag_id=tag["id"],
                                    parent_id=parent_id, kind=kind, stage=stage, stop_reason=stop_reason, actor="mcp")
    except card_rules.CardError as e:
        return _card_error_result(e)
    return _to_public_project(project)


@mcp.tool()
def constructicon_update_project(project_id: str | int, title: str | None = None,
                                 description: str | None = None, cover_slug: str | None = None,
                                 status: str | None = None, start_date: float | None = None,
                                 reset_start_date: bool = False, end_date: float | None = None,
                                 reset_end_date: bool = False, kind: str | None = None,
                                 stage: str | None = None, stop_reason: str | None = None) -> dict | None:
    """Update a project's metadata, including its timeline span.

    kind / stage / stop_reason set the live V2 status through the same rules as
    constructicon_set_kind / constructicon_set_status (a violation returns
    {"ok": false, "error": {code, message}}). `status` is the DEPRECATED v1 word
    (wip, complete, shelved, ...): it is translated to a stage (a warning says so).

    start_date/end_date set manual overrides for this project's position on the Constructicon
    timeline (unix timestamps). reset_start_date/reset_end_date each clear that one override,
    reverting it to the computed default (earliest/latest item date, falling back to the
    project's created_at when it has no items) — a reset flag wins over its corresponding
    date param if both are passed.

    Returns the updated project, or None if not found.
    """
    card_warnings = []
    if db.get_project(project_id) is None:
        return None
    try:
        if status:
            legacy = card_rules.legacy_to_status(status)
            kind = kind or legacy["kind"]
            if not stage:
                stage, stop_reason = legacy["stage"], legacy["stop_reason"]
            card_warnings.extend(legacy["warnings"])
        if kind:
            card_warnings.extend(cards.set_kind(project_id, kind, actor="mcp").warnings)
        if stage or stop_reason:
            card_warnings.extend(cards.set_status(project_id, stage, stop_reason, actor="mcp").warnings)
    except card_rules.CardError as e:
        return _card_error_result(e)
    project = db.update_project(project_id, title=title, description=description,
                                cover_slug=cover_slug)
    if project is None:
        return None
    if reset_start_date or reset_end_date or start_date is not None or end_date is not None:
        new_start = None if reset_start_date else (start_date if start_date is not None else ...)
        new_end = None if reset_end_date else (end_date if end_date is not None else ...)
        project = db.set_project_date_overrides(project_id, start=new_start, end=new_end)
    if not project:
        return None
    result = _to_public_project(db.get_project(project["id"]) or project)
    if card_warnings:
        result["warnings"] = card_warnings
    return result


@mcp.tool()
def constructicon_add_to_project(slug: str, project_id: str | int) -> list[dict]:
    """Add an object to a project.

    If the project has a linked tag, the object is also tagged with it.
    Returns the object's updated project list.
    """
    if db.get_by_slug(slug) is None:
        raise ValueError("slug not found")
    if db.get_project(project_id) is None:
        raise ValueError("project not found")
    project = db.get_project(project_id)
    db.add_item_to_project(project["id"], slug)
    if project.get("tag_id"):
        db.attach_tags(slug, [project["tag_id"]])
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


@mcp.tool()
def constructicon_remove_from_project(slug: str, project_id: str | int) -> list[dict]:
    """Remove an object from a project (does not untag it).

    Returns the object's updated project list.
    """
    if db.get_by_slug(slug) is None:
        raise ValueError("slug not found")
    project = db.get_project(project_id)
    if project is None:
        raise ValueError("project not found")
    db.remove_item_from_project(project["id"], slug)
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


@mcp.tool()
def constructicon_add_items_to_project(project_id: str | int, slugs: list[str]) -> list[dict]:
    """Add multiple objects to a project in a single call.

    Convenience wrapper around constructicon_add_to_project for bulk operations.
    If the project has a linked tag, objects are also tagged with it.
    Returns the list of added objects.
    """
    project = db.get_project(project_id)
    if project is None:
        raise ValueError("project not found")

    added = []
    for slug in slugs:
        if db.get_by_slug(slug) is None:
            continue
        db.add_item_to_project(project["id"], slug)
        if project.get("tag_id"):
            db.attach_tags(slug, [project["tag_id"]])
        added.append(_to_public(db.get_by_slug(slug)))

    return added


@mcp.tool()
def constructicon_set_project_writeup(project_id: str | int, slug: str, owner_words: bool | None = None) -> dict | None:
    """Set a project's write-up document to a given object.

    The object must exist and be an item whose type declares writeup_body_key
    (i.e., can serve as a write-up). The item is also added to the project's
    items if not already present.
    owner_words (optional): true marks the write-up as holding the OWNER'S OWN wording (the
    oral-history flow), which earns the card its "owner words" pip (V2 cards 3.11); false
    clears the mark; omit to leave it alone.
    Returns the updated project, or None if not found.
    """
    project = db.get_project(project_id)
    if project is None:
        return None
    row = db.get_by_slug(slug)
    if row is None:
        raise ValueError("writeup slug not found")
    if not object_types.can_be_writeup(row):
        label = object_types.get_object_type(row.get("media_type")).label
        raise ValueError(f"{label} items can't be a project write-up (their type declares no writeup_body_key)")

    # Add the writeup document to the project items if not already there
    db.add_item_to_project(project["id"], slug)
    if owner_words is not None:
        db.update_content_metadata(slug, type_metadata={"owner_words": bool(owner_words)})

    # Update the project's writeup_slug
    updated = db.update_project(project["id"], writeup_slug=slug)
    return _to_public_project(updated) if updated else None


@mcp.tool()
def constructicon_get_project(id_or_slug: str | int) -> dict | None:
    """Get a project with full details: metadata, items, tags, cover, and write-up body.

    Returns a comprehensive dict with:
    - id, slug, title, description, cover_slug, writeup_slug, parent_id
    - kind, activity, stage, stop_reason (+ labels) and needs_input: the live V2 status;
      `status` is the frozen legacy v1 word
    - open_decisions: any still-open questions about this card (with the suggested answer)
    - families: the families/collections this card is in; members: the members of this
      card when it is a family or collection; children: cards nested under it ("part of")
    - links: typed links in both directions (see constructicon_list_links)
    - items: list of objects in the project (with their tags)
    - cover: the cover object if cover_slug is set, else None
    - writeup: the writeup document object if writeup_slug is set, else None

    Returns None if the project is not found.
    """
    project = db.get_project(id_or_slug)
    if project is None:
        return None

    # Get the project items
    items = db.list_project_items(project["id"])
    items_public = []
    for item in items:
        item_dict = _to_public(item)
        # Add tags for each item
        tags = db.list_tags_for_post(item["slug"])
        item_dict["tags"] = [t["name"] for t in tags]
        items_public.append(item_dict)

    # Get the cover object if it exists
    cover = None
    if project.get("cover_slug"):
        cover_row = db.get_by_slug(project["cover_slug"])
        if cover_row:
            cover = _to_public(cover_row)

    # Get the writeup object if it exists
    writeup = None
    if project.get("writeup_slug"):
        writeup_row = db.get_by_slug(project["writeup_slug"])
        if writeup_row:
            writeup = _to_public(writeup_row)

    return {
        **_to_public_project(project),
        "items": items_public,
        "cover": cover,
        "writeup": writeup,
        "open_decisions": [cards.decision_summary(d) for d in cards.open_card_decisions(project["slug"])],
        # V2 cards 3.6/3.7: families this card is in, members (if it is a family or
        # collection), and the cards nested under it ("part of").
        **cards.family_fields(project),
        # V2 cards 3.8: typed links, both directions, with labels.
        "links": cards.list_links(project["slug"]),
        "children": [{"id": c["id"], "slug": c["slug"], "title": c["title"]}
                     for c in db.list_child_projects(project["id"])],
    }


# --- Hobbies (#360) ---

@mcp.tool()
def constructicon_list_hobbies() -> list[dict]:
    """List all hobbies (tags marked is_hobby=1) with their metadata and project counts.

    Each hobby: {id, name, slug, status, group_code, project_count, flags}.
    status is the manual Active/Inactive switch ('active' | 'inactive'); group_code is the
    2-4 char code shown on cards. flags are COMPUTED on every call (never stored), a list of
    {code, label, detail}: inactive_with_active_work (an inactive hobby that has an active
    project) and active_untouched (an active hobby nothing in has been touched for ~2 years).
    A flag only points at a mismatch; the owner flips the switch by hand.
    Ordered by name."""
    out = []
    for h in db.list_hobbies():
        f = cards.hobby_fields(h)
        out.append({
            "id": f["id"],
            "name": f["name"],
            "slug": f["slug"],
            "status": f["status"],
            "group_code": f["group_code"],
            "project_count": h.get("project_count", 0),
            "flags": f["flags"],
        })
    return out


@mcp.tool()
def constructicon_create_hobby(name: str, status: str = "active") -> dict:
    """Create an empty hobby from just a name (#418).

    Mirrors POST /api/hobbies: creates (or reuses, via get_or_create_tag) a
    top-level blog_tags row with the given name and marks it as a hobby. The
    HTTP route always uses status='active'; this tool also accepts an explicit
    status so a hobby can be stood up inactive in one call.

    status: 'active' or 'inactive' (default 'active'); the deprecated v1 words
    'dormant'/'abandoned' are accepted and stored as 'inactive'.
    Raises ValueError if the name is blank or the status is invalid.
    Returns the new hobby dict {id, name, slug, status, group_code}."""
    name = name.strip()
    if not name:
        raise ValueError("Hobby name can't be empty")

    tag = db.get_or_create_tag(name, parent_id=None)
    db.mark_tag_as_hobby(tag["id"], status=status)
    made = db.get_hobby(tag["id"])
    return {
        "id": tag["id"],
        "name": tag["name"],
        "slug": tag["slug"],
        "status": made["hobby_status"],
        "group_code": made["group_code"],
    }


@mcp.tool()
def constructicon_convert_project_to_hobby(project_slug: str) -> dict | None:
    """Convert an existing project into a hobby (DESTRUCTIVE).

    The project is converted into a hobby tag, its child projects are moved to the hobby
    via project_hobbies, its items are tagged with the hobby, and the project row is deleted.

    This is a one-way operation — to undo, the hobby would need to be converted back
    manually.

    Returns the new hobby tag dict with a summary of what was moved, or None if the
    project doesn't exist."""
    project = db.get_project(project_slug)
    if project is None:
        return None

    # Get counts before conversion for the summary
    children_count = len(db.list_child_projects(project["id"]))
    items_count = len(db.list_project_items(project["id"]))

    hobby = cards.convert_project_to_hobby(project["id"], actor="mcp")

    if hobby is None:
        return None

    return {
        "id": hobby["id"],
        "name": hobby["name"],
        "slug": hobby["slug"],
        "status": hobby.get("hobby_status"),
        "summary": {
            "children_moved": children_count,
            "items_moved": items_count,
        },
    }


@mcp.tool()
def constructicon_add_project_to_hobby(project_slug: str, hobby_slug: str) -> dict | None:
    """Add a project to a hobby's many-to-many relation.

    Both project_slug and hobby_slug are resolved to their respective ids.
    Returns the updated hobby with its project count, or None if either doesn't exist."""
    project = db.get_project(project_slug)
    if project is None:
        raise ValueError("project not found")

    hobby = db.get_hobby(hobby_slug)
    if hobby is None:
        raise ValueError("hobby not found")

    cards.add_to_hobby(project["id"], hobby["id"], actor="mcp")  # logged (V2 cards 3.13)

    # Return the updated hobby
    updated_hobby = db.get_hobby(hobby["id"])
    projects = db.list_projects_for_hobby(hobby["id"])

    return {
        "id": updated_hobby["id"],
        "name": updated_hobby["name"],
        "slug": updated_hobby["slug"],
        "status": updated_hobby.get("hobby_status"),
        "project_count": len(projects),
    }


@mcp.tool()
def constructicon_set_hobby_status(hobby_slug: str, status: str) -> dict | None:
    """Set a hobby's manual Active/Inactive switch (V2 cards 3.3).

    status: 'active' | 'inactive'. The v1 words 'dormant' and 'abandoned' are deprecated
    aliases: they are stored as 'inactive' and the result carries a `warnings` entry.
    The change is recorded in the change log. Mismatch flags (an inactive hobby with
    active work, an active hobby untouched ~2 years) are computed, never stored: the
    result's `flags` shows them as of now.

    Returns {id, name, slug, status, group_code, flags, warnings, changes, batch_id}, None
    if the hobby isn't found, or {"ok": false, "error": {code: "bad_hobby_activity",
    message}} for an unknown value."""
    hobby = db.get_hobby(hobby_slug)
    if hobby is None:
        return None
    try:
        result = cards.set_hobby_activity(hobby["id"], status, actor="mcp")
    except card_rules.CardError as e:
        return _card_error_result(e)
    updated = cards.hobby_fields(db.get_hobby(hobby["id"]))
    return {
        "id": updated["id"],
        "name": updated["name"],
        "slug": updated["slug"],
        "status": updated["status"],
        "group_code": updated["group_code"],
        "flags": updated["flags"],
        "warnings": result.warnings,
        "changes": result.changes,
        "batch_id": result.batch_id,
    }


@mcp.tool()
def constructicon_set_hobby_code(hobby_slug: str, group_code: str) -> dict | None:
    """Set a hobby's 2-4 char group code (the code shown on cards, e.g. COL, 3DP, RCA).

    Letters/digits only, unique across hobbies; stored uppercase. Returns
    {id, name, slug, group_code, changes, batch_id}, None if the hobby isn't found, or
    {"ok": false, "error": {code: "bad_group_code" | "group_code_conflict", message}}."""
    hobby = db.get_hobby(hobby_slug)
    if hobby is None:
        return None
    try:
        result = cards.set_group_code(hobby["id"], group_code, actor="mcp")
    except card_rules.CardError as e:
        return _card_error_result(e)
    updated = db.get_hobby(hobby["id"])
    return {"id": updated["id"], "name": updated["name"], "slug": updated["slug"],
            "group_code": updated["group_code"], "changes": result.changes, "batch_id": result.batch_id}


@mcp.tool()
def constructicon_list_blog_entries(status: str | None = None) -> list[dict]:
    """List all blog entries, optionally filtered by status (e.g. 'draft', 'published').

    Returns entries ordered by most-recently-updated first.
    """
    entries = db.list_blog_entries(status=status)
    return [
        {
            "id": e["id"],
            "slug": e["slug"],
            "title": e["title"],
            "subtitle": e.get("subtitle", ""),
            "status": e["status"],
            "cover_slug": e.get("cover_slug"),
            "content_date": e.get("content_date"),
            "created_at": e["created_at"],
            "updated_at": e["updated_at"],
        }
        for e in entries
    ]


@mcp.tool()
def constructicon_create_blog_entry(title: str, subtitle: str = "", body: str = "", status: str = "draft",
                                    cover_slug: str | None = None, content_date: float | None = None) -> dict:
    """Create a new blog entry.

    title: The entry's title (required; a slug is auto-generated from this).
    subtitle, body: Optional metadata and content.
    status: The entry's status (default 'draft'). Common values: 'draft', 'published'.
    cover_slug: Optional slug of a Constructicon object to use as the entry's cover image.
    content_date: Optional unix timestamp for the entry's content date (distinct from created_at).

    Returns the created entry with projects and items (initially empty).
    """
    entry = db.create_blog_entry(title=title, subtitle=subtitle, body=body, status=status,
                                 cover_slug=cover_slug, content_date=content_date)
    return _to_public_blog_entry(entry)


@mcp.tool()
def constructicon_get_blog_entry(slug: str) -> dict | None:
    """Get a single blog entry by slug, with full hydration: projects and items.

    Returns None if not found.
    """
    entry = db.get_blog_entry(slug)
    return _to_public_blog_entry(entry) if entry else None


@mcp.tool()
def constructicon_update_blog_entry(slug: str, title: str | None = None, subtitle: str | None = None,
                                    body: str | None = None, status: str | None = None,
                                    cover_slug: str | None = None, content_date: float | None = None,
                                    clear_cover_slug: bool = False, clear_content_date: bool = False) -> dict | None:
    """Update a blog entry. Every field defaults to "leave unchanged".

    Pass a value to set it. cover_slug/content_date can't be cleared just by
    passing None (None means "leave unchanged" here), so to clear one, set its
    clear_* flag instead: clear_cover_slug / clear_content_date.

    (The Ellipsis sentinel db.update_blog_entry uses internally can't cross the
    MCP tool boundary — the schema generator treats an Ellipsis default as a
    required arg — so this tool maps None/clear-flags onto that sentinel.)

    Returns the updated entry with full hydration, or None if not found.
    """
    cover = None if clear_cover_slug else (cover_slug if cover_slug is not None else ...)
    cdate = None if clear_content_date else (content_date if content_date is not None else ...)
    updated = db.update_blog_entry(slug, title=title, subtitle=subtitle, body=body, status=status,
                                   cover_slug=cover, content_date=cdate)
    return _to_public_blog_entry(updated) if updated else None


@mcp.tool()
def constructicon_delete_blog_entry(slug: str) -> bool:
    """Delete a blog entry and all its attached projects/items. Irreversible.

    Returns True if deleted, False if not found.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        return False
    db.delete_blog_entry(slug)
    return True


@mcp.tool()
def constructicon_set_blog_entry_projects(slug: str, items: list[dict]) -> dict | None:
    """Set the ordered list of projects attached to a blog entry.

    items: list of dicts with 'project_id' (int) and optional 'note' (str) keys,
      in desired display order. Each dict becomes a (project_id, note) tuple.

    Returns the updated entry with full hydration, or None if not found.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        return None

    # Convert dicts to (project_id, note) tuples
    project_items = [(item.get("project_id"), item.get("note", "")) for item in items]
    db.set_entry_projects(entry["id"], project_items)
    updated = db.get_blog_entry(slug)
    return _to_public_blog_entry(updated) if updated else None


@mcp.tool()
def constructicon_set_blog_entry_items(slug: str, items: list[dict]) -> dict | None:
    """Set the ordered list of Constructicon objects attached to a blog entry.

    items: list of dicts with 'slug' (str) and optional 'note' (str) keys,
      in desired display order. Each dict becomes a (post_slug, note) tuple.

    Returns the updated entry with full hydration, or None if not found.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        return None

    # Convert dicts to (post_slug, note) tuples
    post_items = [(item.get("slug"), item.get("note", "")) for item in items]
    db.set_entry_items(entry["id"], post_items)
    updated = db.get_blog_entry(slug)
    return _to_public_blog_entry(updated) if updated else None


@mcp.tool()
def constructicon_list_tags() -> list[dict]:
    """Get the complete tag tree (hierarchical).

    Returns the root-level tags, each with a nested children array.
    """
    return db.list_tag_tree()


@mcp.tool()
def constructicon_create_tag(name: str, parent_name: str | None = None) -> dict:
    """Create a tag or return the existing one if it already exists under the parent.

    Returns the tag metadata.
    """
    parent_id = None
    if parent_name:
        parent = db.get_or_create_tag(parent_name, parent_id=None)
        parent_id = parent["id"]
    tag = db.get_or_create_tag(name, parent_id=parent_id)
    return tag


@mcp.tool()
def constructicon_get_posts_for_tag(tag_name: str) -> list[dict]:
    """Get all objects tagged with a specific tag (including all descendant tags).

    Returns a list of objects.
    """
    tag = db._find_tag_by_name(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise ValueError(f"No such tag: {tag_name!r}")
    rows = db.list_posts_for_tag(tag["id"], limit=10000)
    return [_to_public(r) for r in rows]


@mcp.tool()
def constructicon_attach_tags(slug: str, tag_names: list[str]) -> dict | None:
    """Add tags to an object.

    Returns the updated object, or None if not found.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    tag_ids = []
    for name in tag_names:
        tag = db.get_or_create_tag(name, parent_id=None)
        tag_ids.append(tag["id"])
    if tag_ids:
        db.attach_tags(slug, tag_ids)
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_detach_tag(slug: str, tag_name: str) -> dict | None:
    """Remove a tag from an object.

    Returns the updated object, or None if not found.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    tag = db._find_tag_by_name(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise ValueError(f"No such tag: {tag_name!r}")
    db.detach_tag(slug, tag["id"])
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_retry_ocr(slug: str) -> dict | None:
    """Force OCR to run (or re-run) on an object.

    Returns the updated object, or None if not found. Raises an error if
    the object is redacted or doesn't support OCR.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if row["redacted"]:
        raise ValueError("File was redacted — there's no content left to OCR")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.ocr_capable:
        raise ValueError(f"OCR isn't available for {spec.label} content")
    db.set_ocr_status(slug, "pending")
    threading.Thread(target=ocr.run_ocr, args=(slug,), daemon=True).start()
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_set_agent_notes(slug: str, notes: str | None = None) -> dict | None:
    """Set or clear agent-authored working notes for an object (#206).

    Agent-only scratch space for Claude working state, never exposed in the
    public API. For example: "this is a Fusion 360 screenshot, not user-facing"
    or "already inventoried, skip on re-run."

    notes: the note text, or None to clear existing notes.
    Returns the updated object (the usual public shape plus "agent_notes"),
    or None if not found.
    """
    row = db.set_agent_notes(slug, notes)
    if row is None:
        return None
    # Never return the raw DB row: it carries the `embedding` BLOB, which
    # isn't JSON-serializable, so the tool call itself failed for every row
    # that had been through OCR (#211).
    return {**_to_public(row), "agent_notes": row.get("agent_notes")}


@mcp.tool()
def constructicon_list_provenance_options(scope: str = "card", include_retired: bool = False) -> dict:
    """The live, owner-editable provenance lists (#529). Read-only; the owner adds,
    renames, retires and reorders options in /admin.

    scope: "card" (what constructicon_set_card_provenance accepts) or "file" (what
    constructicon_set_provenance accepts). include_retired=true also returns retired
    options (retired ones can't be set on anything new).
    Returns {"ok": true, "scope", "options": [{key, label, sort_order, retired}]}.
    """
    try:
        opts = provenance_options.list_options(scope, include_retired=include_retired)
    except card_rules.CardError as e:
        return _card_error_result(e)
    return {"ok": True, "scope": scope,
            "options": [{k: o[k] for k in ("key", "label", "sort_order", "retired")} for o in opts]}


@mcp.tool()
def constructicon_set_provenance(slug: str, provenance: str | None = None) -> dict | None:
    """Set or clear an object's provenance classification (#341).

    Provenance describes how an object came to be captured. The allowed values are
    an editable list (#529) the owner manages in /admin; it starts as found, created,
    documented, result (outcome of a process), reference (cited or sourced from
    elsewhere), design (drafted/designed) and purchased. Call
    constructicon_list_provenance_options(scope="file") for the live list. A value
    that isn't an active key returns {"ok": false, "error": {"code":
    "bad_provenance", ...}} naming the active keys; an object that already holds a
    since-retired key keeps it.

    provenance: an active key, or None to clear.
    Returns the updated object (the usual public shape plus "provenance"),
    or None if not found.
    """
    try:
        row = db.set_provenance(slug, provenance)
    except card_rules.CardError as e:
        return _card_error_result(e)
    if row is None:
        return None
    return {**_to_public(row), "provenance": row.get("provenance")}


@mcp.tool()
def constructicon_set_content_date(slug: str, date: str | None = None) -> dict | None:
    """Set or clear an object's content_date — the content's OWN real-world date
    (#415), the date sibling of constructicon_set_provenance. Distinct from the
    upload timestamp; it's what the Timeline and date displays key off.

    date:
      - an ISO date or datetime string, e.g. "2017-07-31" or "2017-07-31 14:30"
        or "2017-07-31T14:30:00". A naive (offset-less) value is interpreted as
        Mountain Time (America/Denver), matching the archive's timeline
        convention; a string carrying an explicit offset is trusted as-is.
      - a bare unix-seconds number (as a string) is used directly.
      - None or "" clears the date.

    Returns the updated object (usual public shape plus "content_date" in unix
    seconds), or None if the object doesn't exist.
    """
    if db.get_by_slug(slug) is None:
        return None
    if date is None or date.strip() == "":
        db.set_content_date(slug, None)
    else:
        s = date.strip()
        try:
            epoch = float(s)  # already unix seconds
        except ValueError:
            try:
                dt = datetime.fromisoformat(s)
            except ValueError as e:
                raise ValueError(
                    f"Unrecognized date {date!r}: use an ISO date/datetime "
                    f"('YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS') or unix seconds"
                ) from e
            epoch = timeline.source_datetime_to_epoch(dt)  # naive -> Mountain Time
        db.set_content_date(slug, epoch)
    row = db.get_by_slug(slug)
    return {**_to_public(row), "content_date": row.get("content_date")}


@mcp.tool()
def constructicon_set_highlight(slug: str, on: bool = False) -> dict | None:
    """Mark or unmark an object as highlighted (#341).

    Highlight is an orthogonal boolean flag for "cool/unique" objects —
    featured in project exports and curated views.

    on: True to mark as highlighted, False to clear.
    Returns the updated object (the usual public shape plus "highlight"),
    or None if not found.
    """
    row = db.set_highlight(slug, on)
    if row is None:
        return None
    return {**_to_public(row), "highlight": row.get("highlight")}


@mcp.tool()
def constructicon_set_brand_asset(slug: str, is_brand: bool = False, brand_role: str | None = None) -> dict | None:
    """Mark or unmark an object as a brand asset (#350).

    Brand assets are reusable branding objects (logos, icons, colors, etc.)
    that live outside the project/hobby structure and are consumed by
    export templates.

    slug: the object's slug.
    is_brand: True to mark as a brand asset, False to clear.
    brand_role: optional role label (e.g., "logo", "icon", "color").
                One of the standard BRAND_ROLES or a custom value.
                Ignored when is_brand is False; brand_role is cleared
                when the flag is cleared.

    Returns the updated object (the usual public shape plus
    "is_brand_asset" and "brand_role"), or None if not found.
    """
    row = db.set_brand_asset(slug, is_brand, brand_role=brand_role)
    if row is None:
        return None
    return {**_to_public(row), "is_brand_asset": bool(row.get("is_brand_asset")), "brand_role": row.get("brand_role")}


@mcp.tool()
def constructicon_list_brand_assets() -> list[dict]:
    """List all brand assets (#350), grouped by role and ordered by recency.

    Returns a list of brand asset dicts (the usual public shape plus
    "is_brand_asset" and "brand_role").
    """
    assets = db.list_brand_assets()
    return [
        {**_to_public(asset), "is_brand_asset": bool(asset.get("is_brand_asset")), "brand_role": asset.get("brand_role")}
        for asset in assets
    ]


@mcp.tool()
def constructicon_set_project_status(id_or_slug: str, status: str) -> dict | None:
    """DEPRECATED alias of constructicon_set_status that still accepts the v1 words.

    Legacy words are translated to a stage and the result carries `warnings`
    saying so: wip/active -> in_progress; complete/archived/published/
    means-to-an-end -> done (ambiguous: use set_status with in_use if it is still
    in use); shelved -> paused; abandoned -> stopped (abandoned); failed -> stopped
    (failed); idea -> idea; reference-only -> kind collection + in_use. A v2 stage
    name is accepted too. Returns the updated project dict (with `warnings`), None
    if not found, or {"ok": false, "error": {code, message}} for an invalid status.
    """
    if db.get_project(id_or_slug) is None:
        return None
    try:
        legacy = card_rules.legacy_to_status(status)
        warnings = list(legacy["warnings"])
        if legacy["kind"]:
            warnings.extend(cards.set_kind(id_or_slug, legacy["kind"], actor="mcp").warnings)
        warnings.extend(cards.set_status(id_or_slug, legacy["stage"], legacy["stop_reason"], actor="mcp").warnings)
    except card_rules.CardError as e:
        return _card_error_result(e)
    return {**_to_public_project(db.get_project(id_or_slug)), "warnings": warnings}


@mcp.tool()
def constructicon_set_status(card: str | int, stage: str, stop_reason: str | None = None,
                             activity: str | None = None, dry_run: bool = False) -> dict:
    """Set a card's status: stage (+ stop_reason when stopped). V2 cards 3.2.

    stage: in_progress | in_use (both active) or idea | paused | done | stopped
    (all inactive). Activity is derived from the stage; passing `activity` that
    disagrees with it is an error. stopped REQUIRES stop_reason failed|abandoned,
    and a stop_reason on any other stage is an error. An idea can never be active.
    An event only allows idea / in_progress / done / stopped. Any stage can follow
    any other (no state machine). Rule violations return
    {"ok": false, "error": {"code": "bad_status", "message": ...}}; nothing is written.

    card: project id or slug. dry_run=true previews without writing.
    Returns {ok, dry_run, changes: [{card, field, before, after}], warnings, batch_id}.
    """
    try:
        return cards.set_status(card, stage, stop_reason, activity=activity, dry_run=dry_run,
                                actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_set_kind(card: str | int, kind: str, force: bool = False, dry_run: bool = False) -> dict:
    """Set a card's kind: project | thing | action | family | collection | event.

    A thing is one physical object (a one-object build is a Thing, not a Project).
    family and collection are group kinds: they can't be nested under another
    card or have nested children. Leaving a group kind that has members is refused
    unless force=true (drops the memberships). The card's current stage must stay
    valid for the new kind (an event can't be in use or paused).

    Returns {ok, dry_run, changes, warnings, batch_id} or
    {"ok": false, "error": {code, message}}.
    """
    try:
        return cards.set_kind(card, kind, force=force, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_set_whereabouts(card: str | int, whereabouts: str | None = None, note: str | None = None,
                                  clear_note: bool = False, dry_run: bool = False) -> dict:
    """Set (or clear) where a card's physical thing is now. V2 cards 3.4.

    whereabouts: have_it | partial | parted_out | sold | gifted | lost | never_built,
    or omit/null to CLEAR it (null = not recorded). Applies to thing, project and
    collection cards only; an action, event or family is refused. Cross-rules with
    the card's stage: an in_use card can only be have_it or partial; a never_built
    card can't be in_progress or in_use. Violations return
    {"ok": false, "error": {"code": "bad_whereabouts", "message": ...}}; nothing is written.
    note: optional free text ("Skyhawk model on the shelf; parts printed, not
    assembled"); leave it out to keep the existing note, pass clear_note=true to erase it.

    card: project id or slug. dry_run=true previews without writing.
    Returns {ok, dry_run, changes: [{card, field, before, after}], warnings, batch_id}.
    """
    try:
        return cards.set_whereabouts(card, whereabouts, "" if clear_note else (note if note is not None else ...),
                                     dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_set_card_provenance(card: str | int, provenance: str | None = None, credit: str | None = None,
                                      clear_credit: bool = False, dry_run: bool = False) -> dict:
    """Set (or clear) a CARD's provenance and optional credit. V2 cards 3.5.

    provenance: an active key of the editable card list (#529; starts as created |
    found | collected | referenced | client_owned | purchased; the owner manages it in
    /admin, and constructicon_list_provenance_options(scope="card") returns the live
    list), or omit/null to CLEAR it. A card that already holds a since-retired key
    keeps it. One value per card; a card with mixed origins should be split into
    separate cards, never "found + own". credit: who designed it / where it came from
    (leave out to keep the existing credit; clear_credit=true erases it). Bad values
    return {"ok": false, "error": {"code": "bad_provenance", ...}}.

    This is NOT constructicon_set_provenance, which sets the per-FILE provenance
    (found/created/documented/result/reference/design) on one object by file slug and
    is unchanged. A file with no provenance of its own shows its card's for display only.

    card: project id or slug. dry_run=true previews without writing.
    Returns {ok, dry_run, changes, warnings, batch_id}.
    """
    try:
        return cards.set_provenance(card, provenance, "" if clear_credit else (credit if credit is not None else ...),
                                    dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_set_card_highlight(card: str | int, on: bool = False, dry_run: bool = False) -> dict:
    """Mark or unmark a CARD as highlighted ("this one is special"). V2 cards 3.12.
    Independent of the per-file highlight (constructicon_set_highlight, which takes a
    file slug). card: project id or slug. Returns {ok, dry_run, changes, warnings, batch_id}."""
    try:
        return cards.set_highlight(card, on, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_nest(child: str | int, parent: str | int, replace: bool = False, dry_run: bool = False) -> dict:
    """Make `child` PART OF `parent` (nesting: the child only makes sense inside its
    parent, e.g. a Lua script inside the truck it runs on). V2 cards 3.7.

    Refused with {"ok": false, "error": {code, message}}:
    - nest_self: a card can't be part of itself
    - nest_cycle: the parent is already nested under the child
    - nest_group_kind: a family or collection can't be nested or be a parent (use
      constructicon_add_to_family instead)
    - nest_second_parent: the child is already part of another card; pass
      replace=true to move it (or constructicon_unnest first)

    Independent of family membership. dry_run=true previews. Returns
    {ok, dry_run, changes, warnings, batch_id}.
    """
    try:
        return cards.nest(child, parent, replace=replace, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_unnest(child: str | int, dry_run: bool = False) -> dict:
    """Take `child` out of its parent so it stands on its own (no-op if it has none)."""
    try:
        return cards.unnest(child, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_add_to_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    """Put `member` in a family or collection (V2 cards 3.6). Membership is
    many-to-many: a card can be in several families, and a family has many members;
    it is NOT nesting and moves no files. Adding twice is a no-op.

    `family` must be a card of kind family or collection; `member` must not be one
    (families don't contain each other). Violations return
    {"ok": false, "error": {"code": "bad_membership", ...}}. dry_run=true previews.
    Returns {ok, dry_run, changes, warnings, batch_id}.
    """
    try:
        return cards.add_to_family(family, member, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_remove_from_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    """Take `member` out of a family or collection (no-op if it wasn't in it).
    Neither card is otherwise changed."""
    try:
        return cards.remove_from_family(family, member, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


# --- Typed links (V2 cards 3.8) ---

@mcp.tool()
def constructicon_link(a: str | int, b: str | int, type: str, note: str = "", dry_run: bool = False,
                       batch_id: str | None = None) -> dict:
    """Link two cards: "a <type> b". V2 cards 3.8.

    type: built_for (a was built for b) | applies_to (a is applied to b) | used_in
    (a is used in b) | inspired_by (a was inspired by b) | related (symmetric, stored
    both ways). Directed types are stored once and read in reverse from b's side
    (constructicon_list_links shows both). A pair can't be both typed and related:
    a typed link over a related pair UPGRADES it (the related rows are removed),
    while related over a typed pair is refused.

    Errors come back as {"ok": false, "error": {code, message}}, same codes as HTTP:
    bad_link (unknown type, self-link, a family/collection as the source of
    built_for/applies_to/used_in), link_conflict (already linked, or related over a
    typed pair), not_found (no such card). dry_run=true previews.
    batch_id (optional): join an earlier call's batch so one constructicon_undo reverses
    them together (e.g. the split_card, link, add_to_hobby of a reorganization).
    Returns {ok, dry_run, changes, warnings, batch_id}.
    """
    try:
        return cards.link(a, b, type, note, dry_run=dry_run, actor="mcp", batch_id=batch_id).to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_unlink(a: str | int, b: str | int, type: str | None = None, dry_run: bool = False) -> dict:
    """Remove link(s) between two cards. With `type`, only that type ("a <type> b"
    exactly for a directed type; related removes both rows); without it, every link
    between the pair in either direction. A no-op (with a warning) when nothing matches.
    Returns {ok, dry_run, changes, warnings, batch_id} or {"ok": false, "error": ...}."""
    try:
        return cards.unlink(a, b, type, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_retype_link(a: str | int, b: str | int, from_type: str, to_type: str,
                              note: str | None = None, dry_run: bool = False) -> dict:
    """Change a link's type in one step (the upgrade/downgrade path): replaces the
    `from_type` link between the pair (found in either direction) with
    "a <to_type> b". The note carries over unless `note` is given. Errors: not_found
    (no such link), bad_link (same type / unknown type / group-kind source),
    link_conflict (the new link already exists, or related while another typed link
    is on the pair). dry_run=true previews."""
    try:
        return cards.retype_link(a, b, from_type, to_type, note=note, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_retype_links(mapping: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Bulk-retype v1 `related` links to typed ones. DRY-RUN BY DEFAULT: pass
    dry_run=false to write.

    mapping: [{a, b, to_type, from_type?='related'}] where each item reads
    "a <to_type> b" (card ids or slugs). Every item is validated first; the whole
    batch is applied in one transaction (all-or-nothing) unless partial_ok=true,
    which applies the valid items and reports the rest. Find candidates with
    constructicon_list_needs_decision(need='untyped_link').
    Returns {ok, dry_run, applied, changes: [{card, field, before, after}], warnings,
    batch_id, items: [{a, b, to_type, ok, changes, error?}]}."""
    return cards.retype_links(mapping, dry_run=dry_run, partial_ok=partial_ok, actor="mcp")


@mcp.tool()
def constructicon_list_links(card: str | int) -> list[dict] | dict:
    """Every link on a card, both directions: [{slug, title, kind, stage, type,
    direction, label, note}]. direction is out (this card is the source: "this
    <type> that"), in (this card is the target: labelled in reverse, e.g. "Made
    for this") or both (related, symmetric)."""
    try:
        return cards.list_links(card)
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_list_needs_decision(kind: str | None = None, need: str | None = None,
                                      hobby: str | None = None, limit: int | None = None,
                                      card: str | int | None = None) -> list[dict]:
    """List cards waiting on an owner decision, with a suggested answer for each.

    Two sources. STORED questions queued by the v1 -> v2 migration:
    need = card_status (done vs in use / paused vs collection), card_built_for
    (a means-to-an-end card: which card was it built for), card_kind (Thing or
    Project?), card_family_members (is this nested-parent really a family, and which
    cards belong in it: pick several); answer one with constructicon_resolve_pending_decision
    (decision_id, choice = an option key). COMPUTED needs, recomputed on every
    call and never stored (decision_id is null; card_slug is null and hobby_slug
    is set, since they belong to a hobby): need = hobby_inactive_with_active_work
    (an Inactive hobby that has an active project) and hobby_active_untouched (an
    Active hobby untouched for ~2 years). Fix those with constructicon_set_hobby_status.
    Also computed: need = untyped_link (a v1 'related' pair where a typed reading is
    plausible; `link` = {a, b, type} is the suggestion, apply with constructicon_retype_links).
    Also computed: need = missing_provenance (a card with no provenance; `suggested` is the
    majority file provenance mapped to a card value when >50% of its non-write-up files
    agree, never applied; fix with constructicon_set_card_provenance),
    missing_provenance_credit (a found/collected card with no credit) and
    missing_whereabouts (a Thing with none; fix with constructicon_set_whereabouts).
    Also computed: need = status_conflict (active/inactive disagrees with the parent card, or Done
    with nested work still in progress) and blank_writeup_with_files (the auto-made write-up is
    still empty although the card has files). To clear many stored questions at once, see
    constructicon_resolve_decisions (accept_suggested + dry-run).

    Filters: card (one card, slug or id), kind (the card's kind, or 'hobby' for hobby needs only; any other kind
    leaves hobby needs out), need, hobby (slug), limit.
    Each row: {need, card_slug, title, detail, suggested, suggested_reason,
    confidence, decision_id, options: [{key, label}]}. `suggested` is only a
    suggestion; nothing is applied until the decision is resolved.
    """
    return cards.list_needs_decision(kind=kind, need=need, hobby=hobby, limit=limit, card=card)


# --- V2 cards piece 6: the reorganizing toolkit (spec 7.1 / 7.3) -------------------
# Thin wrappers over core/cards.py; no rule lives here. Single-card tools apply by
# default; bulk tools (bulk_edit, resolve_decisions, retype_links) are DRY-RUN by default.

@mcp.tool()
def constructicon_split_card(source: str | int, parts: list[dict], keep_in_source: bool = False,
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Carve files (and a description) out of one card into new cards. V2 cards 6 / 7.4.

    `parts` is a list; each part is {title, kind='project', relation='sibling'|'child',
    stage, stop_reason, provenance, provenance_credit, whereabouts, whereabouts_note,
    description, move_description, file_slugs: [file slugs], link_to_source: {type, note?}
    or null, hobbies: 'inherit'|[hobby slugs], families: 'inherit'|[family slugs], highlight}.
    A `child` is nested under the source (nest rules apply); a `sibling` has no parent and,
    by default, gets the source's hobbies and family memberships copied (a child gets none
    unless you say so). Each part gets its own kind, provenance, stage and whereabouts, and
    is a full card with the usual blank write-up. Files in file_slugs are MOVED out of the
    source (one file may be named by several parts; it lands in each) unless
    keep_in_source=true (then copied); the source's write-up can't be split out.

    All in one transaction under one batch_id: any rule violation writes nothing. Pass that
    batch_id to later calls (constructicon_link, constructicon_add_to_hobby) to keep the
    whole reorganization in one batch, and constructicon_undo(batch_id) reverses it.
    dry_run=true previews exactly what would happen. Returns {ok, dry_run, changes, warnings,
    batch_id, created: [{id, slug, title, kind, relation, files}]}."""
    try:
        return cards.split_card(source, parts, keep_in_source=keep_in_source, dry_run=dry_run, actor="mcp",
                                batch_id=batch_id).to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_delete_project(card: str | int, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Delete a project/card cleanly (#497). Its own auto-made write-up goes with it only while
    it is still blank; a write-up with text is kept as an ordinary unfiled document and named in
    a warning. Links in either direction, family/hobby/file memberships and blog-entry attachments
    are removed; open questions about the card are resolved as stale. Nested children are
    orphaned (stand on their own), never deleted; files themselves are never deleted. Every row
    is imaged: constructicon_undo(batch_id) restores the whole thing. dry_run=true previews.
    Returns {ok, dry_run, changes, warnings, batch_id, deleted, children_orphaned, items_detached, writeup}."""
    try:
        res = cards.delete_card(card, dry_run=dry_run, actor="mcp", batch_id=batch_id)
        return {**res.to_dict(), **res.data}
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_merge_cards(keep: str | int, absorb: list[str | int], dry_run: bool = False,
                              batch_id: str | None = None) -> dict:
    """Fold one or more cards into `keep` (the reverse of split). V2 cards 6.

    Files are unioned (deduped); hobbies and family memberships unioned; the absorbed
    cards' children are re-parented to `keep`; links are re-pointed (self-links and duplicates
    dropped; a typed link beats a related one on the same pair); blog-entry attachments
    re-pointed. keep's cover and write-up win; an absorbed card's blank write-up goes with it
    and a real one stays as an ordinary file. Open questions about absorbed cards are resolved
    as stale. The absorbed card rows are deleted but row-imaged: constructicon_undo(batch_id)
    brings them back with the same id and files. Refused (nothing written) with nest_cycle, or
    bad_merge for a family/collection merged with a non-group card. dry_run=true previews.
    Returns {ok, dry_run, changes, warnings, batch_id, keep, absorbed}."""
    try:
        return cards.merge_cards(keep, absorb, dry_run=dry_run, actor="mcp", batch_id=batch_id).to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_move_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    """Move files (by slug) from one card to another: they leave from_card and join to_card.
    A card's own write-up can't be moved. Refused with bad_files if a file isn't in from_card.
    dry_run=true previews. Returns {ok, dry_run, changes, warnings, batch_id}."""
    try:
        return cards.move_files(slugs, from_card, to_card, dry_run=dry_run, actor="mcp", batch_id=batch_id).to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_copy_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    """Add files from one card to another WITHOUT removing them (files are many-to-many, so
    a photo can live in a project and in a Thing). Same rules as constructicon_move_files."""
    try:
        return cards.copy_files(slugs, from_card, to_card, dry_run=dry_run, actor="mcp", batch_id=batch_id).to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_add_to_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                               batch_id: str | None = None) -> dict:
    """Put a card in a hobby (many allowed; adding twice is a no-op). Logged and undoable.
    (constructicon_add_project_to_hobby is the older name for the same thing.)
    Returns {ok, dry_run, changes, warnings, batch_id}."""
    try:
        return cards.add_to_hobby(card, hobby, dry_run=dry_run, actor="mcp", batch_id=batch_id).to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_remove_from_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                                    batch_id: str | None = None) -> dict:
    """Take a card out of a hobby (no-op if it wasn't in it). Logged and undoable."""
    try:
        return cards.remove_from_hobby(card, hobby, dry_run=dry_run, actor="mcp", batch_id=batch_id).to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_set_home(card: str | int, target: str | int | None = None, dry_run: bool = False) -> dict:
    """Override a card's home (the breadcrumb parent / export link target), or clear the override.
    V2 cards 3.10. The automatic home is: the parent, else the first family the card is in, else
    its first hobby, else the home page; a manual override beats all of that.

    target: 'card:<slug>', 'hobby:<slug>', or a bare card slug/id (a bare name that is no card
    is tried as a hobby). Omit/null to CLEAR the override (back to automatic). A card can't be its
    own home (bad_home). A dangling override (target deleted) silently falls back to automatic and
    shows up in constructicon_explain_card's warnings. Home is not something to curate: this is for
    the rare correction. Returns {ok, dry_run, changes, warnings, batch_id}."""
    try:
        return cards.set_home(card, target, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_explain_card(card: str | int) -> dict:
    """Everything about a card in one call: what to read BEFORE proposing a change. Read-only.
    V2 cards 6.

    Returns identity (id, slug, title, description), kind, activity/stage/stop_reason (+ labels),
    `provisional` (true while the status is still the migration's guess), whereabouts/provenance
    (+ notes, credits), highlight, `hobbies` (with group codes and activity), `families`,
    `members` (for a family/collection), `parent`, `children`, `links` (typed, with direction),
    `home` (resolved + source: override|parent|family|hobby|none, + dangling_override) and
    `home_chain` (the breadcrumb), `files` (total, counts per media type, earliest/latest date),
    `level` (the 0-5 pips: cover, dates, writeup, owner_words, stack, each with a reason),
    `open_decisions` (stored questions, with suggested answers), `needs` (computed needs for this
    card), `warnings`, `suggestions` (computed provenance, suggested typed links) and
    `recent_changes` (the last 5 change-log rows, with batch ids for undo)."""
    try:
        return cards.explain_card(card)
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_bulk_edit(op: str, items: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Run ONE setter over many cards. DRY-RUN BY DEFAULT: pass dry_run=false to write. V2 cards 6.

    op (allow-listed): set_status, set_kind, set_whereabouts, set_card_provenance, set_home,
    set_card_highlight, add_to_hobby, remove_from_hobby, add_to_family, remove_from_family, nest,
    unnest, link, unlink, retype_link, set_hobby_activity.
    items: [{card, args: {...}}] where `card` is the subject and args are that setter's other
    arguments, e.g. op='set_status' -> {card: 'x', args: {stage: 'done'}}; op='add_to_hobby' ->
    {card: 'x', args: {hobby: '3d-printing'}}; op='add_to_family' -> {card: <member>, args:
    {family: <family>}}; op='nest' -> {card: <child>, args: {parent: <parent>}}; op='link' ->
    {card: a, args: {b, type, note?}}; op='set_hobby_activity' -> {card: <hobby slug>, args:
    {value: 'inactive'}}. Unknown arguments are refused.

    Items run in order inside one transaction (later ones see earlier ones). Every item is
    validated and reported with its before/after. All-or-nothing: if ANY item fails nothing is
    written, unless partial_ok=true, which applies the valid ones and reports the rest. One
    batch_id: constructicon_undo(batch_id) reverses the lot.
    Returns {ok, dry_run, op, applied, changes, warnings, batch_id, items: [{card, ok, applied,
    changes, warnings, error?}]}."""
    try:
        return cards.bulk(op, items, dry_run=dry_run, partial_ok=partial_ok, actor="mcp")
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_resolve_decisions(items: list, accept_suggested: bool = False, dry_run: bool = True,
                                    partial_ok: bool = False) -> dict:
    """Clear many card questions (card_status / card_built_for / card_kind / card_family_members)
    at once. DRY-RUN BY DEFAULT: pass dry_run=false to write. V2 cards 7.3.

    items: [{decision_id, choice}] (or {decision_id, choices: [..]} for multi-answer questions);
    with accept_suggested=true, just a list of decision ids: each is answered with ITS OWN
    suggested option, and a question with no suggestion is skipped (reported, not an error).
    Find ids and suggestions with constructicon_list_needs_decision. Each answer runs through the
    same validators as a manual edit. One transaction, one batch_id (undoable): if a decision can't
    be applied nothing is written, unless partial_ok=true.
    Returns {ok, dry_run, batch_id, applied, would_apply, skipped, failed, changes, warnings,
    items: [{decision_id, card_slug, choice, status: applied|would_apply|skipped|failed|rolled_back,
    changes, reason?, error?}]}."""
    try:
        return cards.resolve_decisions(items, accept_suggested=accept_suggested, dry_run=dry_run,
                                       partial_ok=partial_ok, actor="mcp")
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_undo(audit_id: str | int, force: bool = False, dry_run: bool = False) -> dict:
    """Reverse a change-log entry, or a whole batch. V2 cards 3.13.

    audit_id: one change-log row id (from constructicon_list_changes), or a batch_id (every row
    of that operation or bulk call, reversed newest-first in one transaction). Writes its own
    log row, so an undo can itself be undone. REFUSES, writing nothing, with
    {"ok": false, "error": {code}}: undo_conflict (a row it would restore was changed since;
    the message names the field and values; force=true overrides), undo_refused (a migration row,
    an entry that was already undone, or one that recorded no row images), not_found.
    dry_run=true reports what would be reversed. Returns {ok, dry_run, changes, warnings,
    batch_id, undone: [audit ids], undone_ops}."""
    try:
        return cards.undo(audit_id, force=force, dry_run=dry_run, actor="mcp").to_dict()
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_list_changes(card: str | int | None = None, batch_id: str | None = None, limit: int = 50) -> list[dict]:
    """The change log, newest first: [{id, op, actor, batch_id, timestamp, affected_slugs,
    undone_by, mutations}]. Every core write is recorded with row images (table, key, before,
    after). `card` narrows to one card's slug/id; `batch_id` to one operation. Feed an `id` or a
    `batch_id` to constructicon_undo."""
    return cards.list_changes(card=card, batch_id=batch_id, limit=limit)




# --- Curator Stage 3a: Nudges ---

@mcp.tool()
def constructicon_list_needs(kind: str | None = None, limit: int | None = None) -> list[dict]:
    """Curator Stage 3a: list current nudges (actionable needs).

    Returns a ranked list of nudges, sorted by priority DESC. Each nudge includes:
        - nudge_key: stable id for dismissal
        - kind: nudge type (missing_cover, unfiled_objects, confirm_automatch, etc.)
        - target_type: 'project' or 'global'
        - target_id, target_slug: project id/slug or None for global nudges
        - title, summary: human-readable text
        - priority: computed score (base_impact * status_weight)
        - base_impact, status_weight: components of priority
        - action: descriptor dict (type, project_slug, etc.) for the UI/agent to interpret

    Optional filters:
        - kind: filter by nudge kind (e.g., 'missing_cover', 'unfiled_objects')
        - limit: cap the result count (default: all)

    Dismissed nudges are excluded automatically. Deferred ones stay in the list with
    deferred=true. For the whole picture (questions + nudges + needs grouped by card, with
    deferred items split out) use constructicon_curation_queue.
    """
    needs = curator_needs.list_needs()

    # Filter by kind if requested
    if kind is not None:
        needs = [n for n in needs if n["kind"] == kind]

    # Limit if requested
    if limit is not None:
        needs = needs[:limit]

    return needs


@mcp.tool()
def constructicon_dismiss_need(nudge_key: str) -> dict:
    """Dismiss a nudge or computed need for good. (The timed snooze is gone: to set
    something aside without losing it, use constructicon_defer.)

    nudge_key: the `key` of a nudge/need from constructicon_curation_queue (or the
        nudge_key from constructicon_list_needs). A question ("decision:<id>") can't be
        dismissed: answer it or defer it.

    Returns {ok: true, changed}. Recorded in the change log, so it can be undone."""
    try:
        return curation_queue.dismiss(nudge_key, actor="mcp")
    except curation_queue.QueueError as e:
        return {"error": {"code": "bad_request", "message": str(e)}}


@mcp.tool()
def constructicon_curation_queue(card: str | None = None, include_deferred: bool = True) -> dict:
    """The Curator queue: every open question, nudge and need in ONE list, grouped by card
    (the same thing the owner sees in the Curator tab).

    Returns {groups, deferred, counts}. Each group is one card (type 'card': slug, title,
    card_kind, activity, last_touched) or a non-card bucket (type 'hobby' = one hobby's
    flags, 'uploads' = file-level questions about fresh uploads, 'collection' = whole-
    collection nudges like unfiled objects). Order: Active cards first, then most recently
    touched; then hobbies, uploads, collection. Inside a group: questions first (lowest
    confidence first), then nudges, then needs.

    Each item: {type: question|nudge|need, key, label, kind, group, href, deferred,
    dismissible} plus, for a question, {id, options, suggested: {picks, labels, reason},
    confidence, multi}. `group` is the details-panel group that fixes it (the page
    /project/<slug>?edit=<group>). Answer a question with constructicon_resolve_pending_decision
    (id, choice) or constructicon_resolve_decisions (accept_suggested). Set something aside with
    constructicon_defer (no timer); bring it back with constructicon_bring_back; dismiss a
    nudge/need for good with constructicon_dismiss_need. counts.open is the number on the
    Curator tab's badge (open, non-deferred items).

    card: limit to one card's slice (slug). include_deferred=false drops the Deferred section."""
    q = curation_queue.build_queue(card=card)
    if not include_deferred:
        q = {**q, "deferred": []}
    return q


@mcp.tool()
def constructicon_defer(key: str) -> dict:
    """Defer a queue item (a question, nudge or need) from constructicon_curation_queue.
    It moves to the trailing Deferred section, with no timer, and stays there until it
    is answered or brought back (constructicon_bring_back). Recorded in the change log.

    key: the item's `key` ("decision:<id>", "<kind>:project:<id>", "need:<need>:<slug>")."""
    try:
        return curation_queue.defer(key, actor="mcp")
    except curation_queue.QueueError as e:
        return {"error": {"code": "bad_request", "message": str(e)}}


@mcp.tool()
def constructicon_bring_back(key: str) -> dict:
    """Bring a deferred queue item back into the main queue (undo constructicon_defer).
    key: the item's `key`. A dismissed nudge is not brought back by this."""
    try:
        return curation_queue.bring_back(key, actor="mcp")
    except curation_queue.QueueError as e:
        return {"error": {"code": "bad_request", "message": str(e)}}


@mcp.tool()
def constructicon_list_pending_decisions() -> list[dict]:
    """#240/#446/#448: List all open pending decisions, with automatic stale-cleanup.

    The "Needs your input" queue surfaces two decision kinds:
    - project_match: an upload matched multiple project titles (pick which to attach to)
    - retype: a file's type was deferred (pick the actual media type)

    Returns a list of dicts, each with:
        {
            "id": int (decision id),
            "kind": "project_match" | "retype",
            "slug": str (capture_events.slug),
            "title": str (object display name),
            "media_type": str (current media_type),
            "created_at": float (UTC unix seconds),
            "question": str (retype only),
            "options": [{"key": str, "label": str, ...}] (retype only),
            "candidates": [{"id": int, "title": str, "slug": str}] (project_match only),
        }

    Decisions become stale and are automatically resolved if:
    - The object (post_slug) has been deleted
    - A project_match has fewer than 2 candidates remaining

    For a retype question, answer with constructicon_resolve_pending_decision
    and one option's key as `choice`; the option whose key equals the item's
    current media_type means "keep it as it is".
    """
    result = []
    for item in decisions.list_open():
        if cards.is_card_decision_slug(item["post_slug"]):
            # V2 card question: slug is "card:<project slug>"; carries the suggested answer.
            summary = cards.decision_summary({"id": item["id"], "kind": item["kind"],
                                              "post_slug": item["post_slug"], "payload": item["payload"]})
            result.append({
                "id": item["id"],
                "kind": item["kind"],
                "slug": item["post_slug"],
                "card_slug": summary["card_slug"],
                "title": summary["title"],
                "media_type": None,
                "created_at": item["created_at"],
                "question": summary["question"],
                "legacy_status": summary["legacy_status"],
                "provisional": summary["provisional"],
                "suggested": summary["suggested"],
                "suggested_reason": summary["suggested_reason"],
                "confidence": summary["confidence"],
                "options": summary["options"],
            })
            continue
        row = item["row"]
        # Use the same title logic as the web endpoint
        title = row.get("display_name") or row.get("filename") or row.get("content_description") or row["slug"]

        entry = {
            "id": item["id"],
            "kind": item["kind"],
            "slug": item["post_slug"],
            "title": title,
            "media_type": row.get("media_type"),
            "created_at": item["created_at"],
        }

        if item["kind"] == "project_match":
            entry["candidates"] = item.get("candidates", [])
        elif item["kind"] == "retype":
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
        elif item["kind"] == revisions.KIND_ITEM_SUPERSEDES:
            # #477: options are the earlier files this upload might replace (key = their slug) + "none".
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
            entry["suggested"] = item.get("suggested")

        result.append(entry)

    return result


@mcp.tool()
def constructicon_mark_superseded(old: str, new: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Revision tracking (#477): file `new` supersedes file `old` ("rev C replaces rev B"), so `old`
    shows "Superseded, see <current>" and drops out of browse listings (it stays findable by search
    and one click away). Both are item slugs. A chain is linear: A -> B -> C, and the CURRENT revision
    is the one with nothing newer. This is the explicit tool; the upload-time "does this replace ...?"
    question is only ever answered by the owner (constructicon_resolve_pending_decision, kind
    item_supersedes, choice = the older file's slug or "none").

    Errors ({"ok": false, "error": {code, message}}): not_found; bad_revision (self-link, or a
    redacted item); revision_cycle (new is already at or before old in the chain);
    revision_conflict (old already has a newer revision, or new already replaces another item).
    dry_run=true validates only. One change-log entry: constructicon_undo(batch_id) reverses it.
    Returns {ok, dry_run, batch_id, old, new, chain: [slugs, oldest first]}."""
    try:
        return revisions.mark_superseded(old, new, actor="mcp", batch_id=batch_id, dry_run=dry_run)
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_remove_from_revisions(slug: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Take a file out of its revision chain (#477) and close the gap: A -> B -> C minus B is
    A -> C; minus the oldest, B -> C stands alone; minus the current one, the previous revision
    becomes current. A no-op ({"removed": false}) for a file in no chain. Undoable with
    constructicon_undo(batch_id). Returns {ok, dry_run, batch_id, slug, removed, chain}."""
    try:
        return revisions.remove_from_chain(slug, actor="mcp", batch_id=batch_id, dry_run=dry_run)
    except card_rules.CardError as e:
        return _card_error_result(e)


@mcp.tool()
def constructicon_list_revisions(slug: str) -> dict:
    """The revision chain a file belongs to (#477), oldest first: [{slug, title, filename, rev,
    is_current, is_this, redacted}], plus `current` (the newest revision), `superseded` (is this
    file an older one) and this file's `rev` of `of`. {"in_chain": false} for a file with no revisions."""
    if db.get_by_slug(slug) is None:
        return {"ok": False, "error": {"code": "not_found", "message": f"No item {slug!r}."}}
    return {"ok": True, **revisions.revision_view(slug)}


@mcp.tool()
def constructicon_resolve_pending_decision(decision_id: int, choice: str = "", project_ids: list[int] | None = None,
                                           choices: list[str] | None = None) -> dict:
    """#240/#446/#448: Resolve a pending decision with the owner's choice.

    For project_match decisions:
        project_ids: list of project IDs to attach to (may be empty for "none of these")
        choice: unused

    For retype decisions:
        choice: the media_type key to retype to (or empty to skip)
        project_ids: unused

    For V2 card decisions (kind card_status / card_built_for / card_kind, slug
    "card:<project slug>"):
        choice: one option key from constructicon_list_pending_decisions (e.g. "in_use")
        choices: several option keys for multi-answer questions (card_built_for,
        card_family_members). A family answer turns the card into a family, adds the
        picked cards as members and takes any picked nested child out of the nesting
        (all in one logged batch); "none" leaves everything as it is.
        The option's change runs through the same validators as a manual edit; if it
        is invalid today the decision stays open and {"ok": false, "error": {code,
        message}} comes back. card_built_for answers that create a link (a candidate
        card) aren't available until typed links land (piece 4); "is_event" and
        "none" work now.

    Returns:
        {"ok": true, "applied": [...], "remaining": count}
        where "applied" is the list of actions taken (project ids or the choice)

    On error:
        {"error": "reason"} with one of:
        - "No such pending decision"
        - "Already resolved"
        - "Unknown decision kind"
    """
    if project_ids is None:
        project_ids = []

    try:
        result = decisions.resolve(decision_id, choice=choice, project_ids=project_ids, choices=choices or (),
                                   actor="mcp")
        return result
    except card_rules.CardError as e:
        return _card_error_result(e)
    except decisions.DecisionNotFound as e:
        return {"error": str(e)}
    except decisions.DecisionAlreadyResolved as e:
        return {"error": str(e)}
    except (decisions.UnknownDecisionKind, decisions.InvalidChoice) as e:
        return {"error": str(e)}


@mcp.tool()
def constructicon_run_type_action(slug: str, action: str) -> dict:
    """#448: Run a per-type action on an object. Actions are declared per type
    (ObjectTypeSpec.actions). See constructicon_get for the available actions
    on a specific object.

    slug: object slug
    action: action key (e.g. "fetch_youtube_metadata")

    Returns {ok: true, action: key, item: {...}} with the updated object.
    On error: {error: "reason"}.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return {"error": "not found"}

    if row.get("redacted"):
        return {"error": "object is redacted"}

    spec = object_types.get_object_type(row.get("media_type"))
    action_obj = next((a for a in spec.actions if a.key == action), None)
    if action_obj is None:
        return {"error": f"{spec.label} has no action '{action}'"}
    if not action_obj.applies_to(row):  # #446: e.g. Reclassify is .exe-only
        return {"error": f"{spec.label} has no action '{action}' for this item"}

    try:
        result = action_obj.handler(row)
    except Exception as e:
        print(f"Action '{action}' failed: {e!r}", flush=True)
        return {"error": f"Action '{action}' failed: {e}"}

    updated_row = db.get_by_slug(slug)
    return {
        "ok": True,
        "action": action,
        **(result or {}),
        "item": _to_public(updated_row)
    }


if __name__ == "__main__":
    db.init_db()
    # Self-heal: this process (or the web one) may have been killed while OCR
    # was still queued/running for a row, leaving it stuck at "pending"
    # forever otherwise — shared DB, so either process catches the other's.
    # Fired as background threads, not run here directly, so a pile-up of
    # stuck rows can't block this process from ever reaching mcp.run().
    # The periodic watchdog for rows that go stale while this process stays
    # up lives in web/app.py — same shared DB, no need to duplicate it here.
    stuck = db.list_pending_ocr()
    if stuck:
        print(f"re-running OCR for {len(stuck)} row(s) left pending by a prior process")
        for row in stuck:
            db.set_ocr_status(row["slug"], "pending")
            threading.Thread(target=ocr.run_ocr, args=(row["slug"],), daemon=True).start()
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8100, stateless_http=True)
