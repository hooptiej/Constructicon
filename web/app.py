"""Constructicon web app: upload, gallery, and the public /f/{slug} hotlink
route.

No auth — this runs on a LAN-only dev server with no port forward, so the
network perimeter is the security boundary, not a login gate.
"""

import asyncio
import io
import json
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import backup, db, object_types, ocr, similarity, storage, thumbnails

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
# Brand assets (logo, wordmarks, favicons) live at the repo root in
# assets/brand/, independent of web/static/ — see README's "Retained art
# assets" section. Mounted separately rather than copied into web/static so
# there's a single source of truth for them.
_BRAND_DIR = Path(__file__).resolve().parent.parent / "assets" / "brand"
if _BRAND_DIR.is_dir():
    app.mount("/brand", StaticFiles(directory=_BRAND_DIR), name="brand")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Source (capture_events.tech): who or what actually added a row, and how —
# see core/db.py's SOURCE_* constants/source_group() for the full vocabulary
# and grouping logic. The web upload drawer and the desktop uploader app both
# POST to /api/upload with no client-supplied identity (this is a
# single-owner site, not a multi-tech tool) — the server tells them apart by
# the desktop app's identifying request header (see api_upload below) and
# stamps the right Source string itself rather than trusting a client field.
DESKTOP_APP_CLIENT_HEADER = "X-Constructicon-Client"
DESKTOP_APP_CLIENT_VALUE = "desktop-app"

DESKTOP_APP_DIR = Path(__file__).resolve().parent.parent / "desktop_app"
# Separate from both desktop_app/ (source) and storage/ (capture-event
# files) on purpose — this is neither. One file, whoever uploads last wins;
# there's no versioning, just the current build.
DESKTOP_APP_BUILD_DIR = Path(__file__).resolve().parent.parent / "desktop_app_build"
DESKTOP_APP_BUILD_PATH = DESKTOP_APP_BUILD_DIR / "Constructicon-Uploader.zip"


def _has_thumbnail(row, spec=None):
    """Whether `row` should expose a /f/<slug>/thumb URL at all — true for
    any type whose spec has a thumbnail strategy (see core/object_types.py),
    even if the thumbnail hasn't actually been produced yet (get_thumbnail
    below fetches/generates it lazily on first request). False for types
    with ThumbnailSource.NONE — a plain document post (no file at all) or,
    since #28, an uploaded audio file (a real file, but no visual frame to
    show as a thumbnail). Dispatches purely off the spec rather than
    short-circuiting on "row has a filename", since that assumption (true
    for image/pdf/stl/psd/svg/eps, whose uploaded-file types always have
    *some* visual to show) no longer holds once a type can have a stored
    file with nothing image-like to derive a thumbnail from."""
    spec = spec or object_types.get_object_type(row.get("media_type"))
    return spec.thumbnail_source != object_types.ThumbnailSource.NONE


def _to_public(row):
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    return {
        "slug": row["slug"],
        "url": f"/f/{row['slug']}",
        "thumb_url": f"/f/{row['slug']}/thumb",
        "filename": row["filename"],
        # filename is None for content-only rows (youtube/document posts —
        # see insert_content in core/db.py); the gallery cards need
        # something readable to show in its place rather than the literal
        # string "null".
        # row["display_name"] (#11) is a per-object override — set via
        # /api/image/<slug> or the constructicon_rename MCP tool — that takes
        # priority over the old filename/content_description/slug fallback
        # chain when present.
        "display_name": row.get("display_name") or row["filename"] or row.get("content_description") or row["slug"],
        # File-kind badge (issue #12) — driven entirely by the type's
        # ObjectTypeSpec (core/object_types.py) so gallery cards never need
        # an if/else on media_type; a new type registered there picks up a
        # badge automatically. row["icon"] (#11) is a per-object override
        # that takes priority over the type's generic badge_icon.
        "media_type": media_type,
        "type_label": spec.label,
        "type_icon": row.get("icon") or spec.badge_icon,
        "type_badge": spec.badge_text,
        # Whether the gallery/home cards should render the actual thumbnail
        # image (thumb_url above) or a generic file icon — driven by the
        # type's spec (same _has_thumbnail used server-side for the detail
        # page and project covers), not a hardcoded filename-extension
        # check, so a type with a generated thumbnail (a PDF's rendered
        # first page, once a stream/URL capture is wired up) picks this up
        # for free instead of always falling back to the file icon.
        "has_thumbnail": _has_thumbnail(row, spec),
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        "uploaded_at": row["timestamp"],
        # uploaded_by keeps the exact Source string (identity/filter key —
        # used by /api/gallery and search); uploaded_by_display is the short
        # grouping label (see core/db.py's source_group()) for compact card
        # UI, so a long Source sentence like "Hooptie J (me) — manual upload"
        # doesn't overflow a gallery card's small meta line. The full string
        # is only spelled out in full on the object detail page.
        "uploaded_by": row["tech"],
        "uploaded_by_display": db.source_group(row["tech"]),
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        "artifact_link": row["artifact_link"],
    }


def _youtube_embed_url(external_url):
    """Builds a canonical embed URL from whatever YouTube URL shape is
    stored in external_url. Returns None if it doesn't look like a YouTube
    link at all — the template falls back to a plain external-link CTA in
    that case rather than rendering a broken iframe. Video-ID extraction is
    centralized in object_types.extract_youtube_id so this and the static
    thumbnail URL (see core/object_types.py's youtube_thumbnail_url) can't
    drift apart."""
    video_id = object_types.extract_youtube_id(external_url)
    return f"https://www.youtube.com/embed/{video_id}" if video_id else None


def _friendly_date(epoch):
    """'%-d'-style formatting (no leading zero) without relying on the
    platform-specific %-d/%-e strftime extension, which isn't available on
    Windows — this runs cross-platform."""
    if not epoch:
        return None
    dt = datetime.fromtimestamp(epoch)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _friendly_datetime(epoch):
    if not epoch:
        return None
    dt = datetime.fromtimestamp(epoch)
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{_friendly_date(epoch)} at {hour12}:{dt.minute:02d} {ampm}"


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


def _to_object_detail(row):
    """Full detail-page shape for GET /object/<slug> — unlike _to_public,
    this works for any media_type, not just uploaded images. filename can be
    None (a youtube/document row with no local file — see insert_content in
    core/db.py), so nothing here assumes it's set.
    """
    filename = row.get("filename")
    is_file = bool(filename)
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    has_thumb = _has_thumbnail(row, spec)
    return {
        "slug": row["slug"],
        "media_type": media_type,
        "type_label": spec.label,
        "type_icon": spec.badge_icon,
        "type_badge": spec.badge_text,
        "ocr_capable": spec.ocr_capable,
        "filename": filename,
        "is_file": is_file,
        "is_image_file": is_file and Path(filename).suffix.lower() in IMAGE_SUFFIXES,
        # #28: drives the <audio controls> mini player branch in
        # object_detail.html. media_type-based rather than another
        # extension-suffix check (unlike is_image_file, kept as-is above)
        # since "audio" is registered ahead of _to_object_detail via
        # core/object_types.py and nothing here needs to know its exact
        # extensions.
        "is_audio_file": is_file and media_type == "audio",
        "url": f"/f/{row['slug']}" if is_file else None,
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb else None,
        "external_url": row.get("external_url"),
        "youtube_embed_url": _youtube_embed_url(row.get("external_url")) if media_type == "youtube" else None,
        "content_description": row.get("content_description"),
        "content_date_display": _friendly_date(row.get("content_date")),
        # #54: freeform per-type metadata (view/like/comment counts, full
        # description, uploading channel if not the owner's own — see
        # core/object_types.py's "youtube" metadata_fields and
        # scripts/full_youtube_channel_sync.py). {} for every row nothing
        # has ever written type_metadata for, same "always a dict, never
        # missing" contract as row["tags"].
        "type_metadata": row.get("type_metadata") or {},
        # See _to_public's matching comment — row["display_name"]/row["icon"]
        # (#11) are per-object overrides that win over the generic fallbacks.
        "display_name": row.get("display_name") or filename or row.get("content_description") or row["slug"],
        "icon": row.get("icon") or spec.badge_icon,
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        # #47: current project membership — a project selector needs to
        # show what's already attached, not just a blank picker, and (per
        # #51's backfill) an object can now belong to a project it was
        # never uploaded with. _to_project_option is the same slim shape
        # the upload drawer's dropdown already uses.
        "projects": [_to_project_option(p) for p in db.list_projects_for_post(row["slug"])],
        "uploaded_at": row["timestamp"],
        "uploaded_at_display": _friendly_datetime(row["timestamp"]),
        # Full Source string, unshortened — this is the one place it's meant
        # to be spelled out in full (see _to_public for the compact/grouped
        # version used everywhere else).
        "source_display": row["tech"],
        "redacted": bool(row["redacted"]),
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
    }


def _flatten_tags(nodes):
    """Depth-first flatten of the list_tag_tree() structure — used to look
    up a tag by slug from a query param without a dedicated db helper."""
    flat = []
    for node in nodes:
        flat.append(node)
        flat.extend(_flatten_tags(node.get("children") or []))
    return flat


def _project_cover_url(cover_slug):
    """cover_slug references a capture_events row (see projects.cover_slug
    in core/db.py) — reuse the same thumb route the gallery uses for images.
    A row whose type has no thumbnail concept (e.g. a plain document post),
    or a missing/deleted/redacted slug, falls back to None so the template
    can render a placeholder instead of a broken image."""
    if not cover_slug:
        return None
    row = db.get_by_slug(cover_slug)
    if not row or row.get("redacted") or not _has_thumbnail(row):
        return None
    return f"/f/{row['slug']}/thumb"


def _to_project_card(project):
    return {
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        "status": project["status"],
        "cover_url": _project_cover_url(project.get("cover_slug")),
        # #56: front-page sort control needs a date to sort "Newest"/"Oldest"
        # by — created_at was already stored on every project row, just never
        # exposed to this card shape before.
        "created_at": project["created_at"],
    }


def _project_has_tag(project, member_slugs):
    return any(item["slug"] in member_slugs for item in db.list_project_items(project["id"]))


def _to_content_public(row):
    """Public shape for a project-item card. Broader than _to_public: a
    project can contain backfilled youtube/document posts as well as real
    uploaded files, and those have no filename/stored_filename to build a
    thumb from (see core/db.py's insert_content) — but every row, regardless
    of media_type, now gets its own local /object/<slug> detail page, so
    cards always link locally instead of bouncing straight to external_url.
    """
    is_file = bool(row.get("filename"))
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    has_thumb = _has_thumbnail(row)
    return {
        "slug": row["slug"],
        "title": row.get("content_description") or row.get("description") or row.get("filename") or row["slug"],
        "media_type": media_type,
        "type_icon": spec.badge_icon,
        "type_badge": spec.badge_text,
        "is_file": is_file,
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb and not row.get("redacted") else None,
        "link": f"/object/{row['slug']}",
        "external": not is_file,
        "tags": row["tags"],
        "content_date": row.get("content_date"),
        "timestamp": row.get("timestamp"),
    }


OCR_WATCHDOG_INTERVAL_SECONDS = 60
OCR_STALE_THRESHOLD_SECONDS = 600  # 10 min — well past normal queueing even under a big batch


def _refire_ocr(slug):
    # set_ocr_status(..., "pending") stamps a fresh ocr_started_at, so this
    # attempt gets its own staleness clock — without that, a row re-fired
    # here would still look exactly as stale to the watchdog on its very
    # next tick, and get fired again every interval instead of once.
    db.set_ocr_status(slug, "pending")
    threading.Thread(target=ocr.run_ocr, args=(slug,), daemon=True).start()


def _ensure_capture_thumbnail(slug):
    """For a CAPTURE-sourced type that ISN'T ocr_capable (STL today — a
    binary mesh format with no text worth OCR'ing), there's no OCR
    background task to piggyback a thumbnail render onto the way PDF's is
    (see core/ocr.py's _ocr_source_path, which calls
    thumbnails.ensure_thumbnail as a side effect of preparing an OCR
    source). Without this, get_thumbnail's own lazy-generate fallback below
    only fires for content-only rows (stored_filename is None), so a
    file-backed CAPTURE type would silently serve the raw original file
    instead of a real thumbnail on every request until someone happened to
    run backfill_thumbnails.py. Scheduled as its own background task,
    same spirit as OCR, so it doesn't block the upload response."""
    row = db.get_by_slug(slug)
    if row is not None:
        thumbnails.ensure_thumbnail(row)


async def _ocr_watchdog():
    while True:
        await asyncio.sleep(OCR_WATCHDOG_INTERVAL_SECONDS)
        try:
            stale = db.list_stale_pending_ocr(OCR_STALE_THRESHOLD_SECONDS)
            for row in stale:
                print(f"OCR watchdog: re-firing {row['slug']} — pending for over {OCR_STALE_THRESHOLD_SECONDS}s")
                _refire_ocr(row["slug"])
        except Exception as e:
            print(f"OCR watchdog error: {e!r}")


@app.on_event("startup")
async def startup():
    db.init_db()
    db.ensure_special_clients()
    # Self-heal: a redeploy/restart while OCR was still queued or running for
    # a row leaves it stuck at ocr_status="pending" forever otherwise, since
    # nothing else will ever retry it. Fired as background threads, not run
    # here directly — this must not block the app from starting up, which is
    # exactly what happened before this fix when several rows were stuck at
    # once (each blocking, one after another, on the way to accepting any
    # requests at all).
    stuck = db.list_pending_ocr()
    if stuck:
        print(f"re-running OCR for {len(stuck)} row(s) left pending by a prior process")
        for row in stuck:
            _refire_ocr(row["slug"])
    asyncio.create_task(_ocr_watchdog())


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
def home_page(request: Request, tag: str = ""):
    """Home is the gallery itself (left third) plus a curated Projects
    section (right two-thirds) — see README's Projects/tag-tree note for why
    projects and blog_tags are separate concepts. ?tag=<slug> filters the
    Projects section down to cards with at least one item under that tag or
    one of its descendants; the gallery pane (client-side, /api/gallery) is
    unaffected by it.
    """
    tag_tree = db.list_tag_tree()
    selected_tag = None
    if tag:
        selected_tag = next((t for t in _flatten_tags(tag_tree) if t["slug"] == tag), None)
    projects = db.list_projects()
    if selected_tag:
        member_slugs = {r["slug"] for r in db.list_posts_for_tag(selected_tag["id"], limit=10000)}
        projects = [p for p in projects if _project_has_tag(p, member_slugs)]
    # Owner name/initials for the combined gallery+upload pop-out's tab
    # (#17) — SOURCE_GROUPS[0] is the site's single-owner display label
    # (e.g. "Hooptie J (me)"); strip the "(me)" qualifier for the tab's
    # name text and derive initials from what's left ("Hooptie J" -> "HJ").
    _owner_label = db.SOURCE_GROUPS[0].split(" (")[0]
    _owner_initials = "".join(w[0] for w in _owner_label.split()[:2]).upper()
    # #41: uploads with no project membership at all — always shown on the
    # home page (not just in the hover pop-out) so the page never looks
    # empty/broken just because no projects exist yet or an upload wasn't
    # filed into one. See db.list_unfiled_items's docstring for the incident
    # this fixes.
    unfiled_items = [_to_public(r) for r in db.list_unfiled_items()]
    # #107: most recent uploads per media_type. Each type gets its own N
    # most recent items, independent of upload activity in other types. This
    # ensures every type that has any uploads appears in the Files widget tabs.
    # Embedded as JSON keyed by media_type so the Files widget can render
    # per-type tabs and fetch each type's own list without competing within a
    # shared global pool.
    recent_by_type = {
        mt: [_to_public(r) for r in rows]
        for mt, rows in db.list_recent_items_by_type().items()
    }
    return templates.TemplateResponse(
        request, "home.html",
        {
            "active": "home",
            "top_tags": tag_tree,
            "selected_tag_slug": tag or None,
            "projects": [_to_project_card(p) for p in projects],
            "owner_name": _owner_label,
            "owner_initials": _owner_initials,
            "unfiled_items": unfiled_items,
            "recent_by_type": recent_by_type,
        },
    )


@app.post("/api/delete-all")
def api_delete_all():
    """Wipe every capture_events row (and its files), plus tags and
    projects — a full reset. Stands in for imagerepo's old per-user
    'delete my uploads' button now that multi-user accounts are gone;
    single-owner site, so 'my uploads' and 'everything' are the same set.
    Development convenience while content/schema are still in flux, not a
    feature meant to stick around once the site has real content worth
    protecting."""
    rows = db.search(limit=100000)
    for row in rows:
        if row.get("stored_filename"):
            storage.delete_files(row["slug"], row["stored_filename"])
        db.delete_upload(row["slug"])
    conn = db.get_conn()
    conn.execute("DELETE FROM post_tags")
    conn.execute("DELETE FROM project_items")
    conn.execute("DELETE FROM projects")
    conn.execute("DELETE FROM blog_tags")
    conn.commit()
    conn.close()
    return JSONResponse({"deleted": len(rows)})


@app.post("/api/delete")
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


@app.post("/api/backup")
def api_backup():
    """Standalone backup safety net (#20) — zips the DB and every file in
    storage/ into a timestamped archive under core.backup.BACKUP_DIR, then
    prunes down to the most recent BACKUP_RETENTION_COUNT archives.

    Deliberately its own button/endpoint, not called from /api/delete-all or
    /api/delete: a backup that only ran as a side effect of a delete could
    be mistaken for "already backed up" when it wasn't (see #19's history).
    Triggered on demand only — no scheduled job here, see #20's discussion
    for that as separate future work."""
    try:
        info = backup.create_backup()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")
    return JSONResponse(info)


# API Keys (#55) — the admin pane's allowlist of settings keys it knows how
# to display/accept. A secret's storage/endpoints (below, and
# core.db.get_setting/has_setting/set_setting) are generic key/value, so a
# future second key (or any other app-level setting) only needs an entry
# here plus a labeled row in _admin_pane.html, not a schema change.
# thingiverse_app_token (#62): read-only Thingiverse API access token for
# pulling the owner's own public models — no user-auth flow needed on
# Thingiverse's side, so this is exactly the same "paste one static secret"
# shape as youtube_data_api_key. No storage/endpoint changes required; this
# confirms #55/#59's genericness holds for a second key.
KNOWN_SETTINGS = {
    "youtube_data_api_key": "YouTube Data API Key",
    "thingiverse_app_token": "Thingiverse App Token",
}


@app.get("/api/settings")
def api_get_settings():
    """Presence-only view of every known setting — never the actual value.
    {"youtube_data_api_key": true} means a key is stored, not what it is.
    This is deliberately the only way the admin pane's UI learns whether a
    setting exists; the real value is never sent to the browser, on this
    route or any other, after it's been saved (see api_set_setting)."""
    return JSONResponse({key: db.has_setting(key) for key in KNOWN_SETTINGS})


@app.post("/api/settings")
def api_set_setting(key: str = Form(...), value: str = Form("")):
    """Saves one named setting (or, given an empty value, clears it). `key`
    must be one of KNOWN_SETTINGS above — the storage layer is generic, but
    this endpoint only accepts keys the app actually knows how to use, so it
    can't become an arbitrary junk-drawer for an unauthenticated LAN app.
    Deliberately returns only the same presence flag GET /api/settings
    reports, never the value it was just given, so the browser can't get the
    real value echoed back to it after a save."""
    if key not in KNOWN_SETTINGS:
        raise HTTPException(status_code=400, detail=f"Unknown setting key: {key!r}")
    db.set_setting(key, value)
    return JSONResponse({key: db.has_setting(key)})


@app.get("/upload")
def upload_page_redirect():
    # Upload is now a pane on the home page, not its own screen.
    return RedirectResponse("/", status_code=308)


@app.get("/gallery", response_class=HTMLResponse)
def gallery_page_redirect(request: Request):
    # The gallery is now the home page itself — kept as an alias so old
    # links/bookmarks still land somewhere sensible.
    return RedirectResponse("/", status_code=308)


@app.get("/project/{slug}", response_class=HTMLResponse)
def project_detail_page(request: Request, slug: str):
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="not found")
    items = [_to_content_public(r) for r in db.list_project_items(project["id"])]
    return templates.TemplateResponse(
        request, "project_detail.html",
        {
            "project": project,
            "cover_url": _project_cover_url(project.get("cover_slug")),
            "items": items,
        },
    )


@app.get("/gallery/user/{uploader}", response_class=HTMLResponse)
def user_gallery_page(request: Request, uploader: str):
    rows = db.search(uploaded_by=uploader, limit=1000)
    items = [_to_public(r) for r in rows]
    return templates.TemplateResponse(
        request, "user_gallery.html",
        {"uploader": uploader, "uploader_display": uploader, "items": items},
    )


@app.get("/object/{slug}", response_class=HTMLResponse)
def object_detail_page(request: Request, slug: str):
    """Generic detail page for any capture_events row, regardless of
    media_type — image, youtube, document, or anything else. Canonical route
    for what used to be the image-only /image/<slug> page (see the redirect
    below); the template branches on item.media_type/is_file to render the
    right preview (uploaded image, embedded YouTube player, or plain text
    content) instead of assuming every row has an uploaded file.
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    item = _to_object_detail(row)
    full_url = str(request.base_url).rstrip("/") + item["url"] if item["is_file"] else None
    full_object_url = str(request.base_url).rstrip("/") + f"/object/{slug}"
    # #52: relations (core/db.py's add_relation/remove_relation, #16) are
    # type-agnostic — a plain slug-to-slug link with no media_type or
    # is_file check on the backend — so this used to gate the Related panel
    # on item["is_file"] was a leftover from before the object-type registry
    # existed (predating #15) that accidentally hid "Add related" for every
    # content-only row (youtube, document posts), not just non-file types.
    # Always computed now so every object type gets the same panel.
    related = [_to_public(r) for r in db.list_related(slug)]
    return templates.TemplateResponse(
        request, "object_detail.html",
        {"item": item, "full_url": full_url, "full_object_url": full_object_url, "related": related},
    )


@app.get("/image/{slug}")
def image_detail_redirect(slug: str):
    # /image/<slug> was the original imagerepo-era canonical route (image-only).
    # /object/<slug> replaced it so any bookmarked/hotlinked old URLs still resolve.
    return RedirectResponse(f"/object/{slug}", status_code=308)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    return templates.TemplateResponse(request, "account.html", {})


# --- API ---

@app.post("/api/upload")
async def api_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    description: str = Form(""),
    tags: str = Form("[]"),
    client: str = Form(""),
    project_id: str = Form(""),
    modified_at: str = Form(""),
):
    # Which Source string a browser upload gets is decided server-side, not
    # by a client-supplied field — the desktop uploader app (see
    # desktop_app/constructicon_uploader/api.py) identifies itself with this
    # header on every request; the web upload drawer sends nothing extra, so
    # its absence is what marks a deliberate one-off drag-drop through the
    # browser UI.
    is_desktop_app = request.headers.get(DESKTOP_APP_CLIENT_HEADER) == DESKTOP_APP_CLIENT_VALUE
    user = db.SOURCE_AUTOMATED_UPLOAD if is_desktop_app else db.SOURCE_MANUAL_UPLOAD
    content = await file.read()
    file_size = len(content)
    source_modified_at = float(modified_at) / 1000 if modified_at else None
    dupe = db.find_duplicate(file.filename, file_size, source_modified_at)
    if dupe is not None:
        dupe_date = datetime.fromtimestamp(dupe["timestamp"]).strftime("%b %-d, %Y at %-I:%M %p")
        raise HTTPException(
            status_code=409,
            detail=f"Already uploaded by {dupe['tech']} on {dupe_date} — see /object/{dupe['slug']}",
        )
    # media_type from the uploaded file's extension — the only place this
    # decision has to be extension-based, since that's all /api/upload has
    # to go on. Everything downstream (thumbnail, OCR, badge, delete,
    # backup) dispatches off this media_type via core/object_types.py's
    # registry, not off the extension again.
    media_type = object_types.detect_media_type(file.filename)
    if media_type is None:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {Path(file.filename).suffix}")
    try:
        slug, stored_filename = storage.save_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    spec = object_types.get_object_type(media_type)
    db.insert_upload(
        slug, file.filename, stored_filename, user,
        description=description, tags=tag_list,
        client=client or None,
        file_size=file_size, source_modified_at=source_modified_at,
        media_type=media_type,
        ocr_status="pending" if spec.ocr_capable else None,
    )
    # Runs after this response is sent — OCR happens once the upload/tag step
    # is actually done, not as part of what the user is waiting on. The client
    # polls GET /api/image/{slug} to see ocr_status flip from "pending".
    if spec.ocr_capable:
        background_tasks.add_task(ocr.run_ocr, slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        # A CAPTURE-sourced type with no OCR pass to piggyback a thumbnail
        # render onto (STL today) still needs one generated somewhere —
        # see _ensure_capture_thumbnail above.
        background_tasks.add_task(_ensure_capture_thumbnail, slug)
    _attach_to_project(slug, project_id or None)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.post("/api/content")
async def api_create_content(
    request: Request,
    background_tasks: BackgroundTasks,
    media_type: str = Form(...),
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
    """
    spec = object_types.get_object_type(media_type)
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        raise HTTPException(status_code=400, detail=f"{spec.label} objects require a file upload — use /api/upload")
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
    content_date_epoch = float(content_date) if content_date else None
    slug = storage.make_slug()
    db.insert_content(
        slug, user, media_type,
        external_url=external_url or None,
        content_description=content_description or None,
        content_date=content_date_epoch,
        description=description, tags=tag_list,
        client=client or None,
        type_metadata=parsed_type_metadata,
    )
    row = db.get_by_slug(slug)
    if spec.ocr_capable and row["ocr_status"] == "pending":
        background_tasks.add_task(ocr.run_ocr, slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        background_tasks.add_task(_ensure_capture_thumbnail, slug)
    _attach_to_project(slug, project_id or None)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.get("/api/image/{slug}")
def api_get_image(request: Request, slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_to_public(row))


@app.post("/api/image/{slug}/ocr")
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


@app.post("/api/image/{slug}")
def api_update_image(
    request: Request,
    slug: str,
    description: str = Form(""),
    tags: str = Form("[]"),
    client: str = Form(""),
    display_name: str | None = Form(None),
    icon: str | None = Form(None),
    content_description: str | None = Form(None),
    type_metadata: str | None = Form(None),
):
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    row = db.update_tags(slug, description=description, tags=tag_list, client=client or None)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    # display_name/icon (#11) — no dedicated UI yet (see #24's "Coming soon
    # (#11)" admin-pane stub), but the field/endpoint exists so a "rename"
    # or "set icon" is at least possible by hand (a form POST here).
    if display_name is not None or icon is not None:
        row = db.rename_object(slug, display_name=display_name, icon=icon)
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
        row = db.update_content_metadata(slug, content_description=content_description, type_metadata=parsed_metadata)
    return JSONResponse(_to_public(row))


@app.post("/api/image/{slug}/redact")
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


@app.post("/api/image/{slug}/delete")
def api_delete_image(request: Request, slug: str):
    """Full delete — file and metadata both gone, no recovery."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.get("stored_filename"):
        storage.delete_files(slug, row["stored_filename"])
    db.delete_upload(slug)
    return JSONResponse({"deleted": True})


@app.post("/api/image/{slug}/related")
def api_add_related(request: Request, slug: str, related_slug: str = Form(...)):
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    if db.get_by_slug(related_slug) is None:
        raise HTTPException(status_code=404, detail="related image not found")
    db.add_relation(slug, related_slug)
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


@app.post("/api/image/{slug}/related/remove")
def api_remove_related(request: Request, slug: str, related_slug: str = Form(...)):
    db.remove_relation(slug, related_slug)
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


@app.post("/api/image/{slug}/project")
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
    _attach_to_project(slug, project_id)
    return JSONResponse([_to_project_option(p) for p in db.list_projects_for_post(slug)])


@app.post("/api/image/{slug}/project/remove")
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


@app.get("/api/image/{slug}/similar")
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


@app.get("/api/gallery")
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


@app.get("/downloads/constructicon-uploader-source.zip")
def download_desktop_app_source(request: Request):
    """Source only, not a built .app — py2app has to run on an actual Mac,
    which this server can't do (it's the same Linux/Docker box everything
    else runs on). Zipped fresh from disk on every request rather than a
    pre-built artifact, so it's never out of sync with what's actually in
    the repo."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DESKTOP_APP_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            arcname = Path("constructicon-uploader-source") / path.relative_to(DESKTOP_APP_DIR)
            zf.write(path, arcname=str(arcname))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=constructicon-uploader-source.zip"},
    )


@app.get("/api/account/desktop-app-build")
def api_get_desktop_app_build(request: Request):
    if not DESKTOP_APP_BUILD_PATH.exists():
        return JSONResponse({"exists": False})
    stat = DESKTOP_APP_BUILD_PATH.stat()
    return JSONResponse({"exists": True, "size": stat.st_size, "uploaded_at": stat.st_mtime})


@app.post("/api/account/desktop-app-build")
async def api_upload_desktop_app_build(request: Request, file: UploadFile = File(...)):
    """A tech who's built the app locally (py2app has to run on an actual
    Mac — this server can't build one itself) uploads the resulting zip
    here so everyone else can just download a working binary instead of
    building their own. No versioning: whoever uploads last is what
    everyone gets next."""
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Expected a .zip file (zip the built .app, don't upload it unzipped)")
    content = await file.read()
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise HTTPException(status_code=400, detail="That file isn't a valid zip archive")
    DESKTOP_APP_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_APP_BUILD_PATH.write_bytes(content)
    stat = DESKTOP_APP_BUILD_PATH.stat()
    return JSONResponse({"exists": True, "size": stat.st_size, "uploaded_at": stat.st_mtime})


@app.get("/downloads/constructicon-uploader.zip")
def download_desktop_app_build(request: Request):
    if not DESKTOP_APP_BUILD_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No built app has been uploaded yet — download the source zip and build it with Build.command, "
                   "or ask whoever last built one to upload it from account settings.",
        )
    return FileResponse(DESKTOP_APP_BUILD_PATH, media_type="application/zip", filename="Constructicon Uploader.zip")


@app.get("/api/clients")
def api_clients(request: Request):
    return JSONResponse(db.list_clients())


def _to_project_option(project):
    """Slim shape for the upload drawer's Project dropdown — just enough to
    populate a <select> and let the client hand project_id back on upload."""
    return {"id": project["id"], "slug": project["slug"], "title": project["title"], "status": project["status"]}


@app.get("/api/projects")
def api_projects(request: Request):
    """Populates the upload drawer's Project dropdown (#1) — every project,
    most-recently-updated first, same ordering list_projects() already uses
    for the home page's Projects column."""
    return JSONResponse([_to_project_option(p) for p in db.list_projects()])


@app.post("/api/projects")
def api_create_project(request: Request, title: str = Form(...)):
    """Creates a project from the upload drawer's "+ New project..." flow
    (#1) — distinct from scripts/seed_example_projects.py's one-off seeding,
    this is the first real UI-driven way to make a project.

    Also creates (or reuses) a root-level blog_tags row with the same name
    and links it via projects.tag_id, so every object later tagged to this
    project also becomes reachable through the ordinary tag-based browsing
    the rest of the site already has (see core/db.py's create_project
    docstring and README's "tied to the site tags" note) — not a parallel
    system, just handing the existing tag tree a project-shaped entry point.
    """
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name can't be empty")
    tag = db.get_or_create_tag(title, parent_id=None)
    project = db.create_project(title, tag_id=tag["id"])
    return JSONResponse(_to_project_option(project))


@app.post("/api/projects/{project_id}")
def api_update_project(
    request: Request,
    project_id: str,
    title: str = Form(None),
    description: str = Form(None),
    cover_slug: str = Form(None),
    status: str = Form(None),
):
    """Updates a project's properties (issue #103). Allows setting any
    combination of title, description, cover_slug (slug of an attached item
    to use as the cover image), and status. Only overwrites fields that were
    passed; omitted fields are left unchanged. Returns the updated project
    in _to_project_option shape (same as the list endpoint)."""
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    updated = db.update_project(
        project_id,
        title=title,
        description=description,
        cover_slug=cover_slug,
        status=status,
    )
    return JSONResponse(updated or {})


def _attach_to_project(slug, project_id):
    """Shared by /api/upload and /api/content: adds the new row to the given
    project's curated item list (so it shows up on the project's own detail
    page) and, if that project has a linked tag (see api_create_project /
    core/db.py's create_project), also tags the row with it — the "tied to
    the site tags" half of #1, so the object surfaces through tag-based
    browsing too, not just the project page. A project_id that doesn't
    resolve to a real project (bad/stale value) is silently ignored rather
    than failing the whole upload over a cosmetic mismatch.

    Also auto-sets the project's cover_slug to this item's slug if the
    project currently has no cover (issue #103) — fires only once per
    project, on the first item it receives."""
    if not project_id:
        return
    project = db.get_project(project_id)
    if project is None:
        return
    db.add_item_to_project(project["id"], slug)
    if project.get("tag_id"):
        db.attach_tags(slug, [project["tag_id"]])
    # Auto-set cover to first item if project has no cover yet
    if not project.get("cover_slug"):
        db.update_project(project["id"], cover_slug=slug)


@app.get("/api/search")
def api_search(request: Request, query: str = "", tags: str = "", client: str = ""):
    tag_list = [t for t in tags.split(",") if t] or None
    results = db.search(query=query or None, tags=tag_list, client=client or None)
    return JSONResponse([_to_public(r) for r in results])


# --- Public hotlink (no auth — Hudu/Slack need to fetch this directly) ---

@app.get("/f/{slug}")
def get_file(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=410, detail="file was redacted (sensitive content) — metadata is still on the image page")
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(path, filename=row["filename"])


@app.get("/f/{slug}/thumb")
def get_thumbnail(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=410, detail="file was redacted (sensitive content)")
    if not storage.thumb_path_for(slug).exists() and not row.get("stored_filename"):
        # Content-only row (youtube and friends — see core/db.py's
        # insert_content) whose thumbnail hasn't been fetched/captured yet,
        # e.g. the background OCR pass hasn't run or its fetch failed
        # transiently. Try once, synchronously, so a first page load isn't
        # stuck with a permanently broken thumbnail just because of timing.
        thumbnails.ensure_thumbnail(row)
    path = storage.thumb_path_or_original(slug, row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(path)
