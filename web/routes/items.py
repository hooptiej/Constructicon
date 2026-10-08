"""Item routes (#547): upload, content, processing, /api/image/*, per-item captions,
gallery, clients, bulk edits, tags, search, multi-delete."""

import json
import logging
import time
from datetime import datetime

from fastapi import Request, Form, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from core import besteffort, captions, db, ingest, items, membership, object_types, ocr, revisions, similarity, storage, thumbnails
from core import tags as tags_svc, timeline
from web.common import DESKTOP_APP_CLIENT_HEADER, DESKTOP_APP_CLIENT_VALUE
from web.shapes import _friendly_datetime, _to_project_option, _to_public
from core import policy, roles
from core.object_types import code as code_type
from web.roles import RoleRouter, requires

log = logging.getLogger("constructicon.web")


def _parse_tags_form(tags):
    """The `tags` form field: a JSON list of tag names, or empty. A value that isn't valid JSON is a
    clean 400 (#551: it used to be silently treated as "no tags", so the tags a client sent were lost)."""
    if not tags:
        return []
    try:
        return json.loads(tags)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="tags must be a JSON list of tag names")

router = RoleRouter(default_role=roles.EDITOR)  # #557: routes without their own label are editor


@router.post("/api/delete")
def api_delete_selected(slugs: list[str] = Form(...)):
    """Selective delete (#19) — remove just the given objects, any mix of
    sources (uploaded images, youtube rows, etc.), without touching tags
    or projects. That global wipe is specific to /api/delete-all's
    full-reset button; this is the day-to-day 'clear this test content'
    path, driven by gallery checkboxes or a single object's detail page.
    #541: one items.delete batch (files to the trash for 7 days); unknown slugs are skipped.
    `batch_id` undoes the lot via POST /api/changes/{batch_id}/undo."""
    result = items.delete(slugs, missing_ok=True)
    return JSONResponse({"deleted": result.data["deleted"], "batch_id": result.batch_id,
                         "trash_days": items.TRASH_DAYS})


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
    user = db.source_automated_upload() if is_desktop_app else db.source_manual_upload()  # #562

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
    tag_list = _parse_tags_form(tags)

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
    user = db.source_automated_upload() if is_desktop_app else db.source_manual_upload()  # #562
    tag_list = _parse_tags_form(tags)
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
    except Exception as e:
        besteffort.warn(log, "items: unreadable type_metadata JSON in the processing status", e,
                        slug=row.get("slug"))
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
        if cs == "done":
            state = "done"
        elif captions.DISABLED:
            # #585: captioning is switched off, so nothing will ever queue this item. "off" is
            # settled (not in flight, not a failure); a caption an agent wrote still reads "done".
            state = "off"
        else:
            state = "failed" if cs == "failed" else "pending"
        stages.append({"stage": "Caption", "state": state})
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


@router.get("/api/processing", dependencies=requires(roles.VIEWER))
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
    return JSONResponse({"items": items, "count": in_flight_count, "captions_disabled": captions.DISABLED})


@router.get("/api/image/{slug}", dependencies=requires(roles.VIEWER))
def api_get_image(request: Request, slug: str):
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    return JSONResponse(_to_public(row))


# #607: the OCR / text panel no longer rides inside the object page (a 1 MB file text was embedded three
# times and froze the tab). The page fetches it here, on demand, capped.
TEXT_DEFAULT_LIMIT = 100_000
TEXT_MAX_LIMIT = 1_000_000


@router.get("/api/image/{slug}/text", dependencies=requires(roles.VIEWER))
def api_get_item_text(request: Request, slug: str, limit: int = TEXT_DEFAULT_LIMIT):
    """The item's extracted text (OCR / the text layer / a text file's content), first `limit`
    characters (default 100,000, at most 1,000,000), with the full length so the page can say
    "showing N of M"."""
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    limit = max(1, min(int(limit), TEXT_MAX_LIMIT))
    text = row.get("extracted_text") or ""
    return JSONResponse({"slug": slug, "text": text[:limit], "chars": len(text),
                         "limit": limit, "truncated": len(text) > limit,
                         "ocr_status": row.get("ocr_status")})  # the page polls this for OCR progress


# A saved page must not be able to phone home, run script, post a form or read the app's cookies.
# `sandbox` (no tokens) = no scripts, forms, popups, same-origin or top navigation even if this URL is
# opened directly; default-src 'none' = no network at all (images/fonts/media only as data: URIs, styles
# only inline). The page itself is framed by an <iframe sandbox=""> as well (core/object_types/code.py).
RENDERED_HTML_CSP = ("sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
                     "media-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")


@router.get("/api/image/{slug}/rendered", dependencies=requires(roles.VIEWER))
def api_rendered_html(request: Request, slug: str):
    """An uploaded .html file as a web page, for the object page's sandboxed Rendered view (#607).
    Same item policy as the file itself; decoded from its own encoding (a UTF-16 `gpresult /h`
    report included) and served as UTF-8 under the strict CSP above."""
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    policy.require_file(row)  # a redacted file is admin-only, as at /f/<slug>
    if row["redacted"]:
        raise HTTPException(status_code=410, detail="file was redacted (sensitive content)")
    if not row.get("stored_filename") or not code_type.is_html(row.get("filename")):
        raise HTTPException(status_code=404, detail="this item is not an HTML file")
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    html, truncated = code_type.render_source(path)
    headers = {
        "Content-Security-Policy": RENDERED_HTML_CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, max-age=60",
    }
    if truncated:
        headers["X-Rendered-Truncated"] = "1"
    return Response(content=html, media_type="text/html; charset=utf-8", headers=headers)


@router.post("/api/image/{slug}/ocr")
def api_retry_ocr(request: Request, slug: str, background_tasks: BackgroundTasks):
    """Force a (re-)run of OCR — for images that never got it, or a lousy
    first pass worth retrying."""
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable
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
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="File was redacted — there's no image left to caption")
    spec = object_types.get_object_type(row.get("media_type"))
    if not captions.should_caption(spec):
        raise HTTPException(status_code=400, detail=f"Captioning isn't available for {spec.label} content")
    captions.mark_pending(slug)  # pipeline bookkeeping, owned by core/captions.py (#541 phase D)
    if advance:
        current_step = row["type_metadata"].get(captions.STEP_KEY, 0)
        next_step = (current_step + 1) % len(captions.STEPS)
        captions.run_caption(slug, next_step, False)  # #592: persists into caption_queue, no background task
    else:
        captions.run_caption(slug)
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
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    tm = row["type_metadata"]
    if tm.get(captions.STATUS_KEY) != "done" or not tm.get(captions.METADATA_KEY):
        raise HTTPException(status_code=400, detail="No current suggested caption to mark as used")
    step_index = tm.get(captions.STEP_KEY, 0)
    step_label = captions.describe_step(step_index)
    items.update(slug, type_metadata={
        captions.DESCRIPTION_STEP_KEY: step_index,
        captions.DESCRIPTION_STEP_LABEL_KEY: step_label,
        captions.DESCRIPTION_MODEL_KEY: tm.get("auto_caption_model"),
        captions.DESCRIPTION_USED_AT_KEY: time.time(),
    })
    return JSONResponse({"step": step_index, "step_label": step_label})


@router.get("/api/captions/unreviewed", dependencies=requires(roles.VIEWER))
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
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    items.update(slug, type_metadata={"auto_caption_dismissed": True})
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
    """#541 phase B: every field below is one items.update call, so one Save is ONE change-log
    batch (undo restores all of it). Validation (provenance key, physical-piece date, dates)
    happens before anything is written. #541 phase C: free-text tags ride in the same call
    (items.update(tags=...)), so tags + fields are still ONE batch."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    tag_list = None
    if tags is not None:
        tag_list = _parse_tags_form(tags)

    # #244: distinguish "field not provided" (None) from "field provided empty"
    # ("") so we can clear overrides -- FastAPI's Form(None) collapses BOTH
    # cases to the same None value, so read the raw form directly instead.
    # A present-but-empty display_name/icon clears the override back to the
    # default fallback; an absent one leaves it alone.
    form_data = await request.form()
    fields = {}
    if description is not None:
        fields["description"] = description
    if client is not None:
        fields["client"] = client
    # display_name/icon (#11) — a "rename" or "set icon" by hand (a form POST here).
    if "display_name" in form_data:
        fields["display_name"] = form_data.get("display_name")
    if "icon" in form_data:
        fields["icon"] = form_data.get("icon")
    # content_description/type_metadata (#54): corrections after creation (e.g.
    # scripts/full_youtube_channel_sync.py). type_metadata is a JSON object string, MERGED
    # into whatever the row already has, never a wholesale replace; the #425 physical-piece
    # keys are cleaned/validated in core.
    if content_description is not None:
        fields["content_description"] = content_description
    if type_metadata is not None:
        try:
            fields["type_metadata"] = json.loads(type_metadata) if type_metadata else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="type_metadata must be valid JSON")
    # content_date (Timeline feature, #265): a real known date, not an override.
    if content_date is not None:
        fields["content_date"] = content_date
    # Timeline feature: reset_display_date wins over a stray display_date value.
    if reset_display_date:
        fields["display_date_override"] = None
    elif display_date:
        # The <input type="datetime-local"> is pre-filled in Mountain Time (see
        # _datetime_local_value); parse the typed value the same way, not as UTC.
        fields["display_date_override"] = timeline.source_datetime_to_epoch(datetime.fromisoformat(display_date))
    # provenance (#341): "" clears.
    if "provenance" in form_data:
        fields["provenance"] = form_data.get("provenance") or None
    # highlight (#341): any non-empty string is on.
    if "highlight" in form_data:
        fields["highlight"] = bool(form_data.get("highlight"))
    # brand asset (#350): checkbox plus optional role.
    if "is_brand_asset" in form_data:
        fields["is_brand_asset"] = bool(form_data.get("is_brand_asset"))
        fields["brand_role"] = form_data.get("brand_role") or None
    if fields or tag_list is not None:
        row = items.update(slug, tags=tag_list, **fields).item
    else:
        row = db.get_by_slug(slug)
    return JSONResponse(_to_public(row))


@router.post("/api/image/{slug}/redact")
def api_redact_image(request: Request, slug: str):
    """Remove the file only — sensitive content (e.g. a visible password) —
    but keep the metadata for future correlation. The file is HELD in the
    trash with no expiry (owner decision 2026-10-04): never auto-deleted, and
    "Empty trash now" skips it. Recover it (POST .../recover-redacted) or
    delete it permanently (POST .../delete-redacted-file)."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    result = items.redact(slug)
    return JSONResponse({**_to_public(result.item), "batch_id": result.batch_id, "held": True})


@router.post("/api/image/{slug}/recover-redacted")
def api_recover_redacted(slug: str):
    """Brings a held redacted file back and un-redacts the item (as before the redact).
    409 no_redact_hold when no file is held (already deleted, or an old redaction)."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    result = items.recover_redacted(slug)
    return JSONResponse({**_to_public(result.item), "batch_id": result.batch_id})


@router.post("/api/image/{slug}/delete-redacted-file", dependencies=requires(roles.ADMIN))
def api_delete_redacted_file(slug: str, confirm: str = Form("")):
    """Permanently erases the held redacted file (confirm=true). The item stays redacted,
    metadata only. Not undoable."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    result = items.delete_redacted_file(slug, confirm)
    return JSONResponse({"deleted": True, "slug": slug, "bytes": result.data["bytes"]})


@router.post("/api/image/{slug}/unredact")
def api_unredact_image(request: Request, slug: str):
    """#282: reverse of /redact -- clears the flag so the row rejoins
    ordinary browsing/search. For a redaction whose file is already gone
    (old redactions, or after delete-redacted-file); the row stays a file-less
    metadata record, just findable again. While the file is still held it
    answers 409 redact_hold_exists: recover it or delete it permanently. 409 rather than a silent
    no-op on a row that isn't redacted, so a stale admin-page list can't
    misreport success."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    return JSONResponse(_to_public(items.unredact(slug).item))


@router.post("/api/image/{slug}/delete")
def api_delete_image(request: Request, slug: str):
    """Full delete — the row and everything pointing at it. #541: the file goes to the trash
    for 7 days; POST /api/changes/{batch_id}/undo restores row, links and file until then."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    result = items.delete([slug])
    return JSONResponse({"deleted": True, "batch_id": result.batch_id, "trash_days": items.TRASH_DAYS})


@router.post("/api/image/{slug}/thumbnail/refresh")
def api_refresh_thumbnail(request: Request, slug: str):
    """#385: Force (re)generation of a thumbnail. Deletes any existing cached
    thumbnail and calls ensure_thumbnail to regenerate it. Useful when a
    thumbnail failed (e.g. youtube FETCH_URL thumbnail 404'd) or was missed."""
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable

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
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable

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
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    policy.viewable_item(related_slug, "related image not found")  # #467: 404 if missing or not viewable
    items.relate(slug, related_slug)  # #541 phase C: the link and the tags/cards it shares, one undo
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


@router.post("/api/image/{slug}/related/remove")
def api_remove_related(request: Request, slug: str, related_slug: str = Form(...)):
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    items.unrelate(slug, related_slug)
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


# --- Revision chains (#477) ---
# Core (core/revisions.py) validates and writes through the change log; a rule violation is a
# CardError, which the app-wide handler turns into the shared 422/409/404 {error:{code,message}}.

@router.get("/api/image/{slug}/revisions", dependencies=requires(roles.VIEWER))
def api_get_revisions(slug: str):
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    return JSONResponse(revisions.revision_view(slug))


@router.post("/api/image/{slug}/superseded-by")
def api_mark_superseded_by(slug: str, new_slug: str = Form(...)):
    """`new_slug` replaces this item (this item becomes "Superseded, see <current>")."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    policy.viewable_item(new_slug)
    result = revisions.mark_superseded(slug, new_slug)
    return JSONResponse({**result, "revisions": revisions.revision_view(slug)})


@router.post("/api/image/{slug}/supersedes")
def api_mark_supersedes(slug: str, old_slug: str = Form(...)):
    """This item replaces `old_slug`."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    policy.viewable_item(old_slug)
    result = revisions.mark_superseded(old_slug, slug)
    return JSONResponse({**result, "revisions": revisions.revision_view(slug)})


@router.post("/api/image/{slug}/revisions/remove")
def api_remove_from_revisions(slug: str):
    """Takes this item out of its chain; its neighbours link up (A -> B -> C minus B = A -> C)."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
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
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    if db.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    membership.add_files(project_id, [slug], **membership.UI_EFFECTS)  # #541 phase C: undoable
    return JSONResponse([_to_project_option(p) for p in db.list_projects_for_post(slug)])


@router.post("/api/image/{slug}/project/remove")
def api_remove_object_from_project(request: Request, slug: str, project_id: str = Form(...)):
    """Removes membership only — deliberately leaves the project's linked
    tag (if any) alone, same as removing a manually-curated Related item
    never untags anything either. The tag field is already separately
    editable right above this on the detail page if the user wants it gone
    too."""
    policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    membership.remove_files(project["id"], [slug])
    return JSONResponse([_to_project_option(p) for p in db.list_projects_for_post(slug)])


@router.get("/api/image/{slug}/similar", dependencies=requires(roles.VIEWER))
def api_get_similar(request: Request, slug: str):
    """Auto-detected candidates — visual (perceptual hash) and/or semantic
    (text embedding) — distinct from the manually-curated Related panel.
    Each result carries similarity_reason ("visual"/"text"/"both") and
    similarity_score so the UI can label why it's suggested.
    """
    row = policy.viewable_item(slug)  # #467: 404 if missing or not viewable
    matches = similarity.find_similar(slug)
    results = []
    for m in matches:
        row = db.get_by_slug(m["slug"])
        if row is None or not policy.can_view(row):  # #557
            continue
        item = _to_public(row)
        item["similarity_reason"] = m["reason"]
        item["similarity_score"] = round(m["score"], 3)
        results.append(item)
    return JSONResponse(results)


@router.get("/api/gallery", dependencies=requires(roles.VIEWER))
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
            "items": [_to_public(r) for r in policy.filter_visible(items)],  # #557 (+ the browse clause in db.search)
        })
    return JSONResponse(groups)


@router.get("/api/clients", dependencies=requires(roles.VIEWER))
def api_clients(request: Request):
    return JSONResponse(db.list_clients())




@router.post("/api/bulk/add-to-project")
def api_bulk_add_to_project(slugs: list[str] = Form(...), project_id: str = Form(...)):
    """Issue #98: the Unfiled page's bulk "add to project" action. Same
    per-slug primitive as a single upload's project pick (_attach_to_project)
    — a bad/stale project_id is silently a no-op for every slug, same as the
    single-object path, rather than partially failing the batch.

    #541 phase C: one membership.add_files call (all side effects, as before), so the whole
    bulk add is one batch; the answer carries its batch_id for undo."""
    count = sum(1 for slug in slugs if db.get_by_slug(slug) is not None)
    if not project_id or db.get_project(project_id) is None:
        return JSONResponse({"count": count})
    result = membership.add_files(project_id, slugs, missing_ok=True, **membership.UI_EFFECTS)
    return JSONResponse({"count": count, "batch_id": result.batch_id})


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
    # #541 phase C: core/tags.py, one batch (tags it creates are imaged, so undo removes them).
    result = tags_svc.merge_item_tags(slugs, tag_names)
    return JSONResponse({"count": result.data["count"], "batch_id": result.batch_id})


@router.get("/api/tags", dependencies=requires(roles.VIEWER))
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


@router.get("/api/search", dependencies=requires(roles.VIEWER))
def api_search(request: Request, query: str = "", tags: str = "", client: str = ""):
    tag_list = [t for t in tags.split(",") if t] or None
    # #557: db.search applies the policy's browse clause; the per-item policy is checked here too.
    results = policy.filter_visible(db.search(query=query or None, tags=tag_list, client=client or None))
    # #477: search still finds old revisions, but marks them (superseded_by = the current one).
    return JSONResponse(revisions.decorate([_to_public(r) for r in results]))
