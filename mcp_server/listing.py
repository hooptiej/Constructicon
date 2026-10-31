"""Item lists the list tools share (#547 step 3; moved verbatim from server.py): the redacted,
restricted and brand-asset lists, #722's paging (limit + cursor), tag / unfiled / similar reads.
"""

import base64
import binascii
import json

from mcp_server.shapes import TEXT_PREVIEW_CHARS, _to_public_listed
from mcp_server._common import _viewable_item

from core import policy, db, tags as tags_svc, similarity
from core.errors import InvalidInput, NotFound


def _list_redacted(text_chars=TEXT_PREVIEW_CHARS):
    """Every redacted item (#282): the only listing that includes them (an admin listing)."""
    return [_to_public_listed(r, text_chars=text_chars) for r in db.list_redacted()]


def _list_restricted(text_chars=TEXT_PREVIEW_CHARS):
    """Every sensitive item (#443 types, #603 flags), with the cards each is on (an admin listing)."""
    return [
        {**_to_public_listed(r, text_chars=text_chars),
         "projects": [{"title": p["title"], "slug": p["slug"]} for p in db.list_projects_for_post(r["slug"])]}
        for r in db.list_restricted()
    ]


def _list_brand_assets(text_chars=TEXT_PREVIEW_CHARS):
    assets = policy.filter_visible(db.list_brand_assets())  # #557
    return [
        {**_to_public_listed(asset, text_chars=text_chars), "is_brand_asset": bool(asset.get("is_brand_asset")),
         "brand_role": asset.get("brand_role")}
        for asset in assets
    ]


# #722: paged item lists (which="tag" | "unfiled"): `limit` rows per page, read in SQL (never the
# whole list at once), and an opaque `cursor` naming the view and where the next page starts.
LIST_PAGE_DEFAULT = 100
LIST_PAGE_MAX = 500


def _page_limit(limit, default=None):
    if limit is None:
        return default or LIST_PAGE_DEFAULT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_PAGE_MAX:
        raise InvalidInput(f"limit must be a whole number from 1 to {LIST_PAGE_MAX}.", code="bad_limit")
    return limit


def _make_cursor(view, offset):
    raw = json.dumps({"v": view, "o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_cursor(cursor, view):
    """The offset a cursor continues from (0 for none). bad_cursor when it isn't one of ours;
    cursor_view_mismatch when it belongs to another list (another tag, unfiled vs a tag)."""
    if cursor is None or cursor == "":
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)))
        offset = data["o"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("bad offset")
    except (ValueError, TypeError, KeyError, binascii.Error) as e:
        raise InvalidInput("cursor is not a next_cursor this tool gave out. Start again without it.",
                           code="bad_cursor") from e
    if data.get("v") != view:
        raise InvalidInput("cursor belongs to another list (another which= or tag). Use the next_cursor of "
                           "this same list, or start again without it.", code="cursor_view_mismatch")
    return offset


def _paged(view, fetch, limit, cursor, text_chars, shape=None, default=None):
    """One page: {items (listed shape), next_cursor (null at the end), more, limit}. `fetch(limit,
    offset)` reads rows in SQL (the policy's browse clause applies there); the per-item policy then
    drops any it hides, so a page can hold fewer than `limit` items while `more` is still true.
    `shape(rows)` (optional) replaces the listed shape and must apply the item policy itself."""
    limit = _page_limit(limit, default)
    offset = _read_cursor(cursor, view)
    rows = fetch(limit + 1, offset)
    more = len(rows) > limit
    page = rows[:limit]
    items_out = shape(page) if shape is not None else         [_to_public_listed(r, text_chars=text_chars) for r in policy.filter_visible(page)]
    return {"items": items_out,
            "next_cursor": _make_cursor(view, offset + limit) if more else None, "more": more, "limit": limit}


def _tag_or_404(tag_name):
    tag = tags_svc.find_any(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise NotFound(f"No such tag: {tag_name!r}")
    return tag


def _posts_for_tag(tag_name, text_chars, limit=None, cursor=None):
    """One page of the items tagged `tag_name` or any tag under it (#722: paged in SQL)."""
    tag = _tag_or_404(tag_name)
    return _paged(f"tag:{tag['id']}", lambda n, o: db.list_posts_for_tag(tag["id"], limit=n, offset=o),
                  limit, cursor, text_chars)  # the policy's browse clause applies in db; filter_visible in _paged


def _unfiled(text_chars, limit=None, cursor=None):
    """#556: one page of the items on no card: the /unfiled page's list (db.list_unfiled_items, its
    default view: no brand assets, current revisions only), newest first."""
    return _paged("unfiled", lambda n, o: db.list_unfiled_items(limit=n, offset=o), limit, cursor, text_chars)


def _similar(slug, text_chars):
    """#556: look-alikes of one item, as GET /api/image/{slug}/similar finds them (core/similarity.py,
    read-only), each with similarity_reason (visual | text | both) and similarity_score."""
    _viewable_item(slug)
    out = []
    for m in similarity.find_similar(slug):
        row = db.get_by_slug(m["slug"])
        if row is None or not policy.can_view(row):  # #557
            continue
        out.append({**_to_public_listed(row, text_chars=text_chars), "similarity_reason": m["reason"],
                    "similarity_score": round(m["score"], 3)})
    return out
