"""Image Repo web app: login (Microsoft SSO stub + email-OTP), upload, gallery,
and the public /f/{slug} hotlink route.
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

from core import auth, db, ocr, similarity, storage

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

SESSION_COOKIE = "imagerepo_session"

AVATAR_COLORS = ["#B98B5E", "#8B7355", "#C9A66B", "#7FA37C", "#9CAD5E", "#6B8FA3", "#A36B8F"]

DESKTOP_APP_DIR = Path(__file__).resolve().parent.parent / "desktop_app"
# Separate from both desktop_app/ (source) and storage/ (capture-event
# files) on purpose — this is neither. One file, whoever uploads last wins;
# there's no versioning, just the current build.
DESKTOP_APP_BUILD_DIR = Path(__file__).resolve().parent.parent / "desktop_app_build"
DESKTOP_APP_BUILD_PATH = DESKTOP_APP_BUILD_DIR / "ImageRepo-Uploader.zip"


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = auth.get_session(token)
        if session is not None:
            auth.touch_presence(session["email"])
            return session["email"]
    # Falls back to an API token (Authorization: Bearer ir_...) for the
    # desktop uploader and other unattended clients that can't hold a
    # browser session cookie.
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw_token = auth_header[7:].strip()
        token_hash = auth.hash_api_token(raw_token)
        user = db.get_user_by_token_hash(token_hash)
        if user is not None:
            db.touch_api_token_last_used(token_hash)
            return user
    return None


def require_login(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def _display_name(tech, profile):
    if profile and profile.get("display_name"):
        return profile["display_name"]
    return tech.split("@")[0] if "@" in tech else tech


def _to_public(row, profiles=None):
    profile = (profiles or {}).get(row["tech"])
    return {
        "slug": row["slug"],
        "url": f"/f/{row['slug']}",
        "thumb_url": f"/f/{row['slug']}/thumb",
        "filename": row["filename"],
        "description": row["description"],
        "tags": row["tags"],
        "ticket_id": row["ticket_id"],
        "client": row["client"],
        "uploaded_at": row["timestamp"],
        "uploaded_by": row["tech"],
        "uploaded_by_display": _display_name(row["tech"], profile),
        "uploaded_by_avatar_url": f"/avatar/{row['tech']}" if (profile or {}).get("has_avatar") else None,
        "uploaded_by_avatar_color": (profile or {}).get("avatar_color"),
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        "artifact_link": row["artifact_link"],
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
    db.backfill_users_from_uploads()
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


# --- Auth ---

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/gallery", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/auth/microsoft/start")
def microsoft_start():
    # Not wired up: needs an Entra app registration (client ID, secret,
    # redirect URI) in the Computer Cats tenant before this can redirect
    # to Microsoft's OAuth endpoint.
    return HTMLResponse(
        "<p>Sign-in with Microsoft isn't configured yet — needs an Entra app "
        "registration for this app. <a href='/login'>Back</a></p>",
        status_code=501,
    )


@app.post("/auth/email/request-code")
def request_code(request: Request, email: str = Form(...)):
    code = auth.request_code(email)
    return templates.TemplateResponse(request, "verify_code.html", {"email": email, "dev_code": code})


@app.post("/auth/email/verify")
def verify_code(request: Request, email: str = Form(...), code: str = Form(...)):
    if not auth.verify_code(email, code):
        return templates.TemplateResponse(
            request, "verify_code.html", {"email": email, "error": "That code is wrong or expired."}
        )
    db.record_user(email)
    token = auth.create_session(email)
    response = RedirectResponse("/gallery", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, max_age=auth.SESSION_TTL)
    return response


@app.post("/auth/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/gallery" if current_user(request) else "/login", status_code=303)


@app.get("/upload")
def upload_page_redirect():
    # Upload is now a pane on the gallery page, not its own screen.
    return RedirectResponse("/gallery", status_code=308)


@app.get("/gallery", response_class=HTMLResponse)
def gallery_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "gallery.html", {"active": "gallery", "user_email": user})


@app.get("/gallery/user/{uploader}", response_class=HTMLResponse)
def user_gallery_page(request: Request, uploader: str):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    profiles = db.list_profiles()
    rows = db.search(uploaded_by=uploader, limit=1000)
    items = [_to_public(r, profiles) for r in rows]
    return templates.TemplateResponse(
        request, "user_gallery.html",
        {"user_email": user, "uploader": uploader, "uploader_display": _display_name(uploader, profiles.get(uploader)), "items": items},
    )


@app.get("/image/{slug}", response_class=HTMLResponse)
def image_detail_page(request: Request, slug: str):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    profiles = db.list_profiles()
    item = _to_public(row, profiles)
    item["uploaded_at_display"] = datetime.fromtimestamp(item["uploaded_at"]).strftime("%b %-d, %Y at %-I:%M %p")
    full_url = str(request.base_url).rstrip("/") + item["url"]
    related = [_to_public(r, profiles) for r in db.list_related(slug)]
    return templates.TemplateResponse(
        request, "image_detail.html",
        {"user_email": user, "item": item, "full_url": full_url, "related": related},
    )


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    profile = db.get_profile(user) or {}
    return templates.TemplateResponse(
        request, "account.html",
        {"user_email": user, "profile": profile, "avatar_colors": AVATAR_COLORS},
    )


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
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
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
    return JSONResponse(_to_public(db.get_by_slug(slug), db.list_profiles()))


@app.get("/api/image/{slug}")
def api_get_image(request: Request, slug: str):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_to_public(row, db.list_profiles()))


@app.post("/api/image/{slug}/ocr")
def api_retry_ocr(request: Request, slug: str, background_tasks: BackgroundTasks):
    """Force a (re-)run of OCR — for images that never got it, or a lousy
    first pass worth retrying."""
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="File was redacted — there's no image left to OCR")
    if Path(row["filename"]).suffix.lower() not in storage.IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"OCR isn't available for {Path(row['filename']).suffix} files")
    db.set_ocr_status(slug, "pending")
    background_tasks.add_task(ocr.run_ocr, slug)
    return JSONResponse(_to_public(db.get_by_slug(slug), db.list_profiles()))


@app.post("/api/image/{slug}")
def api_update_image(
    request: Request,
    slug: str,
    description: str = Form(""),
    tags: str = Form("[]"),
    ticket_id: str = Form(""),
    client: str = Form(""),
):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    row = db.update_tags(slug, description=description, tags=tag_list, ticket_id=ticket_id or None, client=client or None)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_to_public(row, db.list_profiles()))


@app.post("/api/image/{slug}/redact")
def api_redact_image(request: Request, slug: str):
    """Delete the file only — sensitive content (e.g. a visible password) —
    but keep the metadata for future correlation."""
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    storage.delete_files(slug, row["stored_filename"])
    updated = db.mark_redacted(slug)
    return JSONResponse(_to_public(updated, db.list_profiles()))


@app.post("/api/image/{slug}/delete")
def api_delete_image(request: Request, slug: str):
    """Full delete — file and metadata both gone, no recovery."""
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    storage.delete_files(slug, row["stored_filename"])
    db.delete_upload(slug)
    return JSONResponse({"deleted": True})


@app.post("/api/image/{slug}/related")
def api_add_related(request: Request, slug: str, related_slug: str = Form(...)):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    if db.get_by_slug(related_slug) is None:
        raise HTTPException(status_code=404, detail="related image not found")
    db.add_relation(slug, related_slug)
    profiles = db.list_profiles()
    return JSONResponse([_to_public(r, profiles) for r in db.list_related(slug)])


@app.post("/api/image/{slug}/related/remove")
def api_remove_related(request: Request, slug: str, related_slug: str = Form(...)):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    db.remove_relation(slug, related_slug)
    profiles = db.list_profiles()
    return JSONResponse([_to_public(r, profiles) for r in db.list_related(slug)])


@app.get("/api/image/{slug}/similar")
def api_get_similar(request: Request, slug: str):
    """Auto-detected candidates — visual (perceptual hash) and/or semantic
    (text embedding) — distinct from the manually-curated Related panel.
    Each result carries similarity_reason ("visual"/"text"/"both") and
    similarity_score so the UI can label why it's suggested.
    """
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    profiles = db.list_profiles()
    matches = similarity.find_similar(slug)
    results = []
    for m in matches:
        row = db.get_by_slug(m["slug"])
        if row is None:
            continue
        item = _to_public(row, profiles)
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
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    profiles = db.list_profiles()
    uploaders = db.list_uploaders(query=query or None, client=client or None)
    groups = []
    for u in uploaders:
        items = db.search(query=query or None, client=client or None, uploaded_by=u["uploaded_by"], limit=per_user)
        groups.append({
            "uploaded_by": u["uploaded_by"],
            "uploaded_by_display": _display_name(u["uploaded_by"], profiles.get(u["uploaded_by"])),
            "total": u["total"],
            "items": [_to_public(r, profiles) for r in items],
        })
    return JSONResponse(groups)


@app.get("/api/online")
def api_online(request: Request):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    profiles = db.list_profiles()
    known = [
        {
            "email": email,
            "display_name": _display_name(email, profiles.get(email)),
            "avatar_url": f"/avatar/{email}" if (profiles.get(email) or {}).get("has_avatar") else None,
            "avatar_color": (profiles.get(email) or {}).get("avatar_color"),
        }
        for email in db.list_users()
    ]
    return JSONResponse({"online": auth.list_online(), "known": known})


@app.get("/api/profile")
def api_get_profile(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    profile = db.get_profile(user) or {"email": user, "display_name": None, "avatar_color": None, "has_avatar": False}
    return JSONResponse({**profile, "avatar_url": f"/avatar/{user}" if profile.get("has_avatar") else None})


@app.post("/api/account")
def api_update_account(
    request: Request,
    display_name: str = Form(""),
    avatar_color: str = Form(""),
):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    display_name = display_name.strip()
    if avatar_color and avatar_color not in AVATAR_COLORS:
        raise HTTPException(status_code=400, detail=f"'{avatar_color}' isn't one of the available avatar colors")
    profile = db.update_profile(user, display_name=display_name or None, avatar_color=avatar_color or None)
    return JSONResponse(profile)


@app.post("/api/account/avatar")
async def api_upload_avatar(request: Request, file: UploadFile = File(...)):
    """Accepts the already-cropped square PNG the client-side cropper exports
    (from an existing repo image or a fresh upload) and stores it directly on
    the user's profile row — this is a profile photo, not a capture event, so
    it doesn't go through storage.save_file()/insert_upload() at all."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    content = await file.read()
    if len(content) > storage.MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"Avatar image exceeds {storage.MAX_BYTES // (1024*1024)}MB limit")
    try:
        normalized = storage.normalize_avatar(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read that as an image: {e}")
    db.set_avatar_image(user, normalized)
    return JSONResponse({"avatar_url": f"/avatar/{user}"})


@app.post("/api/account/avatar/remove")
def api_remove_avatar(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    db.clear_avatar_image(user)
    return JSONResponse({"avatar_url": None})


@app.post("/api/account/delete-my-uploads")
def api_delete_my_uploads(request: Request):
    """Full delete of every capture-event this account uploaded — file and
    metadata, no recovery. Scoped strictly to the caller's own tech
    identity; there's no way to delete someone else's uploads this way."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    rows = db.search(uploaded_by=user, limit=100000)
    for row in rows:
        storage.delete_files(row["slug"], row["stored_filename"])
        db.delete_upload(row["slug"])
    return JSONResponse({"deleted": len(rows)})


@app.get("/api/account/tokens")
def api_list_tokens(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return JSONResponse(db.list_api_tokens(user))


@app.post("/api/account/tokens")
def api_create_token(request: Request, label: str = Form(...)):
    """Named, revocable API tokens for the desktop uploader (and any other
    unattended client) — a tech can hold several at once, one per machine,
    each independently revocable if a laptop is lost. The raw token is
    returned here once; only its hash is ever stored, so it can't be shown
    again after this response."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Token label is required — e.g. the machine name")
    raw_token = auth.generate_api_token()
    row = db.create_api_token(user, label, auth.hash_api_token(raw_token))
    return JSONResponse({**row, "token": raw_token})


@app.post("/api/account/tokens/revoke")
def api_revoke_token(request: Request, token_id: int = Form(...)):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    db.revoke_api_token(user, token_id)
    return JSONResponse(db.list_api_tokens(user))


@app.get("/downloads/imagerepo-uploader-source.zip")
def download_desktop_app_source(request: Request):
    """Source only, not a built .app — py2app has to run on an actual Mac,
    which this server can't do (it's the same Linux/Docker box everything
    else runs on). Zipped fresh from disk on every request rather than a
    pre-built artifact, so it's never out of sync with what's actually in
    the repo."""
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
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
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
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
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
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
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    if not DESKTOP_APP_BUILD_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No built app has been uploaded yet — download the source zip and build it with Build.command, "
                   "or ask whoever last built one to upload it from account settings.",
        )
    return FileResponse(DESKTOP_APP_BUILD_PATH, media_type="application/zip", filename="ImageRepo Uploader.zip")


@app.get("/avatar/{email}")
def get_avatar(request: Request, email: str):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    image = db.get_avatar_image(email)
    if image is None:
        raise HTTPException(status_code=404, detail="no avatar set for this user")
    return Response(content=image, media_type="image/png")


@app.get("/api/clients")
def api_clients(request: Request):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return JSONResponse(db.list_clients())


@app.get("/api/search")
def api_search(request: Request, query: str = "", tags: str = "", client: str = ""):
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    tag_list = [t for t in tags.split(",") if t] or None
    results = db.search(query=query or None, tags=tag_list, client=client or None)
    return JSONResponse([_to_public(r, db.list_profiles()) for r in results])


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
