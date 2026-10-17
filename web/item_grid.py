"""One file grid, configured per page (#553): templates/_item_grid.html + static/js/item-grid.js.

The four file grids (home's Files panel, /unfiled, an uploader's gallery, a hobby's loose objects)
used to share only the card renderer (static/js/cards.js) and the Sort (#622); each page hand-built
its own type filter, selection bar and bulk scripts, and each got a different subset. Now each page
route calls `build()` and passes the result to the template as `grid`; the include draws the same
controls everywhere and the JS drives them:

  * Sort (templates/_item_sort_options.html), a type filter (one <select>, with counts), the
    All / Unfiled / Filed toggle (not on /unfiled, where it would always say "Unfiled");
  * multi-select and the bulk actions (editors and admins only; a viewer gets no Select button, no
    bar and no `bulk` list): add to card, turn into a new card, tag, provenance, mark sensitive,
    delete. Each calls the existing endpoint and service op (POST /api/bulk/*, /api/delete,
    /api/projects/from-selection), so each is one change-log batch with an Undo bar;
  * paging: every grid is server-paged through GET /api/items/grid (web/files_feed.py), the page
    embeds only the first batch of its default view plus the per-type counts.

User-visible words the grid uses live in LABELS (one place, for the label rename, #700).
"""

from core import actor as actor_ctx, provenance_options, roles
from web import files_feed

# Bulk actions, in the order the bar shows them.
BULK_ACTIONS = ("add_to_card", "new_card", "tag", "provenance", "sensitive", "delete")
FILTERS = ("type", "filed")
ENDPOINT = "/api/items/grid"

LABELS = {
    "select": "Select",
    "add_to_card": "Add to card…",
    "new_card": "Turn into card…",
    "new_card_prompt": "New card name:",
    "tag": "+ Tag selection, press Enter",
    "provenance": "Set provenance…",
    "apply": "Apply",
    "sensitive": "Mark sensitive",
    "delete": "Delete selected",
    "clear": "Clear",
    "all_types": "All types",
    # #714: the Grid / List toggle (every grid)
    "layout": "Layout",
    "grid": "Grid",
    "list": "List",
    "grid_title": "Grid view: the cards",
    "list_title": "List view: one row per file",
}


def can_edit():
    """Editor or above: the bulk endpoints' role (web/routes/items.py and cards.py default to editor)."""
    return roles.at_least(roles.role_of(actor_ctx.current_actor()), roles.EDITOR)


def build(grid_id, source, show_all_revs, *, loaded=None, filters=FILTERS, bulk=BULK_ACTIONS,
          select_default=False, href_from=None, layouts=True, grid_class="cx-item-grid",
          empty_text="No files here yet.", count_noun=("upload", "uploads"), count_suffix="",
          filed_key=None, unfiled_lamp=True):
    """-> (grid config for _item_grid.html, how many older revisions are hidden/shown).

    `source` is a files_feed source tuple (files_feed.HOME, ("unfiled", None), ("user", label),
    ("hobby", slug)). `loaded` = an already computed files_feed.load() result, for a page that
    needs the rows itself (the hobby page's cover choices). `filed_key` is the localStorage key that
    remembers the toggle (home keeps its old one). `layouts`: the Grid / List toggle, on every grid
    since #714 (item-grid.js remembers the choice per grid id)."""
    by_type, n_sup, unfiled = loaded if loaded is not None else files_feed.load(show_all_revs, source)
    seed, meta = files_feed.initial(by_type, unfiled, show_all_revs, source)
    editable = can_edit()
    actions = [a for a in bulk if a in BULK_ACTIONS] if editable else []
    filters = [f for f in filters if f in FILTERS]
    total = sum(c["all"] for c in meta["counts"].values())
    grid = {
        "id": grid_id,
        "endpoint": ENDPOINT,
        "seed": seed,
        "meta": meta,
        "filters": filters,
        "bulk": actions,
        "can_edit": editable,
        "select_default": bool(select_default and actions),
        "href_from": href_from,
        "layouts": layouts,
        "grid_class": grid_class,
        "empty_text": empty_text,
        "count": {"one": count_noun[0], "other": count_noun[1], "suffix": count_suffix, "total": total},
        "filed_key": filed_key or f"constructicon.grid.{grid_id}.filed",
        "unfiled_lamp": unfiled_lamp,
        "labels": LABELS,
        "provenance_options": provenance_options.picker_options("file") if "provenance" in actions else [],
    }
    return grid, n_sup
