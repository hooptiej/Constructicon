"""File routes (#547): /f/{slug} hotlinks + thumbnails, /downloads/*, and the brand-asset
and wallpaper listings."""

import io
import zipfile
from pathlib import Path

from fastapi import Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from core import db, object_types, storage, thumbnails
from web.common import DESKTOP_APP_BUILD_PATH, DESKTOP_APP_DIR
from core import access_log, policy, roles
from core.errors import AppError
from web.roles import RoleRouter, requires

router = RoleRouter(default_role=roles.VIEWER)  # #557: routes without their own label are viewer


@router.get("/downloads/constructicon-uploader-source.zip")
def download_desktop_app_source(request: Request):
    """Source only, not a built .app — py2app has to run on an actual Mac,
    which this server can't do (it's the same Linux/Docker box everything
    else runs on). Zipped fresh from disk on every request rather than a
    pre-built artifact, so it's never out of sync with what's actually in
    the repo.

    #601: the app image must carry desktop_app/ (mounted read-only by compose, or the copy baked
    into the image). Before this, a container without it served a valid but EMPTY zip. Now a
    source tree that doesn't hold the uploader package is a 503 with the reason, never an empty
    download."""
    files = [p for p in sorted(DESKTOP_APP_DIR.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts] if DESKTOP_APP_DIR.is_dir() else []
    if not any(p.relative_to(DESKTOP_APP_DIR).as_posix() == "constructicon_uploader/api.py" for p in files):
        raise AppError(
            "uploader_source_missing",
            "The uploader source isn't installed on this server (desktop_app/ is missing from the app "
            "container). Ask the administrator to mount ./desktop_app into the web service.",
            status=503,
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = Path("constructicon-uploader-source") / path.relative_to(DESKTOP_APP_DIR)
            zf.write(path, arcname=str(arcname))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=constructicon-uploader-source.zip"},
    )


@router.get("/downloads/constructicon-uploader.zip")
def download_desktop_app_build(request: Request):
    if not DESKTOP_APP_BUILD_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No built app has been uploaded yet — download the source zip and build it with Build.command, "
                   "or ask whoever last built one to upload it from account settings.",
        )
    return FileResponse(DESKTOP_APP_BUILD_PATH, media_type="application/zip", filename="Constructicon Uploader.zip")


# --- Brand Assets (#350) ---

@router.get("/api/brand-assets")
def api_list_brand_assets(request: Request):
    """List all brand assets, grouped by role.

    Returns a list of brand asset dicts with slug, title, brand_role, and URLs."""
    assets = policy.filter_visible(db.list_brand_assets())  # #557
    return JSONResponse([
        {
            "slug": asset["slug"],
            "title": asset.get("content_description") or asset.get("display_name") or asset["slug"],
            "brand_role": asset.get("brand_role"),
            "thumb_url": f"/f/{asset['slug']}/thumb",
            "file_url": f"/f/{asset['slug']}",
        }
        for asset in assets
    ])


@router.get("/api/wallpapers")
def api_list_wallpapers(request: Request):
    """List all wallpaper objects (#422) — everything tagged wallpaper /
    Desktop Picture, newest first. The shape the /wallpaper page consumes."""
    wallpapers = policy.filter_visible(db.list_wallpapers())  # #557
    return JSONResponse([
        {
            "slug": w["slug"],
            "title": w.get("content_description") or w.get("display_name") or w.get("filename") or w["slug"],
            "thumb_url": f"/f/{w['slug']}/thumb",
            "file_url": f"/f/{w['slug']}",
            "is_file": bool(w.get("stored_filename")),
            "uploaded_at": w.get("timestamp"),
            "content_date": w.get("content_date"),
        }
        for w in wallpapers
    ])


# --- Public hotlink (no login: Hudu/Slack/the static site fetch these directly) ---
# #467 step 2: restricted and redacted items need an admin session or the install token; anyone
# else gets 404, the same as a missing slug (policy.require_file).

@router.get("/f/{slug}", dependencies=requires(roles.PUBLIC))
def get_file(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    policy.require_file(row)  # #467 step 2: public, except restricted/redacted items (admin only, else 404)
    if row["redacted"]:
        raise HTTPException(status_code=410, detail="file was redacted (sensitive content) — metadata is still on the image page")
    policy.note_access(row, access_log.HOW_FILE)  # #604 follow-up 7: sensitive items only
    if not row.get("stored_filename"):
        # Content-only row (youtube/url/document — see core/db.py's
        # insert_content): there is no local file to serve. Without this
        # guard storage.path_for(None) raises TypeError and the route 500s
        # (#211), even though _to_public advertises /f/<slug> for every row.
        raise HTTPException(status_code=404, detail="this object has no uploaded file — see its /object page")
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(path, filename=row["filename"])


@router.get("/f/{slug}/thumb", dependencies=requires(roles.PUBLIC))
def get_thumbnail(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    policy.require_file(row)  # #467 step 2: public, except restricted/redacted items (admin only, else 404)
    if row["redacted"]:
        raise HTTPException(status_code=410, detail="file was redacted (sensitive content)")
    policy.note_access(row, access_log.HOW_THUMB)  # #604 follow-up 7: sensitive items only
    if not storage.thumb_path_for(slug).exists() and not row.get("stored_filename"):
        # Content-only row (youtube and friends — see core/db.py's
        # insert_content) whose thumbnail hasn't been fetched/captured yet,
        # e.g. the background OCR pass hasn't run or its fetch failed
        # transiently. Try once, synchronously, so a first page load isn't
        # stuck with a permanently broken thumbnail just because of timing.
        thumbnails.ensure_thumbnail(row)
    spec = object_types.get_object_type(row.get("media_type"))
    if spec.thumbnail_source != object_types.ThumbnailSource.UPLOADED_FILE and not storage.thumb_path_for(slug).exists():
        # #478: a CAPTURE/FETCH type's thumbnail is generated, never the file
        # itself. Try once (e.g. a capture the background task hasn't done);
        # if there's still nothing, 404 rather than falling back to the
        # original, which served a .docx/.zip/.stl to an <img> tag.
        if row.get("stored_filename"):
            thumbnails.ensure_thumbnail(row)
        if not storage.thumb_path_for(slug).exists():
            raise HTTPException(status_code=404, detail="no thumbnail for this item")
    path = storage.thumb_path_or_original(slug, row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(path)
