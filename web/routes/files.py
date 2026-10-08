"""File routes (#547): /f/{slug} hotlinks + thumbnails, and the brand-asset
and wallpaper listings."""

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from core import db, item_title, object_types, storage, thumbnails
from web import content_security
from core import access_log, policy, roles
from web.roles import RoleRouter, requires

router = RoleRouter(default_role=roles.VIEWER)  # #557: routes without their own label are viewer


# --- Brand Assets (#350) ---

@router.get("/api/brand-assets")
def api_list_brand_assets(request: Request):
    """List all brand assets, grouped by role.

    Returns a list of brand asset dicts with slug, title, brand_role, and URLs."""
    assets = policy.filter_visible(db.list_brand_assets())  # #557
    return JSONResponse([
        {
            "slug": asset["slug"],
            "title": item_title.title_of(asset),
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
            "title": item_title.title_of(w),
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
    # #610: never active content on the app origin (nosniff; HTML/SVG/XML sandboxed, HTML a download)
    return content_security.serve_file(path, filename=row["filename"])


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
    # #610: a thumbnail is a generated raster (nosniff only); if it fell back to the original file,
    # that file gets the full active-content treatment, typed by the original's name.
    is_generated = path == storage.thumb_path_for(slug)
    return content_security.serve_file(path, type_name=None if is_generated else row.get("filename"))
