"""Image Repo web app: upload, gallery, and the public /f/{slug} hotlink
route.

No auth — this runs on a LAN-only dev server with no port forward, so the
network perimeter is the security boundary, not a login gate.
"""

import asyncio
import io
import json
import re
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

from core import db, ocr, similarity, storage

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
DESKTOP_APP_CLIENT_HEADER = "X-Imagerepo-Client"
DESKTOP_APP_CLIENT_VALUE = "desktop-app"

DESKTOP_APP_DIR = Path(__file__).resolve().parent.parent / "desktop_app"
# Separate from both desktop_app/ (source) and storage/ (capture-event
# files) on purpose — this is neither. One file, whoever uploads last wins;
# there's no versioning, just the current build.
DESKTOP_APP_BUILD_DIR = Path(__file__).resolve().parent.parent / "desktop_app_build"
DESKTOP_APP_BUILD_PATH = DESKTOP_APP_BUILD_DIR / "ImageRepo-Uploader.zip"


def _to_public(row):
    return {
        "slug": row["slug"],
        "url": f"/f/{row['slug']}",
        "thumb_url": f"/f/{row['slug']}/thumb",
        "filename": row["filename"],
        # filename is None for content-only rows (youtube/document posts —
        # see insert_content in core/db.py); the gallery cards need
        # something readable to show in its place rather than the literal
        # string "null".
        "display_name": row["filename"] or row.get("content_description") or row["slug"],
        "description": row["description"],
        "tags": row["tags"],
        "ticket_id": row["ticket_id"],
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


YOUTUBE_ID_RE = re.compile(r'(?:v=|/embed/|youtu\.be/)([A-Za-z0-9_-]{6,})')


def _youtube_embed_url(external_url):
    """Extracts the video ID from any of the URL shapes we might have stored
    (watch?v=, youtu.be/, or an already-embed URL) and builds a canonical
    embed URL. Returns None if external_url doesn't look like a YouTube link
    at all — the template falls back to a plain external-link CTA in that
    case rather than rendering a broken iframe."""
    if not external_url:
        return None
    m = YOUTUBE_ID_RE.search(external_url)
    return f"https://www.youtube.com/embed/{m.group(1)}" if m else None


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
    return {
        "slug": row["slug"],
        "media_type": media_type,
        "filename": filename,
        "is_file": is_file,
        "is_image_file": is_file and Path(filename).suffix.lower() in IMAGE_SUFFIXES,
        "url": f"/f/{row['slug']}" if is_file else None,
        "thumb_url": f"/f/{row['slug']}/thumb" if is_file else None,
        "external_url": row.get("external_url"),
        "youtube_embed_url": _youtube_embed_url(row.get("external_url")) if media_type == "youtube" else None,
        "content_description": row.get("content_description"),
        "content_date_display": _friendly_date(row.get("content_date")),
        "display_name": filename or row.get("content_description") or row["slug"],
        "description": row["description"],
        "tags": row["tags"],
        "ticket_id": row["ticket_id"],
        "client": row["client"],
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
    Rows with no local file (a youtube/document post used as a cover) or a
    missing/deleted slug fall back to None so the template can render a
    placeholder instead of a broken image."""
    if not cover_slug:
        return None
    row = db.get_by_slug(cover_slug)
    if not row or not row.get("filename") or row.get("redacted"):
        return None
    return f"/f/{row['slug']}/thumb"


def _to_project_card(project):
    return {
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        "status": project["status"],
        "cover_url": _project_cover_url(project.get("cover_slug")),
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
    return {
        "slug": row["slug"],
        "title": row.get("content_description") or row.get("description") or row.get("filename") or row["slug"],
        "media_type": row.get("media_type") or "image",
        "is_file": is_file,
        "thumb_url": f"/f/{row['slug']}/thumb" if is_file and not row.get("redacted") else None,
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
    return templates.TemplateResponse(
        request, "home.html",
        {
            "active": "home",
            "top_tags": tag_tree,
            "selected_tag_slug": tag or None,
            "projects": [_to_project_card(p) for p in projects],
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
    related = [_to_public(r) for r in db.list_related(slug)] if item["is_file"] else []
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
    ticket_id: str = Form(""),
    client: str = Form(""),
    modified_at: str = Form(""),
):
    # Which Source string a browser upload gets is decided server-side, not
    # by a client-supplied field — the desktop uploader app (see
    # desktop_app/imagerepo_uploader/api.py) identifies itself with this
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
    try:
        slug, stored_filename = storage.save_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    is_image = Path(file.filename).suffix.lower() in storage.IMAGE_EXTENSIONS
    db.insert_upload(
        slug, file.filename, stored_filename, user,
        description=description, tags=tag_list,
        ticket_id=ticket_id or None, client=client or None,
        file_size=file_size, source_modified_at=source_modified_at,
        ocr_status="pending" if is_image else None,
    )
    # Runs after this response is sent — OCR happens once the upload/tag step
    # is actually done, not as part of what the user is waiting on. The client
    # polls GET /api/image/{slug} to see ocr_status flip from "pending".
    if is_image:
        background_tasks.add_task(ocr.run_ocr, slug)
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
    if not row.get("filename"):
        raise HTTPException(status_code=400, detail="This row has no uploaded file — OCR isn't available for it")
    if Path(row["filename"]).suffix.lower() not in storage.IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"OCR isn't available for {Path(row['filename']).suffix} files")
    db.set_ocr_status(slug, "pending")
    background_tasks.add_task(ocr.run_ocr, slug)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.post("/api/image/{slug}")
def api_update_image(
    request: Request,
    slug: str,
    description: str = Form(""),
    tags: str = Form("[]"),
    ticket_id: str = Form(""),
    client: str = Form(""),
):
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    row = db.update_tags(slug, description=description, tags=tag_list, ticket_id=ticket_id or None, client=client or None)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
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


@app.get("/downloads/imagerepo-uploader-source.zip")
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
            arcname = Path("imagerepo-uploader-source") / path.relative_to(DESKTOP_APP_DIR)
            zf.write(path, arcname=str(arcname))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=imagerepo-uploader-source.zip"},
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


@app.get("/downloads/imagerepo-uploader.zip")
def download_desktop_app_build(request: Request):
    if not DESKTOP_APP_BUILD_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No built app has been uploaded yet — download the source zip and build it with Build.command, "
                   "or ask whoever last built one to upload it from account settings.",
        )
    return FileResponse(DESKTOP_APP_BUILD_PATH, media_type="application/zip", filename="ImageRepo Uploader.zip")


@app.get("/api/clients")
def api_clients(request: Request):
    return JSONResponse(db.list_clients())


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
    path = storage.thumb_path_or_original(slug, row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(path)
