"""The home Files panel, a page at a time (#624).

The panel used to embed every item in the page and sort, filter and tab over the full list in the
browser (~1.2 MB of inline JSON on a 1,800-item archive, growing with it). Now the server keeps the
full list and the browser asks for what it has scrolled to:

  * `initial()`: what home embeds. The first BATCH items of the default view (All files, Newest),
    per-type counts for the tabs and the All/Unfiled/Filed toggle, and the cursor for "more".
  * `page()`: one page of any view, behind GET /api/home/files. A view is (type tab, filed
    mode, sort). The same call serves the first page of every view the page didn't embed (each type
    tab, another sort, Unfiled only, ...) and every later page.

Both read the SAME visible list, built the way the page always built it: db.list_recent_items_by_type
(brand assets and redacted rows excluded, the browse clause), policy.filter_visible (restricted and
flagged items), then _split_revisions (current revisions only unless show_all_revs). The route layer
adds the role check; this module never sees an item the viewer may not.

Order is the order the old client produced: types merged alphabetically, then a STABLE sort by the
sort key, so ties keep the merge order. To add a sort (#622's card_date), add one entry to SORTS and
one <option> to home.html; nothing else knows the list of sorts.

A cursor is an opaque token: the view it belongs to, the offset reached and the last slug served.
"Continue after that slug" survives an upload landing at the top between two pages; the offset is the
fallback when that slug has since gone. A cursor from a different view is refused (cursor_view_mismatch).
"""

import base64
import binascii
import json
import unicodedata

from core import item_title, policy
from core import db
from core.errors import AppError
from web.shapes import _card_items, _split_revisions

BATCH = 120          # what #517 draws first, and the page size of the endpoint
MAX_LIMIT = 500
FILED_MODES = ("all", "unfiled", "filed")
DEFAULT_FILED = "all"
DEFAULT_SORT = "newest"


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


# sort name -> (key function over a capture_events row, reverse)
SORTS = {
    "newest": (lambda r: r["timestamp"], True),
    "oldest": (lambda r: r["timestamp"], False),
    "az": (_az_key, False),
}


def _bad(code, message, **details):
    return AppError(code, message, status=400, details=details)


def load(show_all_revs):
    """The visible list: ({media_type: [rows, newest first]}, superseded rows hidden, unfiled slugs)."""
    by_type = {}
    n_sup = 0
    for mt, rows in db.list_recent_items_by_type(limit_per_type=10000, include_superseded=True).items():
        rows, n = _split_revisions(policy.filter_visible(rows), show_all_revs)
        n_sup += n
        if rows:
            by_type[mt] = rows
    return by_type, n_sup, set(db.list_unfiled_slugs(include_superseded=True))


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

def _view_key(tab, filed, sort, show_all_revs):
    return f"{tab}|{filed}|{sort}|{'all' if show_all_revs else 'current'}"


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

def initial(by_type, unfiled, show_all_revs):
    """-> (seed items, meta) for home.html. The seed is the first BATCH card items of the default
    view (All files, Newest): exactly what the panel draws first. Every other view, type tabs
    included, is fetched from page() on its first use. meta carries the per-type counts (tabs and
    the All/Unfiled/Filed numbers), the cursor that continues the seed view, and the seed view."""
    rows = view_rows(by_type, "all", DEFAULT_FILED, DEFAULT_SORT, unfiled)
    seed_rows = rows[:BATCH]
    meta = {
        "batch": BATCH,
        "seed": {"filed": DEFAULT_FILED, "sort": DEFAULT_SORT},
        "rev": "all" if show_all_revs else "",
        "counts": counts(by_type, unfiled),
        "cursor": _next_cursor(rows, BATCH, _view_key("all", DEFAULT_FILED, DEFAULT_SORT, show_all_revs)),
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


def page(by_type, unfiled, show_all_revs, tab, filed, sort, cursor, limit):
    """One page of a view: {"items", "unfiled_slugs", "total", "next_cursor"}. Raises AppError
    (400, a specific code) for a parameter it can't use."""
    if sort not in SORTS:
        raise _bad("bad_sort", f"sort {sort!r} is not one of {', '.join(SORTS)}")
    if filed not in FILED_MODES:
        raise _bad("bad_filed", f"filed {filed!r} is not one of {', '.join(FILED_MODES)}")
    if tab != "all" and tab not in by_type:
        raise _bad("bad_type", f"type {tab!r} has no files you can see; the types here are: "
                               f"all, {', '.join(sorted(by_type)) or '(none)'}")
    limit = parse_limit(limit)
    view_key = _view_key(tab, filed, sort, show_all_revs)
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
    }
