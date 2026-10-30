"""The file grids, a page at a time (#624; every grid since #553).

The panel used to embed every item in the page and sort, filter and tab over the full list in the
browser (~1.2 MB of inline JSON on a 1,800-item archive, growing with it). Now the server keeps the
full list and the browser asks for what it has scrolled to:

  * `initial()`: what home embeds. The first BATCH items of the default view (All files, Date: newest),
    per-type counts for the tabs and the All/Unfiled/Filed toggle, and the cursor for "more".
  * `page()`: one page of any view, behind GET /api/home/files. A view is (type tab, filed
    mode, sort). The same call serves the first page of every view the page didn't embed (each type
    tab, another sort, Unfiled only, ...) and every later page.

Both read the SAME visible list, built the way the page always built it: db.list_recent_items_by_type
(brand assets and redacted rows excluded, the browse clause), policy.filter_visible (restricted and
flagged items), then _split_revisions (current revisions only unless show_all_revs). The route layer
adds the role check; this module never sees an item the viewer may not.

Order is the order the old client produced: types merged alphabetically, then a STABLE sort by the
sort key, so ties keep the merge order. To add a sort, add one entry to SORTS here, the same entry to
static/js/item-sort.js (the grids that sort in the browser) and one <option> to
templates/_item_sort_options.html (every grid's Sort menu); scripts/test_item_sort_622.js checks the
three agree.

#553: the same feed serves all four file grids (templates/_item_grid.html + static/js/item-grid.js).
A grid names its SOURCE, the list it shows; everything after "which rows" (visibility, revisions, type
filter, the filed toggle, sorts, cursors) is shared:
  * "home"            every file (db.list_recent_items_by_type), the home Files panel;
  * "unfiled"         files on no card (db.list_unfiled_items), /unfiled;
  * "user:<username>" one ACCOUNT's uploads (capture_events.uploaded_by_user_id, #560), /gallery/user/<username>.
                      An old link naming a Source label instead of a user still works: it lists that label;
  * "label:<label>"   one Source label's uploads (db.search(uploaded_by=...)), /gallery/source/<label>;
  * "hobby:<slug>"    a hobby's loose objects (db.list_loose_hobby_objects), /hobby/<slug>.
Each source reads the rows the way its page always did, then policy.filter_visible and
_split_revisions, so no source shows an item the viewer may not see.

A cursor is an opaque token: the view it belongs to, the offset reached and the last slug served.
"Continue after that slug" survives an upload landing at the top between two pages; the offset is the
fallback when that slug has since gone. A cursor from a different view is refused (cursor_view_mismatch).
"""

import base64
import binascii
import json
import unicodedata

from core import install_config, item_dates, item_title, object_types, policy
from core import db
from core.errors import AppError, NotFound
from web.shapes import _card_items, _split_revisions

BATCH = 120          # what #517 draws first, and the page size of the endpoint
MAX_LIMIT = 500
FILED_MODES = ("all", "unfiled", "filed")
DEFAULT_FILED = "all"
DEFAULT_SORT = "date_newest"  # #622: the date on the card, newest first


# ICU's (and so String.localeCompare's) order of ASCII punctuation and symbols, after whitespace and
# before digits and letters. Read off node's Intl.Collator; the rest of Unicode falls back to code point.
_ICU_PUNCT = "_-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"


def _primary(ch):
    """(kind, rank) of one base character: whitespace < punctuation/symbols < digits < letters."""
    cat = unicodedata.category(ch)
    if cat[0] == "Z" or cat == "Cc":
        return (0, ord(ch))
    if ch in _ICU_PUNCT:
        return (1, _ICU_PUNCT.index(ch))
    if cat[0] in "PSC":
        return (1, 100 + ord(ch))
    if cat == "Nd":
        return (2, unicodedata.digit(ch))
    if cat[0] == "N":
        return (2, 100 + ord(ch))
    return (3, ord(ch))


def _az_key(row):
    """A-Z by the card's title, matching what the old client's String.localeCompare did: case and
    accents don't decide the order first (a < B < c, e < é < f), then accents, then lowercase
    before uppercase. Checked against Node's ICU on a seeded archive (scripts/test_home_paging_624.js
    --compare); exotic non-ASCII punctuation falls back to code point order."""
    decomposed = unicodedata.normalize("NFD", item_title.title_of(row))
    base = [c for c in decomposed if not unicodedata.combining(c)]
    folded = [(f, c.isupper(), len(c.casefold()) > 1) for c in base for f in c.casefold()]  # ß folds to ss
    return (tuple(_primary(f) for f, _, _ in folded),
            decomposed.casefold(),
            tuple((upper, expanded) for _, upper, expanded in folded))  # lowercase first; ss before ß


def _card_date_newest(row):
    """#622: the date the card shows (core/item_dates.py, the same value web/shapes.py makes
    card_date / card_date_epoch from), newest first, then the upload time to break ties. #725: an
    item that inherits its card's range sorts at the range's start; an undated one goes LAST (in
    either direction), by upload time among themselves."""
    return item_dates.sort_key(row, newest_first=True)


def _card_date_oldest(row):
    """The same, oldest first (undated still last)."""
    return item_dates.sort_key(row, newest_first=False)


# sort name -> (key function over a capture_events row, reverse). The same names, labels and order
# as static/js/item-sort.js (the client-sorted grids) and templates/_item_sort_options.html.
# "newest" / "oldest" keep their pre-#622 meaning (upload time) so old links and API callers don't
# change; "date_newest" (the card date) is the default view since #622.
SORTS = {
    "date_newest": (_card_date_newest, False),  # #725: the key itself is newest-first, undated last
    "date_oldest": (_card_date_oldest, False),
    "newest": (lambda r: r["timestamp"], True),
    "oldest": (lambda r: r["timestamp"], False),
    "az": (_az_key, False),
}


def _bad(code, message, **details):
    return AppError(code, message, status=400, details=details)


# --- sources (#553) ---------------------------------------------------------------------

HOME = ("home", None)


def parse_source(raw):
    """"home" | "unfiled" | "user:<username>" | "label:<label>" | "hobby:<slug>" -> (kind, arg). 400
    bad_source otherwise."""
    raw = (raw or "home").strip()
    kind, sep, arg = raw.partition(":")
    if kind in ("home", "unfiled") and not sep:
        return (kind, None)
    if kind in ("user", "label", "hobby") and arg.strip():
        return (kind, arg)
    raise _bad("bad_source", f"source {raw[:80]!r} is not one of: home, unfiled, user:<username>, label:<label>, "
                             "hobby:<slug>")


def resolve_gallery(who):
    """#560 (owner decision 2026-10-17): what /gallery/user/<who> and source user:<who> show.
    ("user", user row) when `who` is a username (any case): the files that account uploaded;
    else ("label", who) when it is a Source label some item carries (old links from before the
    per-user gallery: the label view, which says it is one); else None (the caller says so, never
    an empty page)."""
    user = db.get_user(username=who) if who else None
    if user:
        return ("user", user)
    if db.source_label_exists(who):
        return ("label", who)
    return None


def unknown_gallery_message(who):
    return (f"There is no user or Source label called {who!r}. A gallery is per account: "
            "/gallery/user/<username> (the username shown in Admin > Users).")


def source_name(source):
    """(kind, arg) -> the string parse_source reads back."""
    kind, arg = source
    return kind if arg is None else f"{kind}:{arg}"


def _source_rows(source):
    """The rows a non-home source lists, superseded revisions included (the caller splits them)."""
    kind, arg = source
    if kind == "unfiled":
        return db.list_unfiled_items(limit=10_000_000, include_superseded=True)
    if kind == "user":
        # #560: an account's uploads; an old Source-label link still lists the label. #563/#553: no row cap.
        found = resolve_gallery(arg)
        if found is None:
            raise NotFound(unknown_gallery_message(arg))
        if found[0] == "user":
            return db.search(uploaded_by_user_id=found[1]["id"], limit=10_000_000)
        return db.search(uploaded_by=arg, limit=10_000_000)
    if kind == "label":
        if not db.source_label_exists(arg):
            raise NotFound(f"No item carries the Source label {arg!r}.")
        return db.search(uploaded_by=arg, limit=10_000_000)
    if kind == "hobby":
        hobby = db.get_hobby(arg)
        if hobby is None:
            raise NotFound(f"No {install_config.hobby_label().one_lc} {arg!r}.")
        return db.list_loose_hobby_objects(hobby["id"], include_superseded=True)
    raise _bad("bad_source", f"unknown source kind {kind!r}")


def group_by_type(rows):
    """[rows] -> {media_type: [rows]}, each type's rows in the order given."""
    by_type = {}
    for r in rows:
        by_type.setdefault(r.get("media_type") or "image", []).append(r)
    return by_type


def unfiled_slugs():
    return set(db.list_unfiled_slugs(include_superseded=True))


def load(show_all_revs, source=HOME):
    """A source's visible list: ({media_type: [rows]}, superseded rows hidden, unfiled slugs)."""
    if source[0] != "home":
        rows, n_sup = _split_revisions(policy.filter_visible(_source_rows(source)), show_all_revs)
        return group_by_type(rows), n_sup, unfiled_slugs()
    by_type = {}
    n_sup = 0
    for mt, rows in db.list_recent_items_by_type(limit_per_type=10000, include_superseded=True).items():
        rows, n = _split_revisions(policy.filter_visible(rows), show_all_revs)
        n_sup += n
        if rows:
            by_type[mt] = rows
    return by_type, n_sup, unfiled_slugs()


def type_labels(by_type):
    """{media_type: its registry label}, for the grid's type filter."""
    out = {}
    for mt in by_type:
        spec = object_types.get_object_type(mt)
        out[mt] = spec.label if spec else mt
    return out


def view_rows(by_type, tab, filed, sort, unfiled):
    """One view's rows in order. `tab` is "all" or a media type that has rows."""
    rows = [r for mt in (sorted(by_type) if tab == "all" else [tab]) for r in by_type.get(mt, [])]
    if filed != "all":
        rows = [r for r in rows if (filed == "unfiled") == (r["slug"] in unfiled)]
    key, reverse = SORTS[sort]
    return sorted(rows, key=key, reverse=reverse)  # stable, also when reversed


def counts(by_type, unfiled):
    """{media_type: {"all", "unfiled", "filed"}}: what each tab's toggle shows."""
    out = {}
    for mt, rows in by_type.items():
        n_un = sum(1 for r in rows if r["slug"] in unfiled)
        out[mt] = {"all": len(rows), "unfiled": n_un, "filed": len(rows) - n_un}
    return out


# --- cursors ---------------------------------------------------------------------------

def _view_key(tab, filed, sort, show_all_revs, source=HOME):
    # home keeps its pre-#553 key (a cursor issued before the change still continues); other sources
    # are prefixed, so a cursor never continues another grid's list
    key = f"{tab}|{filed}|{sort}|{'all' if show_all_revs else 'current'}"
    return key if source[0] == "home" else f"{source_name(source)}|{key}"


def make_cursor(view_key, offset, last_slug):
    raw = json.dumps({"v": view_key, "o": offset, "s": last_slug}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def read_cursor(token, view_key):
    """-> (offset, last slug). Refuses what this module didn't issue, or another view's."""
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode())
        view, offset, last = data["v"], data["o"], data["s"]
        if not (isinstance(view, str) and isinstance(last, str) and isinstance(offset, int)
                and not isinstance(offset, bool) and offset >= 0):
            raise ValueError("wrong field types")
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        raise _bad("bad_cursor", f"cursor {token[:40]!r} is not a cursor this endpoint issued "
                                 f"({type(e).__name__}: {e}). Ask for the first page again without a cursor.")
    if view != view_key:
        raise _bad("cursor_view_mismatch",
                   f"cursor was issued for view {view!r} (type|filed|sort|revisions) but this request is for "
                   f"{view_key!r}. A cursor only continues the view it came from.")
    return offset, last


def _next_cursor(rows, end, view_key):
    return make_cursor(view_key, end, rows[end - 1]["slug"]) if end < len(rows) else None


# --- what the page embeds ----------------------------------------------------------------

def initial(by_type, unfiled, show_all_revs, source=HOME):
    """-> (seed items, meta) for home.html. The seed is the first BATCH card items of the default
    view (All files, Date: newest): exactly what the panel draws first. Every other view, type tabs
    included, is fetched from page() on its first use. meta carries the per-type counts (tabs and
    the All/Unfiled/Filed numbers), the cursor that continues the seed view, and the seed view."""
    rows = view_rows(by_type, "all", DEFAULT_FILED, DEFAULT_SORT, unfiled)
    seed_rows = rows[:BATCH]
    meta = {
        "batch": BATCH,
        "seed": {"filed": DEFAULT_FILED, "sort": DEFAULT_SORT},
        "rev": "all" if show_all_revs else "",
        "counts": counts(by_type, unfiled),
        "cursor": _next_cursor(rows, BATCH, _view_key("all", DEFAULT_FILED, DEFAULT_SORT, show_all_revs, source)),
        "source": source_name(source),
        "labels": type_labels(by_type),
        "unfiled_total": len(unfiled),
        "unfiled_slugs": [r["slug"] for r in seed_rows if r["slug"] in unfiled],  # the seed's lamps only
    }
    return _card_items(seed_rows), meta


# --- one page of any view ------------------------------------------------------------------

def parse_limit(raw):
    if raw in (None, ""):
        return BATCH
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise _bad("bad_limit", f"limit must be a whole number from 1 to {MAX_LIMIT}, got {str(raw)[:40]!r}")
    if not 1 <= n <= MAX_LIMIT:
        raise _bad("bad_limit", f"limit must be from 1 to {MAX_LIMIT}, got {n}")
    return n


def page(by_type, unfiled, show_all_revs, tab, filed, sort, cursor, limit, source=HOME):
    """One page of a view: {"items", "unfiled_slugs", "total", "next_cursor", "counts", "labels"}
    (counts / labels: the source's per-type numbers as they are now, so a grid can refresh its
    filter after a bulk action, #553). Raises AppError (400, a specific code) for a parameter it
    can't use."""
    if sort not in SORTS:
        raise _bad("bad_sort", f"sort {sort!r} is not one of {', '.join(SORTS)}")
    if filed not in FILED_MODES:
        raise _bad("bad_filed", f"filed {filed!r} is not one of {', '.join(FILED_MODES)}")
    if tab != "all" and tab not in by_type:
        raise _bad("bad_type", f"type {tab!r} has no files you can see; the types here are: "
                               f"all, {', '.join(sorted(by_type)) or '(none)'}")
    limit = parse_limit(limit)
    view_key = _view_key(tab, filed, sort, show_all_revs, source)
    rows = view_rows(by_type, tab, filed, sort, unfiled)
    start = 0
    if cursor:
        offset, last = read_cursor(cursor, view_key)
        pos = next((i for i, r in enumerate(rows) if r["slug"] == last), None)
        start = pos + 1 if pos is not None else offset
    end = min(len(rows), start + limit)
    chunk = rows[start:end]
    return {
        "items": _card_items(chunk),
        "unfiled_slugs": [r["slug"] for r in chunk if r["slug"] in unfiled],
        "total": len(rows),
        "next_cursor": _next_cursor(rows, end, view_key),
        "counts": counts(by_type, unfiled),
        "labels": type_labels(by_type),
    }
