"""The MCP tools of one area. list_items (which=redacted | restricted | brand_assets | tag | unfiled |
similar) and its aliases.

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server.shapes import TEXT_PREVIEW_CHARS, text_chars_arg
from mcp_server._common import _only_for
from mcp_server.listing import (_list_brand_assets, _list_redacted, _list_restricted, _posts_for_tag, _similar,
                                _unfiled)

from core.errors import InvalidInput


@mcp.tool(selector="which")
def constructicon_list_items(which: typing.Literal["redacted", "restricted", "brand_assets", "tag", "unfiled", "similar"],
                             tag: str | None = None, text_chars: int = TEXT_PREVIEW_CHARS, slug: str | None = None,
                             limit: int | None = None, cursor: str | None = None) -> list[dict] | dict:
    """List items of one kind (listed shape: text previews, big type_metadata values trimmed):
    "tag": everything tagged `tag` or any tag under it (walks the tag tree; tag required). PAGED.
    "unfiled": the items on no card, as the web's Unfiled page lists them (no brand assets, current
      revisions), newest first. PAGED.
    "similar": look-alikes of the item `slug` (required): same picture re-saved, or similar text;
      each with similarity_reason (visual | text | both) and similarity_score; best first. A list.
    "brand_assets": logos, icons and other brand assets, with is_brand_asset and brand_role.
    "redacted": every redacted item (the only listing that has them; admin).
    "restricted": every sensitive item (keys, certificates, items marked sensitive), each with
      `projects` [{title, slug}] and `sensitive_reason` (admin).
    PAGED lists answer {items, next_cursor, more, limit}: limit 1..500 (default 100); pass
    next_cursor back as cursor= for the next page (null = the end). A page can hold fewer than limit
    items while more is true (items you can't see are left out).
    E.g. constructicon_list_items(which="tag", tag="3D Printing", limit=200).
    """
    chars = text_chars_arg(text_chars)
    if which == "tag":
        _only_for('which="tag"', slug=(slug, None))
        if not (tag or "").strip():
            raise InvalidInput('which="tag" needs tag (a tag name).')
        return _posts_for_tag(tag, chars, limit, cursor)
    if which == "unfiled":
        _only_for('which="unfiled"', tag=(tag, None), slug=(slug, None))
        return _unfiled(chars, limit, cursor)
    if which == "similar":
        _only_for('which="similar"', tag=(tag, None), limit=(limit, None), cursor=(cursor, None))
        if not (slug or "").strip():
            raise InvalidInput('which="similar" needs slug (the item to find look-alikes of).')
        return _similar(slug, chars)
    _only_for(f'which="{which}"', tag=(tag, None), slug=(slug, None), limit=(limit, None), cursor=(cursor, None))
    if which == "redacted":
        return _list_redacted(chars)
    if which == "restricted":
        return _list_restricted(chars)
    if which == "brand_assets":
        return _list_brand_assets(chars)
    raise InvalidInput(f"Unknown which {which!r}: use tag, unfiled, similar, brand_assets, redacted or restricted.")


@mcp.tool(alias_of=("constructicon_list_items", "redacted"))
def constructicon_list_redacted() -> list[dict]:
    return _list_redacted()


@mcp.tool(alias_of=("constructicon_list_items", "restricted"))
def constructicon_list_restricted() -> list[dict]:
    return _list_restricted()
