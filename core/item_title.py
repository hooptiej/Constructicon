"""The one answer to "what is this item called?" (#542).

Every surface that shows an item's name (cards, object page, project grid and timeline, piles,
MCP, search, the static export, the blog builder, the curation queue) calls `title_of(row)`.
The order is decided here once:

    display_name  (the owner's rename override, #11/#241)
    -> content_description  (the content's own title or caption, #587/#588)
    -> filename
    -> slug

`description` is never a title: it is the uploader's note about the file (see capture_events in
core/db.py), so it is shown as text, not as a name. `scripts/test_item_title_542.py` fails if a
hand-rolled chain of these fields reappears outside this module.

A leaf module (imports nothing from core) so core/cards.py, core/revisions.py and the object-type
previews can use it without a cycle. The row may be a raw capture_events row or any dict that
carries the same keys (a public item dict already holds the resolved title in `display_name`).
"""


def title_of(row):
    return (row.get("display_name") or row.get("content_description")
            or row.get("filename") or row.get("slug") or "")
