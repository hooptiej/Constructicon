"""The MCP tools of one area. Blog entries: list / create / get / update (+ aliases).

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

from mcp_server.app import mcp
from mcp_server.shapes import _to_public_blog_entry

from core import mcp_tokens, db, blog, changes
from core.errors import InvalidInput, NotFound


@mcp.tool()
def constructicon_list_blog_entries(status: str | None = None) -> list[dict]:
    """All blog entries, most recently updated first; status filters (e.g. "draft", "ready")."""
    entries = db.list_blog_entries(status=status)
    return [
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
            **mcp_tokens.creator_fields(e.get("created_by_user_id"), e.get("created_via_token_id")),  # #778
        }
        for e in entries
    ]


@mcp.tool()
def constructicon_create_blog_entry(title: str, subtitle: str = "", body: str = "", status: str = "draft",
                                    cover_slug: str | None = None,
                                    content_date: float | str | None = None) -> dict:
    """Create a blog entry (its slug comes from the title). status: "draft" (default), "ready" ...
    cover_slug: an item as its cover. content_date: the date it is about, best as an ISO date
    ("2026-10-01"); don't hand-compute unix seconds (UTC midnight shows as the day before).
    Attach cards and items with constructicon_update_blog_entry. -> the entry + batch_id.
    """
    result = blog.create(title, subtitle=subtitle, body=body, status=status, cover_slug=cover_slug,
                         content_date=content_date)  # #541 phase D: logged + undoable
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_get_blog_entry(slug: str) -> dict | None:
    """One blog entry with its cards and items (each with its note and sort_order)."""
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")
    return _to_public_blog_entry(entry)


def _attachments(rows, key, what):
    """[(key value, note)] from [{key, note?}], refusing anything else (bad_request, nothing written)."""
    if isinstance(rows, (str, dict)) or not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise InvalidInput(f"{what} must be a list of {{\"{key}\": ..., \"note\": \"...\"}} objects.")
    return [(r.get(key), r.get("note", "")) for r in rows]


@mcp.tool()
def constructicon_update_blog_entry(slug: str, title: str | None = None, subtitle: str | None = None,
                                    body: str | None = None, status: str | None = None,
                                    cover_slug: str | None = None, content_date: float | str | None = None,
                                    clear_cover_slug: bool = False, clear_content_date: bool = False,
                                    projects: list[dict] | None = None, items: list[dict] | None = None) -> dict:
    """Edit a blog entry, every change in ONE batch. Left out (None) = left alone.
    clear_cover_slug / clear_content_date clear those two. content_date as in create.
    projects: the cards attached, in order, as a FULL replace: [{"project_id": <card id or
      slug>, "note": "..."}]; [] removes them all. items: the same for items: [{"slug": "...",
      "note": "..."}]. An unknown card or item is not_found (nothing written).
    -> the entry (with its cards and items) + batch_id.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")
    card_rows = _attachments(projects, "project_id", "projects") if projects is not None else None
    item_rows = _attachments(items, "slug", "items") if items is not None else None
    cover = None if clear_cover_slug else (cover_slug if cover_slug is not None else ...)
    cdate = None if clear_content_date else (content_date if content_date is not None else ...)
    entry_fields = any(v is not None for v in (title, subtitle, body, status, cover_slug, content_date)) or \
        clear_cover_slug or clear_content_date
    batch_id = changes.new_batch_id()
    with db.transaction():
        if entry_fields or (card_rows is None and item_rows is None):  # as before: an update bumps updated_at
            blog.update(entry["id"], title=title, subtitle=subtitle, body=body, status=status,
                        cover_slug=cover, content_date=cdate, batch_id=batch_id)
        if card_rows is not None:
            blog.set_projects(entry["id"], card_rows, batch_id=batch_id)
        if item_rows is not None:
            blog.set_items(entry["id"], item_rows, batch_id=batch_id)
        fresh = db.get_blog_entry(entry["id"])
    return {**_to_public_blog_entry(fresh), "batch_id": batch_id}


@mcp.tool(alias_of=("constructicon_delete", "blog_entry"))
def constructicon_delete_blog_entry(slug: str) -> bool:
    blog.delete(slug)
    return True


@mcp.tool(alias_of=("constructicon_update_blog_entry", None))
def constructicon_set_blog_entry_projects(slug: str, items: list[dict]) -> dict | None:
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")

    # Convert dicts to (project_id, note) tuples. #541 phase D: logged + undoable; an unknown card
    # (id or slug) is not_found instead of a row no page can show.
    project_items = [(item.get("project_id"), item.get("note", "")) for item in items]
    result = blog.set_projects(entry["id"], project_items)
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool(alias_of=("constructicon_update_blog_entry", None))
def constructicon_set_blog_entry_items(slug: str, items: list[dict]) -> dict | None:
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")

    # Convert dicts to (post_slug, note) tuples. #541 phase D: logged + undoable; unknown file = not_found.
    post_items = [(item.get("slug"), item.get("note", "")) for item in items]
    result = blog.set_items(entry["id"], post_items)
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}
