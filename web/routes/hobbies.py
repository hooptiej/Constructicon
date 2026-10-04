"""Hobby routes (#547): /api/hobbies and /api/hobby/*."""

from fastapi import Request, Form, HTTPException, APIRouter
from fastapi.responses import JSONResponse

from core import card_rules, cards, db
from web.shapes import _to_object_detail

router = APIRouter()


# --- Hobbies (#360) ---

@router.get("/api/hobbies")
def api_list_hobbies(request: Request):
    """List all hobbies (tags marked is_hobby=1) with their project counts.

    Returns a list of hobby tag dicts, each with `status` (active|inactive), `group_code`,
    and the computed mismatch `flags` (V2 cards 3.3; never stored)."""
    return JSONResponse([{**h, **cards.hobby_fields(h)} for h in db.list_hobbies()])


@router.post("/api/hobbies")
def api_create_hobby(request: Request, name: str = Form(...)):
    """Create an empty hobby from just a name.

    Creates a top-level blog_tags row with the name, marks it as a hobby
    with status='active', and returns the new hobby's id and slug."""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Hobby name can't be empty")

    tag = db.get_or_create_tag(name, parent_id=None)
    db.mark_tag_as_hobby(tag["id"], status="active")
    return JSONResponse({"id": tag["id"], "slug": tag["slug"]})


@router.get("/api/hobby/{id_or_slug}")
def api_get_hobby(request: Request, id_or_slug: str):
    """Get a hobby's full details: metadata, attached projects, and attached objects.

    Returns a dict with the hobby tag, its projects, and its objects (via post_tags)."""
    hobby = db.get_hobby(id_or_slug)
    if hobby is None:
        raise HTTPException(status_code=404, detail="hobby not found")

    projects = db.list_projects_for_hobby(hobby["id"])
    # Get all objects tagged with this hobby tag
    items = db.list_posts_for_tag(hobby["id"], include_descendants=False)

    return JSONResponse({
        **cards.hobby_fields(hobby),
        "projects": [
            {
                "id": p["id"],
                "slug": p["slug"],
                "title": p["title"],
                "status": card_rules.curator_status(p),
            }
            for p in projects
        ],
        "items": [_to_object_detail(item) for item in items],
    })


@router.post("/api/hobby/{id_or_slug}/status")
def api_set_hobby_status(request: Request, id_or_slug: str, status: str = Form(...)):
    """Set a hobby's manual Active/Inactive switch (V2 cards 3.3).

    `dormant` / `abandoned` are deprecated aliases for `inactive` (a `warnings` entry says
    so). A bad value is a CardError -> HTTP 422 {error:{code:'bad_hobby_activity'}}.
    Returns the updated hobby dict (with computed flags) plus `warnings`."""
    hobby = db.get_hobby(id_or_slug)
    if hobby is None:
        raise HTTPException(status_code=404, detail="hobby not found")

    result = cards.set_hobby_activity(hobby["id"], status)
    updated = db.get_hobby(hobby["id"])
    return JSONResponse({**updated, **cards.hobby_fields(updated), "warnings": result.warnings})


@router.post("/api/hobby/{id_or_slug}/group-code")
def api_set_hobby_group_code(request: Request, id_or_slug: str, group_code: str = Form(...)):
    """Edit a hobby's 2-4 char group code (V2 cards 3.9). Unique across hobbies."""
    hobby = db.get_hobby(id_or_slug)
    if hobby is None:
        raise HTTPException(status_code=404, detail="hobby not found")
    cards.set_group_code(hobby["id"], group_code)
    updated = db.get_hobby(hobby["id"])
    return JSONResponse({**updated, **cards.hobby_fields(updated)})


@router.post("/api/hobby/{id_or_slug}/add-project")
def api_add_project_to_hobby(request: Request, id_or_slug: str, project_id: str = Form(...)):
    """Add a project to a hobby.

    project_id can be an id (int) or slug (str).
    Returns the updated hobby's project list."""
    hobby = db.get_hobby(id_or_slug)
    if hobby is None:
        raise HTTPException(status_code=404, detail="hobby not found")

    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    db.add_project_to_hobby(project["id"], hobby["id"])

    projects = db.list_projects_for_hobby(hobby["id"])
    return JSONResponse([
        {
            "id": p["id"],
            "slug": p["slug"],
            "title": p["title"],
            "status": card_rules.curator_status(p),
        }
        for p in projects
    ])


@router.post("/api/hobby/{id_or_slug}/remove-project")
def api_remove_project_from_hobby(request: Request, id_or_slug: str, project_id: str = Form(...)):
    """Remove a project from a hobby.

    project_id can be an id (int) or slug (str).
    Returns the updated hobby's project list."""
    hobby = db.get_hobby(id_or_slug)
    if hobby is None:
        raise HTTPException(status_code=404, detail="hobby not found")

    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    db.remove_project_from_hobby(project["id"], hobby["id"])

    projects = db.list_projects_for_hobby(hobby["id"])
    return JSONResponse([
        {
            "id": p["id"],
            "slug": p["slug"],
            "title": p["title"],
            "status": card_rules.curator_status(p),
        }
        for p in projects
    ])
