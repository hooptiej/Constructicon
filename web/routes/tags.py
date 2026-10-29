"""Tag management routes (#555): the tag tree, rename / merge / move / delete, the cleanup sweep,
and the item chip's remove. Thin adapters over core/tags.py (every write one change-log batch,
undoable with POST /api/changes/{batch}/undo); refusals come back in the shared error shape with
the service's own message (what was tried, that nothing changed, what to do instead)."""

from fastapi import Form
from fastapi.responses import JSONResponse

from core import policy, roles
from core import tags as tags_svc
from web.roles import RoleRouter, requires

router = RoleRouter(default_role=roles.EDITOR)  # #557: routes without their own label are editor


def _bool(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@router.get("/api/tags/tree", dependencies=requires(roles.VIEWER))
def api_tag_tree():
    """The tag tree the caller may see (private cards' and hidden tags left out), each node with
    `path`, `items` (tagged directly), `cards` (the cards it is the linked tag of) and `hobby`."""
    return JSONResponse(tags_svc.tree())


@router.get("/api/tags/lookup", dependencies=requires(roles.VIEWER))
def api_tag_lookup(name: str = ""):
    """Every visible tag with this name (any case), with its path and what uses it. 404 when none."""
    return JSONResponse(tags_svc.get_by_name(name))


@router.post("/api/tags/{tag_id}/rename")
def api_tag_rename(tag_id: int, name: str = Form(...), dry_run: str = Form("")):
    """Rename a tag everywhere (409 tag_name_conflict / tag_is_card_tag)."""
    return JSONResponse(tags_svc.rename(tag_id, name, dry_run=_bool(dry_run)).to_dict())


@router.post("/api/tags/{tag_id}/merge")
def api_tag_merge(tag_id: int, into: str = Form(...), dry_run: str = Form("")):
    """Merge this tag into `into` (an id or a name): its items and sub-tags move there, it is deleted."""
    return JSONResponse(tags_svc.merge(tag_id, into, dry_run=_bool(dry_run)).to_dict())


@router.post("/api/tags/{tag_id}/move")
def api_tag_move(tag_id: int, parent: str = Form(""), dry_run: str = Form("")):
    """Put the tag under `parent` (an id or a name; blank = the top level)."""
    return JSONResponse(tags_svc.move(tag_id, parent.strip() or None, dry_run=_bool(dry_run)).to_dict())


@router.post("/api/tags/{tag_id}/delete")
def api_tag_delete(tag_id: int, dry_run: str = Form("")):
    """Delete the tag and take it off its items (undoable). Refused for a tag a card or a group owns, and for
    a tag with sub-tags."""
    return JSONResponse(tags_svc.delete(tag_id, dry_run=_bool(dry_run)).to_dict())


@router.get("/api/tags/sweep")
def api_tag_sweep():
    """The cleanup list (orphan, empty and near-duplicate tags). Read only: nothing is deleted."""
    return JSONResponse({"entries": tags_svc.sweep(), "kinds": list(tags_svc.SWEEP_KINDS)})


@router.post("/api/tags/sweep/apply")
def api_tag_sweep_apply(tag_ids: list[int] = Form(...), dry_run: str = Form("")):
    """Delete exactly the picked tags from the cleanup list, as one undoable batch. The whole call is
    refused (409 sweep_refused, saying which and why) if any pick is no longer safe to delete."""
    return JSONResponse(tags_svc.sweep_apply(tag_ids, dry_run=_bool(dry_run)).to_dict())


@router.post("/api/image/{slug}/tags/{tag_id}/remove")
def api_item_tag_remove(slug: str, tag_id: int):
    """The item chip's x: take one tag off this item (undoable). #730: 404 when missing, 403 saying why
    for an item the caller can't see."""
    policy.writable_item(slug)  # #730: 404 if missing, 403 saying why if hidden
    res = tags_svc.detach(slug, tag_id)
    return JSONResponse({**res.to_dict(), "chips": tags_svc.chips(slug)})


@router.get("/api/image/{slug}/tags", dependencies=requires(roles.VIEWER))
def api_item_tags(slug: str):
    """The item's chips [{id, name, slug}] (its tag-tree tags, without the linked tags of the cards it is
    on, which show as the cards themselves)."""
    policy.viewable_item(slug)
    return JSONResponse(tags_svc.chips(slug))
