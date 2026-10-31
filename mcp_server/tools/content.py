"""The MCP tools of one area. add_content, the set_sensitive alias, related (add / remove) and their aliases.

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server.shapes import _to_public
from mcp_server._common import _related_listed, _run_in_thread

from core import policy, db, ingest, items
from core.errors import InvalidInput


@mcp.tool()
def constructicon_add_content(media_type: str, external_url: str | None = None, content_description: str | None = None,
                           description: str = "", tags: list[str] | None = None,
                           uploaded_by: str = db.SOURCE_AUTHORED, sensitive: bool = False) -> dict:
    """Create an item with no uploaded file: a YouTube link, a web page, a written post.
    media_type must be a type that takes no file (e.g. "youtube"); external_url its link;
    content_description its own title. Files go through constructicon_upload. sensitive locks it
    from the start. uploaded_by is a free-text Source note, never who added it (the token's user).
    Text extraction runs in the background. Error: content_refused.
    """
    result = ingest.ingest_content(
        source=uploaded_by,
        run_background=_run_in_thread,
        media_type=media_type,
        external_url=external_url,
        content_description=content_description,
        description=description,
        tags=tags,
        sensitive=sensitive,
    )

    if result.error:
        error_msg = result.error
        if error_msg.endswith("require a file upload"):
            error_msg += " — use constructicon_upload"
        raise InvalidInput(error_msg, code="content_refused")
    return _to_public(result.row)


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_sensitive(slug: str, sensitive: bool = True, dry_run: bool = False) -> dict:
    result = items.set_sensitive(slug, sensitive, dry_run=dry_run)
    return {"ok": True, "dry_run": dry_run, "batch_id": result.batch_id, "slug": slug,
            "sensitive": result.data["sensitive"], "changed": result.data["changed"],
            "item": _to_public(result.item) if result.item else None}


def _relate(slug, other):
    policy.viewable_rows([slug, other])  # #709: missing = not_found, not visible = forbidden (says why)
    items.relate(slug, other)  # #541 phase C: one undoable batch (link + what it shares)
    return _related_listed(slug)


def _unrelate(slug, other):
    items.unrelate(slug, other)  # a door in core (#709)
    return _related_listed(slug)


@mcp.tool(selector="action")
def constructicon_related(slug: str, other: str, action: typing.Literal["add", "remove"] = "add") -> list[dict]:
    """Link two items as related (both directions), or unlink them.
    "add" (default): also shares their tags and card memberships both ways, as one batch (undo
      takes all of it back). "remove": the link only; what it shared stays.
    Returns the items now related to `slug` (listed shape). To read them:
    constructicon_get(slug, include=["related"]).
    """
    if action == "add":
        return _relate(slug, other)
    if action == "remove":
        return _unrelate(slug, other)
    raise InvalidInput(f"Unknown action {action!r}: use add or remove.")


@mcp.tool(alias_of=("constructicon_related", "add"))
def constructicon_add_related(slug: str, related_slug: str) -> list[dict]:
    return _relate(slug, related_slug)


@mcp.tool(alias_of=("constructicon_related", "remove"))
def constructicon_remove_related(slug: str, related_slug: str) -> list[dict]:
    return _unrelate(slug, related_slug)
