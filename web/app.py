"""Image Repo web app: upload, gallery, and the public /f/{slug} hotlink
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

# Attribution text stored in capture_events.tech when a client doesn't supply
# its own — this is a single-owner site, not a multi-tech tool, so there's no
# real user identity behind it anymore.
DEFAULT_UPLOADER = "hooptiej"

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
        "uploaded_by": row["tech"],
        "uploaded_by_display": row["tech"],
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        "artifact_link": row["artifact_link"],
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
    thumb or /image/ page from (see core/db.py's insert_content) — so this
    links out to external_url instead when there's no local file.
    """
    is_file = bool(row.get("filename"))
    return {
        "slug": row["slug"],
        "title": row.get("content_description") or row.get("description") or row.get("filename") or row["slug"],
        "media_type": row.get("media_type") or "image",
        "is_file": is_file,
        "thumb_url": f"/f/{row['slug']}/thumb" if is_file and not row.get("redacted") else None,
        "link": f"/image/{row['slug']}" if is_file else (row.get("external_url") or f"/image/{row['slug']}"),
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


@app.get("/image/{slug}", response_class=HTMLResponse)
def image_detail_page(request: Request, slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    item = _to_public(row)
    item["uploaded_at_display"] = datetime.fromtimestamp(item["uploaded_at"]).strftime("%b %-d, %Y at %-I:%M %p")
    full_url = str(request.base_url).rstrip("/") + item["url"]
    related = [_to_public(r) for r in db.list_related(slug)]
    return templates.TemplateResponse(
        request, "image_detail.html",
        {"item": item, "full_url": full_url, "related": related},
    )


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
    tech: str = Form(""),
):
    user = tech.strip() or DEFAULT_UPLOADER
    content = await file.read()
    file_size = len(content)
    source_modified_at = float(modified_at) / 1000 if modified_at else None
    dupe = db.find_duplicate(file.filename, file_size, source_modified_at)
    if dupe is not None:
        dupe_date = datetime.fromtimestamp(dupe["timestamp"]).strftime("%b %-d, %Y at %-I:%M %p")
        raise HTTPException(
            status_code=409,
            detail=f"Already uploaded by {dupe['tech']} on {dupe_date} — see /image/{dupe['slug']}",
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
    storage.delete_files(slug, row["stored_filename"])
    updated = db.mark_redacted(slug)
    return JSONResponse(_to_public(updated))


@app.post("/api/image/{slug}/delete")
def api_delete_image(request: Request, slug: str):
    """Full delete — file and metadata both gone, no recovery."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
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
