"""Card routes (#547): /api/projects/*, /api/project/*, /api/cards/*, /api/links*,
/api/families/*, /api/changes/*."""

import logging
from datetime import datetime

from fastapi import Request, Form, HTTPException
from fastapi.responses import JSONResponse, Response

from core import besteffort, card_rules, cards, changes, db, hobbies, membership, timeline
from core.errors import NotFound
from web.shapes import _to_card_face, _to_project_option
from core import roles
from web.roles import RoleRouter, requires

log = logging.getLogger("constructicon.web")

router = RoleRouter(default_role=roles.EDITOR)  # #557: routes without their own label are editor


@router.get("/api/cards/{slug}", dependencies=requires(roles.VIEWER))
def api_card(slug: str):
    """The card face for one card (V2 piece 7, 8.1): zone content as JSON, plus the cover URL."""
    project = db.get_project(slug)
    if project is None:
        raise NotFound(f"No card '{slug}'.")  # #548: the shared error shape (was a hand-built 404 without `detail`)
    return JSONResponse(_to_card_face(project))


@router.get("/api/projects", dependencies=requires(roles.VIEWER))
def api_projects(request: Request):
    """Populates the upload drawer's Project dropdown (#1) — every project,
    most-recently-updated first, same ordering list_projects() already uses
    for the home page's Projects column."""
    return JSONResponse([_to_project_option(p) for p in db.list_projects()])


# (#423) The per-project auto write-up now lives in db.create_project (as
# db._make_project_writeup), so every create path — these HTTP routes, the
# constructicon_create_project MCP tool, and seed scripts — gets one and none
# can silently skip it. The old web-only _create_writeup_for_project helper
# (and its manual calls in the routes below) was removed as redundant.


@router.post("/api/projects")
def api_create_project(request: Request, title: str = Form(...), parent_id: str = Form(None),
                       kind: str = Form(None), stage: str = Form(None), stop_reason: str = Form(None)):
    """Creates a project from the upload drawer's "+ New project..." flow
    (#1) — distinct from scripts/seed_example_projects.py's one-off seeding,
    this is the first real UI-driven way to make a project.

    Also creates (or reuses) a root-level blog_tags row with the same name
    and links it via projects.tag_id, so every object later tagged to this
    project also becomes reachable through the ordinary tag-based browsing
    the rest of the site already has (see core/db.py's create_project
    docstring and README's "tied to the site tags" note) — not a parallel
    system, just handing the existing tag tree a project-shaped entry point.

    parent_id (#133) optionally sets this project as a child of another project.

    Also auto-creates a document-type capture_event (#156) as the project's
    write-up, adds it to project_items, and sets the project's writeup_slug
    to that document's slug.
    """
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name can't be empty")

    parent_id_int = None
    if parent_id:
        try:
            parent_id_int = int(parent_id)
            if db.get_project(parent_id_int) is None:
                raise HTTPException(status_code=400, detail="Parent project not found")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid parent_id")

    # #541 phase D: core validates kind/stage/nest before anything is written, and the tag, the
    # card and its write-up are ONE undoable batch.
    result = cards.create(title, parent=parent_id_int, kind=kind or None, stage=stage or None,
                          stop_reason=stop_reason or None)
    return JSONResponse(_to_project_option(result.data["card"]))


@router.post("/api/projects/from-selection")
def api_create_project_from_selection(slugs: list[str] = Form(...), title: str = Form(...)):
    """Issue #98: "turn this selection into a project" — the Unfiled page's
    bulk-tag follow-up prompt, and usable standalone. Deliberately operates
    on the exact slugs passed in, not a tag-name lookup: this app has two
    separate, unlinked tag systems (the flat per-object `tags` array bulk
    tagging above writes to, vs. the hierarchical blog_tags/post_tags tree
    /api/tags and list_posts_for_tag walk) — a tag-name lookup here would
    silently miss items that were only ever bulk-tagged the flat way.

    Registered ahead of /api/projects/{project_id} below on purpose — FastAPI
    matches routes in registration order, and that dynamic path param route
    would otherwise swallow this literal path (project_id="from-selection"),
    404ing instead of ever reaching this handler. Same for from-related."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name can't be empty")
    # #541 phase D: tag + card + write-up + the files (all side effects, as before) = one batch.
    batch_id = changes.new_batch_id()
    with db.transaction():
        project = cards.create(title, batch_id=batch_id).data["card"]
        membership.add_files(project["id"], slugs, missing_ok=True, batch_id=batch_id, **membership.UI_EFFECTS)
    return JSONResponse(_to_project_option(project))


@router.post("/api/projects/from-related")
def api_create_project_from_related(slug: str = Form(...), title: str = Form(...)):
    """Issue #98: "turn this object + its related items into a project" —
    surfaced on the object detail page's Related panel. See from-selection
    above for why this is registered ahead of /api/projects/{project_id}."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name can't be empty")
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    batch_id = changes.new_batch_id()
    with db.transaction():
        project = cards.create(title, batch_id=batch_id).data["card"]
        membership.add_files(project["id"], [slug] + [r["slug"] for r in db.list_related(slug)],
                             missing_ok=True, batch_id=batch_id, **membership.UI_EFFECTS)
    return JSONResponse(_to_project_option(project))


@router.post("/api/projects/{project_id}")
async def api_update_project(
    request: Request,
    project_id: str,
    title: str = Form(None),
    description: str = Form(None),
    cover_slug: str = Form(None),
    status: str = Form(None),
    kind: str = Form(None),
    stage: str = Form(None),
    stop_reason: str = Form(None),
    activity: str = Form(None),
    writeup_slug: str = Form(None),
    start_date: str = Form(None),
    reset_start_date: bool = Form(False),
    end_date: str = Form(None),
    reset_end_date: bool = Form(False),
):
    """Updates a project's properties (issue #103). Allows setting any
    combination of title, description, cover_slug (slug of an attached item
    to use as the cover image), and status. Only overwrites fields that were
    passed; omitted fields are left unchanged. Returns the updated project
    in _to_project_option shape (same as the list endpoint).

    parent_id (#133) can be set to create/remove a parent-child relationship.
    Pass empty string to remove a parent, or a project ID to set one.

    cover_project_id (#356) sets the cover by borrowing from a child project,
    mutually exclusive with cover_slug. Pass empty string to clear.

    writeup_slug (#156) can be set to point to a document-type item as the
    project's write-up. Pass empty string to remove a writeup."""
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # (#367): Distinguish "parent_id field not submitted" from "parent_id
    # submitted empty (clear parent)". We CANNOT use `parent_id: str = Form(None)`
    # for this: FastAPI delivers an empty-string form field as None, colliding
    # with "field absent". So read the raw form and key on PRESENCE.
    #   - key absent            -> leave parent_id unchanged (sentinel ...)
    #   - key present, empty     -> clear parent (None)
    #   - key present, a value   -> validate + set
    _form = await request.form()
    parent_id_value = ...  # "..." means don't update parent_id
    if "parent_id" in _form:
        raw_parent = (str(_form.get("parent_id")) or "").strip()
        if raw_parent == "":
            parent_id_value = None  # explicit clear
        else:
            try:
                parent_id_int = int(raw_parent)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid parent_id")
            # Self / missing parent / cycle / group kind / second parent are all
            # refused by core.cards.nest below (CardError -> 409/404 with a code).
            parent_id_value = parent_id_int
    replace_parent = str(_form.get("replace") or "").strip().lower() in ("1", "true", "yes", "on")
    # writeup_slug (#156): absent -> leave; "" -> clear; a slug -> core validates it (exists, can be a
    # write-up). cover_project_id (#356): same raw-form presence logic as parent_id; core checks the
    # child exists and is a child of this card. Both refusals are 400s with the old messages.
    writeup_slug_value = ...
    if writeup_slug is not None:
        writeup_slug_value = writeup_slug if writeup_slug else None
    cover_project_id_value = ...
    if "cover_project_id" in _form:
        raw_cover_project = (str(_form.get("cover_project_id")) or "").strip()
        if raw_cover_project == "":
            cover_project_id_value = None  # explicit clear
        else:
            try:
                cover_project_id_value = int(raw_cover_project)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid cover_project_id")

    # #563: parse the dates up front too (a bad value is a 400, not a 500 after the writes).
    new_start = new_end = ...
    if reset_start_date or reset_end_date or start_date or end_date:
        try:
            new_start = None if reset_start_date else (timeline.source_datetime_to_epoch(datetime.fromisoformat(start_date)) if start_date else ...)
            new_end = None if reset_end_date else (timeline.source_datetime_to_epoch(datetime.fromisoformat(end_date)) if end_date else ...)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date / end_date")

    # #563: every check that can fail with a 400 runs above; the writes below share ONE
    # transaction (no awaits inside it), so a refusal part-way (e.g. a CardError from the stage
    # rules) rolls the whole request back instead of leaving the card nested but not updated.
    # #541 phase D: the whole Save is ONE batch, so one undo reverses all of it.
    card_warnings = []
    batch_id = changes.new_batch_id()
    with db.transaction():
        # Title / description / cover / write-up / timeline overrides: one imaged change-log row
        # (op update_card). First, so its 400s (bad write-up or cover card) come before the
        # nest/status refusals, as they did. The reset_* flags win over a date sent alongside.
        cards.update(project["id"], title=title, description=description, cover_slug=cover_slug,
                     writeup_slug=writeup_slug_value, cover_project_id=cover_project_id_value,
                     start=new_start, end=new_end, batch_id=batch_id)
        # "Part of" (V2 cards 3.7) goes through core.cards.
        if parent_id_value is None:
            cards.unnest(project["id"], batch_id=batch_id)
            parent_id_value = ...
        elif parent_id_value is not ...:
            card_warnings.extend(cards.nest(project["id"], parent_id_value, replace=replace_parent,
                                            batch_id=batch_id).warnings)
            parent_id_value = ...

        # V2 cards: kind / stage / stop_reason / activity go through core.cards (the same
        # validators the MCP uses; a violation is a CardError -> HTTP 422). The legacy
        # `status` word is translated to a stage; the legacy column itself is frozen.
        if status:
            legacy = card_rules.legacy_to_status(status)
            if legacy["kind"] and not kind:
                kind = legacy["kind"]
            if not stage:
                stage, stop_reason = legacy["stage"], legacy["stop_reason"]
            card_warnings.extend(legacy["warnings"])
        if kind:
            card_warnings.extend(cards.set_kind(project["id"], kind, batch_id=batch_id).warnings)
        if stage or activity or stop_reason:
            card_warnings.extend(cards.set_status(project["id"], stage, stop_reason or None,
                                                  activity=activity or None, batch_id=batch_id).warnings)

        # Re-read so kind/stage edits made above through core.cards show in the response.
        updated = {**(db.get_project(project["id"]) or {}), "batch_id": batch_id}
        if card_warnings:
            updated = {**updated, "warnings": card_warnings}
    return JSONResponse(updated)


@router.post("/api/projects/{project_id}/orphan-child")
def api_orphan_child(project_id: str, child_id: str = Form(...)):
    """Orphan a child project: set its parent_id to NULL.

    The child becomes top-level. Returns the updated child project."""
    child = db.get_project(child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="Child project not found")

    # Resolve parent_id: if it's a digit, use it; otherwise resolve the slug.
    parent_resolved = db.get_project(project_id)
    if parent_resolved is None:
        raise HTTPException(status_code=404, detail="Parent project not found")
    parent_id = parent_resolved["id"]

    # Verify the child is actually a child of this project.
    if child.get("parent_id") != parent_id:
        raise HTTPException(status_code=400, detail="Child is not a child of this project")

    cards.unnest(child["id"])
    return JSONResponse(db.get_project(child["id"]) or {})


@router.post("/api/projects/{project_id}/whereabouts")
def api_set_whereabouts(project_id: str, whereabouts: str = Form(""), note: str | None = Form(None)):
    """V2 cards 3.4: set (blank clears) where the physical thing is now, plus an
    optional note (omit `note` to leave it alone). Rule violations are CardErrors:
    422 bad_whereabouts (wrong kind, unknown value, in_use / never_built cross-rules)."""
    result = cards.set_whereabouts(project_id, whereabouts or None, note if note is not None else ...)
    return JSONResponse({**result.to_dict(), **cards.whereabouts_fields(db.get_project(project_id))})


@router.post("/api/projects/{project_id}/provenance")
def api_set_card_provenance(project_id: str, provenance: str = Form(""), credit: str | None = Form(None)):
    """V2 cards 3.5: set (blank clears) the CARD's provenance and optionally its credit
    (omit `credit` to leave it alone). 422 bad_provenance on an unknown value.
    Per-file provenance (/api/image/{slug}) is a separate field and untouched."""
    result = cards.set_provenance(project_id, provenance or None, credit if credit is not None else ...)
    return JSONResponse({**result.to_dict(), **cards.whereabouts_fields(db.get_project(project_id))})


@router.post("/api/projects/{project_id}/highlight")
def api_set_card_highlight(project_id: str, on: str = Form("0")):
    """V2 cards 3.12: the card's own highlight flag (independent of file highlights)."""
    result = cards.set_highlight(project_id, on.strip().lower() in ("1", "true", "on", "yes"))
    return JSONResponse({**result.to_dict(), **cards.whereabouts_fields(db.get_project(project_id))})


@router.post("/api/projects/{project_id}/home")
def api_set_card_home(project_id: str, target: str = Form("")):
    """V2 cards 3.10: override the card's home ('card:<slug>' / 'hobby:<slug>'); blank
    clears the override (back to automatic: parent, first family, first hobby). 422 bad_home."""
    result = cards.set_home(project_id, target.strip() or None)
    return JSONResponse({**result.to_dict(), "home": cards.resolve_home(project_id)})


@router.post("/api/projects/{project_id}/nest")
def api_nest_card(project_id: str, parent: str = Form(...), replace: str = Form("0")):
    """V2 cards 3.7: make this card part of `parent` (id or slug). CardErrors: 409 nest_*."""
    result = cards.nest(project_id, parent, replace=replace.strip().lower() in ("1", "true", "on", "yes"))
    return JSONResponse(result.to_dict())


@router.post("/api/projects/{project_id}/unnest")
def api_unnest_card(project_id: str):
    """V2 cards 3.7: take this card out of its parent (no-op if it has none)."""
    return JSONResponse(cards.unnest(project_id).to_dict())


@router.get("/api/projects/{project_id}/explain", dependencies=requires(roles.VIEWER))
def api_explain_card(project_id: str):
    """V2 cards 6: everything about a card in one JSON object (same as constructicon_explain_card)."""
    return JSONResponse(cards.explain_card(project_id))


@router.post("/api/changes/{batch_id}/undo")
def api_undo_change(batch_id: str, force: str = Form("0")):
    """V2 cards 3.13: undo a change-log row (numeric id) or batch. 409 undo_conflict when a
    row changed since; 422 undo_refused for migration rows / already-undone entries."""
    result = cards.undo(batch_id, force=force.strip().lower() in ("1", "true", "on", "yes"))
    return JSONResponse(result.to_dict())


@router.post("/api/families/{family_id}/members/add")
def api_family_add_member(family_id: str, member: str = Form(...)):
    """V2 cards 3.6: put `member` (card id or slug) in a family or collection.
    Many-to-many; adding twice is a no-op. Rule violations are CardErrors
    (422 bad_membership). Not nesting: no file moves."""
    result = cards.add_to_family(family_id, member)
    return JSONResponse(result.to_dict())


@router.post("/api/families/{family_id}/members/remove")
def api_family_remove_member(family_id: str, member: str = Form(...)):
    """V2 cards 3.6: take `member` out of a family or collection (no-op if absent)."""
    result = cards.remove_from_family(family_id, member)
    return JSONResponse(result.to_dict())


@router.post("/api/projects/{project_id}/remove-item")
def api_remove_item_from_project(project_id: str, slug: str = Form(...)):
    """Remove an object from a project (the project page's x).

    The object is detached but not deleted; its tags and the card's cover are left alone.
    #541 phase C: membership.remove_files, undoable. Returns {success: true, batch_id}."""
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Verify the object exists.
    obj = db.get_by_slug(slug)
    if obj is None:
        raise HTTPException(status_code=404, detail="Object not found")

    result = membership.remove_files(project["id"], [slug])
    return JSONResponse({"success": True, "batch_id": result.batch_id})


@router.post("/api/projects/{project_id}/delete")
def api_delete_project(project_id: str):
    """Delete a project without cascade.

    Orphans all child projects, detaches all objects, removes from hobbies,
    and deletes the project row. Nothing else is deleted. Returns a summary
    of what was orphaned/detached."""
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Core does the work (#497): one transaction, change-log row images (undoable via
    # constructicon_undo(batch_id)), no ghost write-up / links / family rows left behind.
    result = cards.delete_card(project["id"])
    return JSONResponse({**result.to_dict(), **result.data})


@router.get("/api/projects/{project_id}/export.zip", dependencies=requires(roles.VIEWER))
def api_export_project(project_id: str):
    """Export a project as a zip file containing the manifest and all uploaded
    files. The zip contains:
    - manifest.json: Project metadata and item descriptions
    - files/: Directory with uploaded files for each item

    Useful for offline review or agent analysis without repeated API calls."""
    try:
        from core import project_export
        zip_bytes = project_export.export_project(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    # Get project name for the download filename
    try:
        project = db.get_project(project_id)
        filename = f"{project['slug']}-export.zip" if project else "project-export.zip"
    except Exception as e:
        besteffort.warn(log, "project export: couldn't look up the card for the download filename", e,
                        project_id=project_id)
        filename = "project-export.zip"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _related_projects_public(slug):
    """Trim shape for the project-detail Related-projects widget (#408)."""
    return [
        {"slug": p["slug"], "title": p["title"], "status": card_rules.curator_status(p)}
        for p in db.list_related_projects(slug)
    ]


@router.post("/api/project/{slug}/related")
def api_add_project_related(request: Request, slug: str, related_slug: str = Form(...)):
    """#408: link two projects as peers (bidirectional). Kept for existing callers;
    since V2 3.8 it creates a `related` link through core.cards.link. Adding a pair
    that is already related is a no-op; a pair that already has a typed link is
    refused (409 link_conflict), as is a self-link (422 bad_link)."""
    if db.get_project(slug) is None:
        raise HTTPException(status_code=404, detail="project not found")
    if db.get_project(related_slug) is None:
        raise HTTPException(status_code=404, detail="related project not found")
    try:
        cards.link(slug, related_slug, "related")
    except card_rules.CardError as e:
        if not (e.code == "link_conflict" and e.details.get("reason") == "duplicate"):
            raise
    return JSONResponse(_related_projects_public(slug))


@router.post("/api/project/{slug}/related/remove")
def api_remove_project_related(request: Request, slug: str, related_slug: str = Form(...)):
    if db.get_project(slug) is not None and db.get_project(related_slug) is not None:
        cards.unlink(slug, related_slug, "related")
    return JSONResponse(_related_projects_public(slug))


# --- Typed links (V2 cards 3.8). Rule violations are CardErrors: 422 bad_link,
# 409 link_conflict, 404 not_found -- the same codes the MCP tools return. ---

@router.get("/api/project/{slug}/links", dependencies=requires(roles.VIEWER))
def api_project_links(slug: str):
    """Every link on a card, both directions, with direction + label."""
    return JSONResponse(cards.list_links(slug))


@router.post("/api/links")
def api_link(a: str = Form(...), b: str = Form(...), type: str = Form(...), note: str = Form("")):
    """"a <type> b". Directed types store one row, `related` two. A typed link over a
    related pair upgrades it; related over a typed pair is refused (link_conflict)."""
    result = cards.link(a, b, type, note)
    return JSONResponse({**result.to_dict(), "links": cards.list_links(a)})


@router.post("/api/links/remove")
def api_unlink(a: str = Form(...), b: str = Form(...), type: str = Form("")):
    """Removes the `type` link between a and b (all links on the pair when `type` is blank)."""
    result = cards.unlink(a, b, type or None)
    return JSONResponse({**result.to_dict(), "links": cards.list_links(a)})


@router.post("/api/links/retype")
def api_retype_link(a: str = Form(...), b: str = Form(...), from_type: str = Form(...), to_type: str = Form(...)):
    """Replaces the `from_type` link on the pair with "a <to_type> b" in one transaction."""
    result = cards.retype_link(a, b, from_type, to_type)
    return JSONResponse({**result.to_dict(), "links": cards.list_links(a)})


@router.post("/api/project/{slug}/convert-to-hobby", dependencies=requires(roles.ADMIN))
def api_convert_project_to_hobby(request: Request, slug: str):
    """Convert an existing project into a hobby (DESTRUCTIVE, but undoable since #541 phase D).

    The project is converted into a hobby tag, its children are moved to the hobby,
    its items are tagged with the hobby, and the project row is deleted (core/hobbies.py
    convert_from_card: one batch; POST /api/changes/{batch_id}/undo restores the card).

    Returns the new hobby tag dict with summary info about what was moved, plus batch_id."""
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    result = hobbies.convert_from_card(project["id"])
    hobby = result.data["hobby"]
    return JSONResponse({
        "id": hobby["id"],
        "name": hobby["name"],
        "slug": hobby["slug"],
        "status": hobby["status"],
        "summary": {
            "children_moved": result.data["children_moved"],
            "items_moved": result.data["items_moved"],
        },
        "batch_id": result.batch_id,
    })
