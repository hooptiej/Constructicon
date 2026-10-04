"""Blog-entry and site-export routes (#547): /api/blog-entries* and /api/export/*."""

import json
from datetime import datetime
from pathlib import Path

from fastapi import Request, Form, HTTPException, APIRouter
from fastapi.responses import JSONResponse

from core import db, site_export
from web.shapes import _to_blog_entry_detail

router = APIRouter()


@router.get("/api/blog-entries")
def api_list_blog_entries(request: Request, status: str = ""):
    """List all blog entries, optionally filtered by status (draft/published/etc).
    Returns a list of entry metadata without hydrated projects/items."""
    entries = db.list_blog_entries(status=status if status else None)
    return JSONResponse([
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
    ])


@router.get("/api/blog-entries/{slug}")
def api_get_blog_entry(request: Request, slug: str):
    """Get a single blog entry by slug with full hydration: projects and items."""
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Blog entry not found")
    return JSONResponse(_to_blog_entry_detail(entry))


@router.post("/api/blog-entries")
def api_create_blog_entry(
    request: Request,
    title: str = Form(...),
    subtitle: str = Form(""),
    body: str = Form(""),
    status: str = Form("draft"),
    cover_slug: str = Form(None),
    content_date: str = Form(None),
):
    """Create a new blog entry. title is required; others are optional.
    content_date, if provided, is parsed from a timestamp string."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    # Parse content_date if provided
    parsed_content_date = None
    if content_date:
        try:
            parsed_content_date = float(content_date)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid content_date format")

    entry = db.create_blog_entry(
        title=title,
        subtitle=subtitle,
        body=body,
        status=status,
        cover_slug=cover_slug,
        content_date=parsed_content_date,
    )
    return JSONResponse(_to_blog_entry_detail(entry))


@router.post("/api/blog-entries/{slug}")
async def api_update_blog_entry(
    request: Request,
    slug: str,
    title: str | None = Form(None),
    subtitle: str | None = Form(None),
    body: str | None = Form(None),
    status: str | None = Form(None),
):
    """Update a blog entry. Only the fields present in the form are touched;
    title/subtitle/body/status default to None ("leave unchanged").

    cover_slug and content_date are tri-state, read from the raw form the same
    way #244 does for display_name/icon: a field absent from the form is left
    unchanged; present-but-empty ("") clears it; a value sets it. FastAPI's
    Form(None) collapses "absent" and "empty" to the same None, which is why
    these two are read via request.form() membership instead."""
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Blog entry not found")

    form_data = await request.form()

    cover_slug = ...  # Ellipsis => leave unchanged (db.update_blog_entry's sentinel)
    if "cover_slug" in form_data:
        raw_cover = form_data.get("cover_slug")
        cover_slug = raw_cover if raw_cover else None

    content_date = ...
    if "content_date" in form_data:
        raw_date = form_data.get("content_date")
        if raw_date:
            try:
                content_date = float(raw_date)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid content_date format")
        else:
            content_date = None

    updated = db.update_blog_entry(
        slug,
        title=title,
        subtitle=subtitle,
        body=body,
        status=status,
        cover_slug=cover_slug,
        content_date=content_date,
    )

    if updated is None:
        raise HTTPException(status_code=404, detail="Blog entry not found")

    return JSONResponse(_to_blog_entry_detail(updated))


@router.delete("/api/blog-entries/{slug}")
def api_delete_blog_entry(slug: str):
    """Delete a blog entry and its attached projects/items."""
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Blog entry not found")

    db.delete_blog_entry(slug)
    return JSONResponse({"deleted": True})


def _require_json_content_type(request: Request):
    """#558: JSON-body routes only accept Content-Type: application/json (415
    otherwise). A cross-site <form enctype="text/plain"> can smuggle a JSON-looking
    body with no preflight; requiring the JSON type forces a CORS preflight."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")


@router.put("/api/blog-entries/{slug}/projects")
async def api_set_blog_entry_projects(
    request: Request,
    slug: str,
):
    """Set the ordered list of projects attached to a blog entry.
    Body should be JSON: [{"project_id": ..., "note": "..."}, ...]"""
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Blog entry not found")

    _require_json_content_type(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array")

    # Convert to (project_id, note) tuples
    items = []
    for item in body:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Each item must be a dict")
        project_id = item.get("project_id")
        note = item.get("note", "")
        if project_id is None:
            raise HTTPException(status_code=400, detail="project_id is required")
        items.append((project_id, note))

    db.set_entry_projects(entry["id"], items)
    updated = db.get_blog_entry(slug)
    return JSONResponse(_to_blog_entry_detail(updated))


@router.put("/api/blog-entries/{slug}/items")
async def api_set_blog_entry_items(
    request: Request,
    slug: str,
):
    """Set the ordered list of items (posts) attached to a blog entry.
    Body should be JSON: [{"slug": "...", "note": "..."}, ...]"""
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Blog entry not found")

    _require_json_content_type(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array")

    # Convert to (post_slug, note) tuples
    items = []
    for item in body:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Each item must be a dict")
        post_slug = item.get("slug")
        note = item.get("note", "")
        if post_slug is None:
            raise HTTPException(status_code=400, detail="slug is required")
        items.append((post_slug, note))

    db.set_entry_items(entry["id"], items)
    updated = db.get_blog_entry(slug)
    return JSONResponse(_to_blog_entry_detail(updated))


# --- Site export (generate static site for deployment) ---

@router.post("/api/export/build")
async def api_export_build(request: Request):
    """Build a static website from the selected projects and blog entries.

    Request body (JSON):
    {
        "project_slugs": ["slug1", "slug2"] (optional, defaults to all active),
        "blog_entry_slugs": ["slug1", "slug2"] (optional, defaults to all ready),
        "site": {
            "title": "...",
            "tagline": "..."
        }
    }

    Returns the build report with project/entry/media counts and any warnings.
    Saves the submitted config to app settings for next time.
    """
    _require_json_content_type(request)
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        report = site_export.build_site(body)
        # Persist the config for next time
        db.set_setting("export_config", json.dumps(body))
        return JSONResponse(report)
    except Exception as e:
        print(f"Build failed: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            {"error": str(e)},
            status_code=500
        )


@router.get("/api/export/config")
def api_export_config():
    """Retrieve the saved export configuration. Returns the last successfully
    built config, or an empty object {} if none has been saved yet.
    """
    try:
        saved_json = db.get_setting("export_config")
        if saved_json:
            return JSONResponse(json.loads(saved_json))
        return JSONResponse({})
    except Exception:
        return JSONResponse({})


@router.get("/api/export/targets")
def api_export_targets():
    """Retrieve the configured GitHub Pages publish targets.
    Returns a dict mapping target names to {repo, branch}.
    Never returns the token.
    """
    default_targets = {
        "test": {"repo": "hooptiej/constructicon-export-test", "branch": "master"},
        "live": {"repo": "hooptiej/hooptiej.github.io", "branch": "master"}
    }
    try:
        targets_json = db.get_setting("pages_publish_targets")
        if targets_json:
            targets = json.loads(targets_json)
            return JSONResponse(targets)
        return JSONResponse(default_targets)
    except Exception:
        return JSONResponse(default_targets)


@router.post("/api/export/publish")
async def api_export_publish(request: Request):
    """Publish the current build to a GitHub Pages repository.

    Request body (JSON):
    {
        "target": "test" | "live"
    }

    Returns:
    {
        "target": "test" | "live",
        "repo": "owner/repo",
        "branch": "master",
        "commit": "abc123...",
        "files": 42,
        "pages_url": "https://..."
    }
    """
    _require_json_content_type(request)
    try:
        body = await request.json()
    except Exception:
        body = {}

    target = body.get("target")

    # Guard: token required
    token = db.get_setting("pages_publish_token")
    if not token:
        raise HTTPException(
            status_code=400,
            detail="GitHub Pages publish token not configured (see admin settings)"
        )

    # Guard: target must be known
    try:
        targets_json = db.get_setting("pages_publish_targets")
        if targets_json:
            targets = json.loads(targets_json)
        else:
            targets = {
                "test": {"repo": "hooptiej/constructicon-export-test", "branch": "master"},
                "live": {"repo": "hooptiej/hooptiej.github.io", "branch": "master"}
            }
    except Exception:
        targets = {
            "test": {"repo": "hooptiej/constructicon-export-test", "branch": "master"},
            "live": {"repo": "hooptiej/hooptiej.github.io", "branch": "master"}
        }

    if target not in targets:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown publish target: {target!r}"
        )

    target_config = targets[target]
    repo = target_config["repo"]
    branch = target_config["branch"]

    # Guard: current build must exist
    current_build = Path(__file__).resolve().parent.parent.parent / "exports" / "current"
    if not current_build.exists() or not list(current_build.iterdir()):
        raise HTTPException(
            status_code=400,
            detail="No current build available (build the site first)"
        )

    # Clean origin URL (saved in .git/config) vs. the token-bearing URL used only
    # for the actual network fetch/push (never persisted). See publish_build.
    clean_url = f"https://github.com/{repo}.git"
    auth_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    work_dir = Path(__file__).resolve().parent.parent.parent / "exports" / ".publish" / target

    try:
        report = site_export.publish_build(
            clean_url,
            branch,
            current_build,
            work_dir,
            auth_url=auth_url,
            commit_message=f"Publish site {datetime.now().isoformat()}",
            author=("Constructicon", "constructicon@localhost")
        )
    except Exception as e:
        error_msg = str(e)
        # Strip token from error message
        error_msg = error_msg.replace(token, "[REDACTED]")
        error_msg = error_msg.replace(f"x-access-token:{token}@", "x-access-token:[REDACTED]@")
        print(f"Publish failed for target {target}: {error_msg}")
        raise HTTPException(
            status_code=500,
            detail=f"Publish failed: {error_msg}"
        ) from e

    # Build pages URL
    if repo.endswith(".github.io"):
        # User/org pages
        pages_url = f"https://{repo[:-10]}/"
    else:
        # Project pages
        owner = repo.split("/")[0]
        repo_name = repo.split("/")[1]
        pages_url = f"https://{owner}.github.io/{repo_name}/"

    return JSONResponse({
        "target": target,
        "repo": repo,
        "branch": branch,
        "commit": report["commit"],
        "files": len(report["files"]),
        "pages_url": pages_url
    })
