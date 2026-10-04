"""Item routes (#547): upload, content, processing, /api/image/*, per-item captions,
gallery, clients, bulk edits, tags, search, multi-delete."""

import json
import time
from datetime import datetime

from fastapi import Request, Form, UploadFile, File, HTTPException, BackgroundTasks, APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core import captions, db, ingest, object_types, ocr, revisions, similarity, storage, thumbnails, timeline
from core import physical_piece
from web.common import DESKTOP_APP_CLIENT_HEADER, DESKTOP_APP_CLIENT_VALUE
from web.shapes import _friendly_datetime, _to_project_option, _to_public

router = APIRouter()


@router.post("/api/delete")
def api_delete_selected(slugs: list[str] = Form(...)):
    """Selective delete (#19) — remove just the given objects, any mix of
    sources (uploaded images, youtube rows, etc.), without touching tags
    or projects. That global wipe is specific to /api/delete-all's
    full-reset button; this is the day-to-day 'clear this test content'
    path, driven by gallery checkboxes or a single object's detail page.
    Reuses the same per-row deletion primitives as /api/delete-all."""
    deleted = 0
    for slug in slugs:
        row = db.get_by_slug(slug)
        if row is None:
            continue
        if row.get("stored_filename"):
            storage.delete_files(row["slug"], row["stored_filename"])
        db.delete_upload(row["slug"])
        deleted += 1
    return JSONResponse({"deleted": deleted})


# --- API ---

@router.post("/api/upload")
async def api_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    description: str = Form(""),
    tags: str = Form("[]"),
    client: str = Form(""),
    project_id: str = Form(""),
    modified_at: str = Form(""),
    folder_name: str = Form(""),
):
    # Which Source string a browser upload gets is decided server-side, not
    # by a client-supplied field — the desktop uploader app (see
    # desktop_app/constructicon_uploader/api.py) identifies itself with this
    # header on every request; the web upload drawer sends nothing extra, so
    # its absence is what marks a deliberate one-off drag-drop through the
    # browser UI.
    is_desktop_app = request.headers.get(DESKTOP_APP_CLIENT_HEADER) == DESKTOP_APP_CLIENT_VALUE
    user = db.SOURCE_AUTOMATED_UPLOAD if is_desktop_app else db.SOURCE_MANUAL_UPLOAD

    # #433: get file size early for duplicate check; use file.size if available,
    # otherwise measure via seek/tell
    file_size = file.size
    if file_size is None:
        current = file.file.tell()
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(current)

    try:
        source_modified_at = float(modified_at) / 1000 if modified_at else None
    except ValueError:
        # Same clean-400 contract type_metadata gets in /api/content (#221),
        # rather than a 500 traceback on a garbage timestamp.
        raise HTTPException(status_code=400, detail="modified_at must be a unix-milliseconds number")
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []

    result = await run_in_threadpool(
        lambda: ingest.ingest_file(
            file.file,
            file.filename,
            size=file_size,
            source=user,
            run_background=background_tasks.add_task,
            description=description,
            tags=tag_list,
            client=client,
            source_modified_at=source_modified_at,
            project_id=project_id,
            folder_name=folder_name,
        )
    )

    if result.duplicate:
        # _friendly_datetime, not a raw strftime with %-d/%-I -- those are the
        # platform-specific extensions that helper exists to avoid (#211).
        dupe_date = _friendly_datetime(result.row["timestamp"])
        raise HTTPException(
            status_code=409,
            detail=f"Already uploaded by {result.row['tech']} on {dupe_date} — see /object/{result.row['slug']}",
        )
    elif result.error:
        raise HTTPException(status_code=400, detail=result.error)

    response = _to_public(result.row)
    if result.pending_decision_id:
        response["pending_decision_id"] = result.pending_decision_id
    return JSONResponse(response)




@router.post("/api/content")
async def api_create_content(
    request: Request,
    background_tasks: BackgroundTasks,
    media_type: str | None = Form(None),
    external_url: str = Form(""),
    content_description: str = Form(""),
    content_date: str = Form(""),
    description: str = Form(""),
    tags: str = Form("[]"),
    client: str = Form(""),
    project_id: str = Form(""),
    type_metadata: str | None = Form(None),
):
    """Creates a capture_events row for content with no uploaded file — a
    YouTube link today, a stream/URL capture once a future issue wires up
    its capture_fn (see core/object_types.py). Distinct from /api/upload,
    which is for actual uploaded files and stays UPLOADED_FILE-only.

    Mirrors /api/upload's OCR handling: db.insert_content already stamps
    ocr_status="pending" for any OCR-capable type (see core/db.py), and this
    schedules the same background ocr.run_ocr — which fetches/generates the
    type's thumbnail before running OCR against it (see core/ocr.py).

    type_metadata (#54): an optional JSON object of freeform per-type
    properties (view/like/comment counts, full description, uploading
    channel — see core/object_types.py's "youtube" metadata_fields) to set
    at creation time, so a caller that already has this data (e.g.
    scripts/full_youtube_channel_sync.py, which fetches it from the YouTube
    Data API in the same pass it decides to create the row) doesn't need a
    separate follow-up call the way api_update_image's equivalent field
    does for correcting an EXISTING row.
    media_type auto-classifies from external_url (YouTube vs. a plain web
    page — see object_types.classify_url) when the caller doesn't already
    know which type it wants, matching the upload drawer's generic "paste a
    link" field (#184): the client no longer decides youtube-vs-url itself,
    it just posts the URL and lets the server figure out what it is.
    """
    is_desktop_app = request.headers.get(DESKTOP_APP_CLIENT_HEADER) == DESKTOP_APP_CLIENT_VALUE
    user = db.SOURCE_AUTOMATED_UPLOAD if is_desktop_app else db.SOURCE_MANUAL_UPLOAD
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    try:
        parsed_type_metadata = json.loads(type_metadata) if type_metadata else None
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="type_metadata must be valid JSON")
    try:
        content_date_epoch = float(content_date) if content_date else None
    except ValueError:
        raise HTTPException(status_code=400, detail="content_date must be a unix-seconds number")

    result = ingest.ingest_content(
        source=user,
        run_background=background_tasks.add_task,
        media_type=media_type,
        external_url=external_url or None,
        content_description=content_description or None,
        content_date=content_date_epoch,
        description=description,
        tags=tag_list,
        client=client or None,
        type_metadata=parsed_type_metadata,
        project_id=project_id,
    )

    if result.error:
        detail = result.error
        if result.error.endswith("require a file upload"):
            detail += " — use /api/upload"
        raise HTTPException(status_code=400, detail=detail)

    response = _to_public(result.row)
    if result.pending_decision_id:
        response["pending_decision_id"] = result.pending_decision_id
    return JSONResponse(response)


def _derive_processing_status(row):
    """#388: turn a raw row into the processing drawer's per-stage view.
    Stages shown depend on the type's capabilities: OCR + Embed for OCR-capable
    types, Caption for caption-capable ones. Embed has no status of its own — it
    settles in the same slot as OCR (see core/ocr.py) — so it's derived from
    embedding presence."""
    spec = object_types.get_object_type(row.get("media_type"))
    try:
        tm = json.loads(row.get("type_metadata") or "{}")
    except Exception:
        tm = {}
    # Embedding is deliberately NOT a stage: it's computed inside the OCR slot
    # (see core/ocr.py), so it has already settled by the time OCR reads "done".
    # Surfacing it separately would strand rows that silently never embedded at
    # a phantom "pending" forever. OCR + Caption are the real observable phases.
    stages = []
    if spec and spec.ocr_capable:
        s = row.get("ocr_status")
        stages.append({"stage": "OCR", "state": "done" if s == "done" else "failed" if s == "failed" else "pending"})
    if spec and spec.caption_capable:
        cs = tm.get("auto_caption_status")
        stages.append({"stage": "Caption", "state": "done" if cs == "done" else "failed" if cs == "failed" else "pending"})
    in_flight = any(st["state"] == "pending" for st in stages)
    if not stages:
        overall = "done"
    elif in_flight:
        overall = "processing"
    elif any(st["state"] == "failed" for st in stages):
        overall = "failed"
    else:
        overall = "done"
    return {"stages": stages, "in_flight": in_flight, "overall": overall}


@router.get("/api/processing")
def api_processing(request: Request, session: str = ""):
    """#388: in-flight post-upload work (OCR/caption/embed) for the processing
    drawer. Returns everything still mid-pipeline across the whole box (however
    it was uploaded), plus any explicitly-requested `session` slugs even once
    settled, so the drawer can show the caller's own items finish. Derived from
    existing per-row signals — no status is stored just for this."""
    session_slugs = [s for s in session.split(",") if s][:200]
    by_slug = {r["slug"]: r for r in db.list_processing_candidates()}
    for r in db.get_processing_rows_by_slugs(session_slugs):
        by_slug.setdefault(r["slug"], r)
    session_set = set(session_slugs)
    items = []
    in_flight_count = 0
    for slug, row in by_slug.items():
        st = _derive_processing_status(row)
        is_session = slug in session_set
        if not (st["in_flight"] or is_session):
            continue
        if st["in_flight"]:
            in_flight_count += 1
        items.append({
            "slug": slug,
            "name": row.get("display_name") or row.get("content_description") or row.get("filename") or slug,
            "stages": st["stages"],
            "overall": st["overall"],
            "is_session": is_session,
        })
    # In-flight first, then settled; within each, session items ahead of the rest.
    order = {"processing": 0, "failed": 1, "done": 2}
    items.sort(key=lambda x: (order.get(x["overall"], 3), not x["is_session"]))
    return JSONResponse({"items": items, "count": in_flight_count})


@router.get("/api/image/{slug}")
def api_get_image(request: Request, slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_to_public(row))


@router.post("/api/image/{slug}/ocr")
def api_retry_ocr(request: Request, slug: str, background_tasks: BackgroundTasks):
    """Force a (re-)run of OCR — for images that never got it, or a lousy
    first pass worth retrying."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="File was redacted — there's no image left to OCR")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.ocr_capable:
        raise HTTPException(status_code=400, detail=f"OCR isn't available for {spec.label} content")
    db.set_ocr_status(slug, "pending")
    background_tasks.add_task(ocr.run_ocr, slug)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@router.post("/api/image/{slug}/caption")
def api_retry_caption(request: Request, slug: str, background_tasks: BackgroundTasks, advance: bool = Form(False)):
    """#239: (re-)run the auto-caption suggestion for one object — for rows
    that predate captioning, a failed attempt, or a caption worth another
    roll. Same background/best-effort shape as api_retry_ocr; the detail
    page polls GET /api/image/{slug} for type_metadata.auto_caption_status
    to leave "pending".

    #250: advance=True is the detail page's "Regenerate" click (a caption
    already exists) — moves to the next step in captions.STEPS and runs
    only that one step (cascade=False), wrapping back to step 0 after the
    last one, so repeated clicks give real variety instead of repeating
    the same greedy default. advance=False (the "Generate" case, no prior
    caption) behaves as before: start at step 0 and auto-cascade on empty."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="File was redacted — there's no image left to caption")
    spec = object_types.get_object_type(row.get("media_type"))
    if not captions.should_caption(spec):
        raise HTTPException(status_code=400, detail=f"Captioning isn't available for {spec.label} content")
    db.update_content_metadata(slug, type_metadata={captions.STATUS_KEY: "pending"})
    if advance:
        current_step = row["type_metadata"].get(captions.STEP_KEY, 0)
        next_step = (current_step + 1) % len(captions.STEPS)
        background_tasks.add_task(captions.run_caption, slug, next_step, False)
    else:
        background_tasks.add_task(captions.run_caption, slug)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@router.post("/api/image/{slug}/caption/mark-used")
def api_mark_caption_used(slug: str):
    """#251: fired alongside the detail page's "Use this caption" click —
    records which STEPS rung produced the text just copied into the
    description field, separately from captions.STEP_KEY (which the next
    Regenerate click overwrites). Best-effort/fire-and-forget from the
    frontend's side: the description edit itself isn't gated on this
    succeeding, since losing the provenance note is much cheaper than
    losing the actual caption text."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    tm = row["type_metadata"]
    if tm.get(captions.STATUS_KEY) != "done" or not tm.get(captions.METADATA_KEY):
        raise HTTPException(status_code=400, detail="No current suggested caption to mark as used")
    step_index = tm.get(captions.STEP_KEY, 0)
    step_label = captions.describe_step(step_index)
    db.update_content_metadata(slug, type_metadata={
        captions.DESCRIPTION_STEP_KEY: step_index,
        captions.DESCRIPTION_STEP_LABEL_KEY: step_label,
        captions.DESCRIPTION_MODEL_KEY: tm.get("auto_caption_model"),
        captions.DESCRIPTION_USED_AT_KEY: time.time(),
    })
    return JSONResponse({"step": step_index, "step_label": step_label})


@router.get("/api/captions/unreviewed")
def api_captions_unreviewed(request: Request):
    """#409: objects with an auto-caption suggestion awaiting review — feeds the
    bulk caption-review page. Accept reuses POST /api/image/{slug} (sets
    content_description) + caption/mark-used; skip is POST /api/captions/{slug}/skip."""
    out = []
    for r in db.list_unaccepted_captions():
        tm = r.get("type_metadata") or {}
        out.append({
            "slug": r["slug"],
            "name": r.get("display_name") or r.get("filename") or r["slug"],
            "caption": (tm.get("auto_caption") or "").strip(),
            "thumb_url": f"/f/{r['slug']}/thumb",
            "detail_url": f"/object/{r['slug']}",
        })
    return JSONResponse(out)


@router.post("/api/captions/{slug}/skip")
def api_caption_skip(slug: str):
    """#409: mark an auto-caption suggestion reviewed-but-not-used, so it drops
    out of the confirm_caption queue without being copied into the description."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    db.update_content_metadata(slug, type_metadata={"auto_caption_dismissed": True})
    return JSONResponse({"ok": True})


@router.post("/api/image/{slug}")
async def api_update_image(
    request: Request,
    slug: str,
    description: str | None = Form(None),
    tags: str | None = Form(None),
    client: str | None = Form(None),
    display_name: str | None = Form(None),
    icon: str | None = Form(None),
    content_description: str | None = Form(None),
    type_metadata: str | None = Form(None),
    content_date: float | None = Form(None),
    display_date: str | None = Form(None),
    reset_display_date: bool = Form(False),
    provenance: str | None = Form(None),
    highlight: str | None = Form(None),
):
    # #213 / A5 fix: only parse and pass tags if they were actually provided
    # in the form. Defaults of None mean "don't touch this field", allowing
    # partial updates (e.g. rename-only) without inadvertently wiping tags.
    tag_list = None
    if tags is not None:
        try:
            tag_list = json.loads(tags) if tags else []
        except json.JSONDecodeError:
            tag_list = []
    row = db.update_tags(slug, description=description, tags=tag_list, client=client)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    # #244: distinguish "field not provided" (None) from "field provided empty"
    # ("") so we can clear overrides -- FastAPI's Form(None) collapses BOTH
    # cases to the same None value, so read the raw form directly instead.
    # Pass the raw string straight through to rename_object as-is ("" included)
    # rather than normalizing "" back to None here -- rename_object's own
    # contract already treats None as "leave unchanged" and "" as "clear back
    # to the default fallback" (see its docstring); re-coercing "" to None
    # before calling it would silently throw away that distinction and defeat
    # the whole point of this fix (confirmed live: it did exactly that).
    form_data = await request.form()
    raw_display_name = form_data.get("display_name") if "display_name" in form_data else None
    raw_icon = form_data.get("icon") if "icon" in form_data else None

    # display_name/icon (#11) — no dedicated UI yet (see #24's "Coming soon
    # (#11)" admin-page stub), but the field/endpoint exists so a "rename"
    # or "set icon" is at least possible by hand (a form POST here).
    if "display_name" in form_data or "icon" in form_data:
        row = db.rename_object(slug, display_name=raw_display_name, icon=raw_icon)
    # content_description/type_metadata (#54): lets a caller correct a
    # row's title-ish blurb and/or per-type metadata after creation — added
    # for scripts/full_youtube_channel_sync.py's correction pass (site-
    # scraped titles overwritten with the real YouTube Data API title, plus
    # view/like/comment counts and, when applicable, the uploading channel).
    # type_metadata is a JSON object string, MERGED into whatever the row
    # already has (see db.update_content_metadata) rather than replacing it
    # wholesale, so this can't be used to accidentally wipe out a field some
    # other future writer already set.
    if content_description is not None or type_metadata is not None:
        parsed_metadata = None
        if type_metadata is not None:
            try:
                parsed_metadata = json.loads(type_metadata) if type_metadata else {}
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="type_metadata must be valid JSON")
            # #425: the physical-piece keys (medium, dimensions, date_made, original_location)
            # are trimmed/length-capped and date_made must be YYYY[-MM[-DD]]; other keys pass through.
            if isinstance(parsed_metadata, dict):
                try:
                    parsed_metadata = physical_piece.clean_fields(parsed_metadata)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
        row = db.update_content_metadata(slug, content_description=content_description, type_metadata=parsed_metadata)
    # content_date (Timeline feature, #265): a correction/backfill script
    # with a real known date (e.g. a YouTube video's publishedAt, already
    # fetched on every scripts/full_youtube_channel_sync.py correction
    # pass but previously had no way to write it back) sets it directly —
    # a real value, not a manual override like display_date below.
    if content_date is not None:
        db.set_content_date(slug, content_date)
        row = db.get_by_slug(slug)
    # Timeline feature: reset_display_date wins over a stray display_date
    # value if a client somehow sends both (mirrors the MCP tools' same
    # reset-flag convention in mcp_server/server.py).
    if reset_display_date:
        db.set_display_date_override(slug, None)
        row = db.get_by_slug(slug)
    elif display_date:
        # The <input type="datetime-local"> this comes from is pre-filled
        # by _datetime_local_value in Mountain Time (see there) — parse the
        # owner's typed value the same way, not as the container's own
        # system timezone (UTC), or a no-op re-save would silently shift
        # the stored time by several hours.
        db.set_display_date_override(slug, timeline.source_datetime_to_epoch(datetime.fromisoformat(display_date)))
        row = db.get_by_slug(slug)
    # provenance (#341): like display_name/icon, read the raw form so ""
    # clears the value (None-means-"don't change" convention applies here too).
    # form_data already read above, reuse it.
    if "provenance" in form_data:
        raw_provenance = form_data.get("provenance") if form_data.get("provenance") else None
        row = db.set_provenance(slug, raw_provenance)
    # highlight (#341): truthy form value (any non-empty string) marks it as
    # highlighted, empty/missing means off.
    if "highlight" in form_data:
        raw_highlight = form_data.get("highlight")
        row = db.set_highlight(slug, bool(raw_highlight))
    # brand asset (#350): checkbox for is_brand_asset (0/1), optional text field for brand_role.
    # Like highlight, any non-empty string is truthy for is_brand_asset.
    if "is_brand_asset" in form_data:
        raw_is_brand = form_data.get("is_brand_asset")
        raw_brand_role = form_data.get("brand_role") if form_data.get("brand_role") else None
        row = db.set_brand_asset(slug, bool(raw_is_brand), brand_role=raw_brand_role)
    return JSONResponse(_to_public(row))


@router.post("/api/image/{slug}/redact")
def api_redact_image(request: Request, slug: str):
    """Delete the file only — sensitive content (e.g. a visible password) —
    but keep the metadata for future correlation."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if not row.get("stored_filename"):
        raise HTTPException(status_code=400, detail="This row has no uploaded file to redact")
    storage.delete_files(slug, row["stored_filename"])
    updated = db.mark_redacted(slug)
    return JSONResponse(_to_public(updated))


@router.post("/api/image/{slug}/unredact")
def api_unredact_image(request: Request, slug: str):
    """#282: reverse of /redact -- clears the flag so the row rejoins
    ordinary browsing/search. Can't bring the file back: /redact deleted it
    from storage before setting the flag, so the row stays a file-less
    metadata record; it's just findable again. 409 rather than a silent
    no-op on a row that isn't redacted, so a stale admin-page list can't
    misreport success."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if not row["redacted"]:
        raise HTTPException(status_code=409, detail="This row isn't redacted")
    updated = db.unmark_redacted(slug)
    return JSONResponse(_to_public(updated))


@router.post("/api/image/{slug}/delete")
def api_delete_image(request: Request, slug: str):
    """Full delete — file and metadata both gone, no recovery."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.get("stored_filename"):
        storage.delete_files(slug, row["stored_filename"])
    db.delete_upload(slug)
    return JSONResponse({"deleted": True})


@router.post("/api/image/{slug}/thumbnail/refresh")
def api_refresh_thumbnail(request: Request, slug: str):
    """#385: Force (re)generation of a thumbnail. Deletes any existing cached
    thumbnail and calls ensure_thumbnail to regenerate it. Useful when a
    thumbnail failed (e.g. youtube FETCH_URL thumbnail 404'd) or was missed."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    spec = object_types.get_object_type(row.get("media_type"))
    if spec.thumbnail_source == object_types.ThumbnailSource.NONE:
        raise HTTPException(status_code=400, detail="This object type has no thumbnail")

    # Delete the existing thumbnail to force a fresh generation
    thumb_path = storage.thumb_path_for(slug)
    if thumb_path.exists():
        try:
            thumb_path.unlink()
        except Exception as e:
            # #548: a failure is an error status in the shared shape, not an HTTP 200 {ok:false}.
            raise HTTPException(status_code=500, detail=f"Failed to delete cached thumbnail: {e!r}")

    # Regenerate the thumbnail
    success = thumbnails.ensure_thumbnail(row)
    if success:
        return JSONResponse({
            "ok": True,
            "thumb_url": f"/f/{slug}/thumb"
        })
    raise HTTPException(status_code=500, detail="Failed to generate thumbnail (see logs for details)")


@router.post("/api/image/{slug}/action/{key}")
async def api_run_type_action(request: Request, slug: str, key: str):
    """#448: Generic per-type action route. The type file owns the handler;
    actions are declared in ObjectTypeSpec.actions. #446: actions must
    applies_to(row) to be runnable."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    if row.get("redacted"):
        raise HTTPException(status_code=409, detail="object is redacted")

    spec = object_types.get_object_type(row.get("media_type"))
    action = next((a for a in spec.actions if a.key == key), None)
    if action is None:
        raise HTTPException(status_code=404, detail=f"{spec.label} has no action '{key}'")

    if not action.applies_to(row):
        raise HTTPException(status_code=404, detail=f"{spec.label} has no action '{key}' for this item")

    try:
        result = await run_in_threadpool(action.handler, row)
    except Exception as e:
        print(f"Action '{key}' failed: {e!r}", flush=True)
        raise HTTPException(status_code=500, detail=f"Action '{key}' failed: {e}")

    updated_row = db.get_by_slug(slug)
    return JSONResponse({
        "ok": True,
        "action": key,
        **(result or {}),
        "item": _to_public(updated_row)
    })


@router.post("/api/image/{slug}/related")
def api_add_related(request: Request, slug: str, related_slug: str = Form(...)):
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    if db.get_by_slug(related_slug) is None:
        raise HTTPException(status_code=404, detail="related image not found")
    db.add_relation(slug, related_slug)
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


@router.post("/api/image/{slug}/related/remove")
def api_remove_related(request: Request, slug: str, related_slug: str = Form(...)):
    db.remove_relation(slug, related_slug)
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


# --- Revision chains (#477) ---
# Core (core/revisions.py) validates and writes through the change log; a rule violation is a
# CardError, which the app-wide handler turns into the shared 422/409/404 {error:{code,message}}.

@router.get("/api/image/{slug}/revisions")
def api_get_revisions(slug: str):
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(revisions.revision_view(slug))


@router.post("/api/image/{slug}/superseded-by")
def api_mark_superseded_by(slug: str, new_slug: str = Form(...)):
    """`new_slug` replaces this item (this item becomes "Superseded, see <current>")."""
    result = revisions.mark_superseded(slug, new_slug)
    return JSONResponse({**result, "revisions": revisions.revision_view(slug)})


@router.post("/api/image/{slug}/supersedes")
def api_mark_supersedes(slug: str, old_slug: str = Form(...)):
    """This item replaces `old_slug`."""
    result = revisions.mark_superseded(old_slug, slug)
    return JSONResponse({**result, "revisions": revisions.revision_view(slug)})


@router.post("/api/image/{slug}/revisions/remove")
def api_remove_from_revisions(slug: str):
    """Takes this item out of its chain; its neighbours link up (A -> B -> C minus B = A -> C)."""
    result = revisions.remove_from_chain(slug)
    return JSONResponse({**result, "revisions": revisions.revision_view(slug)})


@router.post("/api/image/{slug}/project")
def api_add_object_to_project(request: Request, slug: str, project_id: str = Form(...)):
    """#47: the object detail page's project editor — same membership
    primitive as an upload-time project pick (_attach_to_project, #1), just
    reachable after the fact instead of only at upload time. Unlike
    _attach_to_project's silent-ignore-on-bad-id (fine for a stale value
    riding along with an upload), a bad project_id here is a real error —
    it's the only thing this request is trying to do."""
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    if db.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    ingest.attach_to_project(slug, project_id)
    return JSONResponse([_to_project_option(p) for p in db.list_projects_for_post(slug)])


@router.post("/api/image/{slug}/project/remove")
def api_remove_object_from_project(request: Request, slug: str, project_id: str = Form(...)):
    """Removes membership only — deliberately leaves the project's linked
    tag (if any) alone, same as removing a manually-curated Related item
    never untags anything either. The tag field is already separately
    editable right above this on the detail page if the user wants it gone
    too."""
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db.remove_item_from_project(project["id"], slug)
    return JSONResponse([_to_project_option(p) for p in db.list_projects_for_post(slug)])


@router.get("/api/image/{slug}/similar")
def api_get_similar(request: Request, slug: str):
    """Auto-detected candidates — visual (perceptual hash) and/or semantic
    (text embedding) — distinct from the manually-curated Related panel.
    Each result carries similarity_reason ("visual"/"text"/"both") and
    similarity_score so the UI can label why it's suggested.
    """
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    matches = similarity.find_similar(slug)
    results = []
    for m in matches:
        row = db.get_by_slug(m["slug"])
        if row is None:
            continue
        item = _to_public(row)
        item["similarity_reason"] = m["reason"]
        item["similarity_score"] = round(m["score"], 3)
        results.append(item)
    return JSONResponse(results)


@router.get("/api/gallery")
def api_gallery(request: Request, query: str = "", client: str = "", per_user: int = 4):
    """Grouped-by-uploader gallery data: each uploader's most recent N items
    plus their real total, queried per-uploader so no single prolific
    uploader's activity can push others out of a global result limit.
    """
    uploaders = db.list_uploaders(query=query or None, client=client or None)
    groups = []
    for u in uploaders:
        items = db.search(query=query or None, client=client or None, uploaded_by=u["uploaded_by"], limit=per_user)
        groups.append({
            # u["uploaded_by"] is already the short group key (see
            # db.list_uploaders) — used both as the display label and as the
            # /gallery/user/<uploader> link target.
            "uploaded_by": u["uploaded_by"],
            "uploaded_by_display": u["uploaded_by"],
            "total": u["total"],
            "items": [_to_public(r) for r in items],
        })
    return JSONResponse(groups)


@router.get("/api/clients")
def api_clients(request: Request):
    return JSONResponse(db.list_clients())




@router.post("/api/bulk/add-to-project")
def api_bulk_add_to_project(slugs: list[str] = Form(...), project_id: str = Form(...)):
    """Issue #98: the Unfiled page's bulk "add to project" action. Same
    per-slug primitive as a single upload's project pick (_attach_to_project)
    — a bad/stale project_id is silently a no-op for every slug, same as the
    single-object path, rather than partially failing the batch."""
    count = 0
    for slug in slugs:
        if db.get_by_slug(slug) is not None:
            ingest.attach_to_project(slug, project_id)
            count += 1
    return JSONResponse({"count": count})


@router.post("/api/bulk/attach-tags")
def api_bulk_attach_tags(slugs: list[str] = Form(...), tag_names: list[str] = Form(...)):
    """Issue #98: the Unfiled page's bulk tagging action. db.update_tags
    fully replaces a row's tags/description/client with whatever's passed —
    the single-object edit form gets away with this because it always
    resends the complete current description+tags together. A bulk action
    can't do that: it must read each row first and merge, or it would wipe
    out every selected item's existing tags and description. tag_names are
    unioned onto each row's own existing tags (deduped); description/client
    are passed back unchanged from the row itself."""
    tag_names = [t.strip() for t in tag_names if t.strip()]
    if not tag_names:
        return JSONResponse({"count": 0})
    count = 0
    for slug in slugs:
        row = db.get_by_slug(slug)
        if row is None:
            continue
        merged_tags = sorted(set(row["tags"]) | set(tag_names))
        db.update_tags(slug, description=row["description"], tags=merged_tags, client=row.get("client"))
        count += 1
    return JSONResponse({"count": count})


@router.get("/api/tags")
def api_tags(request: Request):
    """Return the tag tree flattened with breadcrumb paths for autocomplete.
    Each tag includes its full path from root (e.g. "Parent > Child > Leaf")
    for display in suggestions."""
    def flatten_with_paths(nodes, path=""):
        """Recursively flatten tree nodes with breadcrumb paths."""
        flat = []
        for node in nodes:
            # Build the breadcrumb path for this node
            node_path = f"{path} > {node['name']}" if path else node['name']
            flat.append({
                "id": node["id"],
                "name": node["name"],
                "slug": node["slug"],
                "path": node_path,  # Full breadcrumb for display
                "parent_id": node["parent_id"],
            })
            # Recursively add children
            if node.get("children"):
                flat.extend(flatten_with_paths(node["children"], node_path))
        return flat

    tag_tree = db.list_tag_tree()
    flat_tags = flatten_with_paths(tag_tree)
    return JSONResponse(flat_tags)


@router.get("/api/search")
def api_search(request: Request, query: str = "", tags: str = "", client: str = ""):
    tag_list = [t for t in tags.split(",") if t] or None
    results = db.search(query=query or None, tags=tag_list, client=client or None)
    # #477: search still finds old revisions, but marks them (superseded_by = the current one).
    return JSONResponse(revisions.decorate([_to_public(r) for r in results]))
