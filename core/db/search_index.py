"""The FTS5 search index (#543) and search() over it.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import re
import sqlite3
import time
import unicodedata

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import errors, policy
from .conn import get_conn, _row_to_dict
from .revisions import _not_superseded


# --- Search index (#543) ------------------------------------------------------------------
# ONE searchable text per item in an FTS5 table. `item_search_src` is the view that defines that
# text; `item_search` indexes it (rowid = capture_events.id). Columns, in bm25 weight order (a
# title match outranks a match inside OCR text):
#   display_name (+ short_title, #542), content_description, filename   every name an item has ever worn (a rename must
#                                                  not make the old filename unfindable; title_of()
#                                                  in core/item_title.py picks only ONE of them).
#                                                  #762: filename also holds its CamelCase/digit split.
#   tags          the names of its tag-tree tags (post_tags). #555: the tree is the one tag store; the
#                 `capture_events.tags` column is only a derived cache of the chips, so it is not read
#                 here (a card's linked tag the cache leaves out is still found, by its name).
#   cards         the titles of the cards (projects) the item belongs to (project_items)
#   description   the uploader's note
#   extracted_text  OCR / extracted text
# The table is CONTENTLESS with contentless_delete=1 (SQLite >= 3.43): it keeps the search index
# but no second copy of the OCR text, and a row can be deleted by rowid, so the sync triggers never
# need the old text (an external-content table's 'delete' command does, and breaks when a write is
# silently ignored, as INSERT OR IGNORE into post_tags/project_items is). Every trigger is AFTER a
# change and re-indexes exactly the items that change touches: delete their entry, re-insert from
# the view. The triggers fire on the tables, so the item service, undo, MCP, scripts and the delete
# paths are all covered without each knowing. The schema is created at every boot (both
# processes, idempotent); the one-time backfill is the `search_index_543` data migration.
SEARCH_COLUMNS = ("display_name", "content_description", "filename", "tags", "cards", "description",
                  "extracted_text")
SEARCH_WEIGHTS = (12.0, 12.0, 10.0, 8.0, 8.0, 4.0, 1.0)  # bm25 column weights, SEARCH_COLUMNS order
_SEARCH_COLS_SQL = ", ".join(SEARCH_COLUMNS)

# #762: a run-together filename ("ElCamino.jpeg", "OldTalonFR.jpeg", "Elcamino4.jpeg") is one word to
# the tokenizer, so "camino" or "talon" never matched its start. The filename column also carries the
# name split at its CamelCase and letter/digit boundaries ("El Camino jpeg", "Old Talon FR jpeg",
# "Elcamino 4 jpeg"; `_ - . ( )` become spaces), appended after the original so every old prefix
# match still holds. Only when the split inserted a boundary: a plain name adds nothing, so its
# ranking is unchanged. Pure SQL (a recursive CTE walking the name one character at a time), so no
# connection needs a Python function registered: every writer (web, MCP, undo, scripts, the sqlite3
# shell) fires the triggers safely. ASCII case only (GLOB [A-Z]); an accented capital doesn't split.
_FILENAME_BOUNDARY_SQL = (  # 1 when a word starts at character i of f (not counting `_ - . ( )`)
    "(i > 1 AND ("
    "(substr(f, i - 1, 1) GLOB '[a-z]' AND substr(f, i, 1) GLOB '[A-Z]') "
    "OR (substr(f, i - 1, 1) GLOB '[A-Za-z]' AND substr(f, i, 1) GLOB '[0-9]') "
    "OR (substr(f, i - 1, 1) GLOB '[0-9]' AND substr(f, i, 1) GLOB '[A-Za-z]') "
    "OR (substr(f, i - 1, 1) GLOB '[A-Z]' AND substr(f, i, 1) GLOB '[A-Z]' AND substr(f, i + 1, 1) GLOB '[a-z]')))")
_FILENAME_PARTS_SQL = f"""(SELECT CASE WHEN n > 0 THEN out ELSE '' END FROM (
    WITH RECURSIVE s(i, out, n, f) AS (
        SELECT 1, '', 0, COALESCE(ce.filename, '')
        UNION ALL
        SELECT i + 1,
               out || CASE WHEN substr(f, i, 1) IN ('_', '-', '.', '(', ')') THEN ' '
                           WHEN {_FILENAME_BOUNDARY_SQL} THEN ' ' || substr(f, i, 1)
                           ELSE substr(f, i, 1) END,
               n + {_FILENAME_BOUNDARY_SQL},
               f
        FROM s WHERE i <= length(f))
    SELECT out, n FROM s ORDER BY i DESC LIMIT 1))"""

_SEARCH_VIEW = f"""
CREATE VIEW IF NOT EXISTS item_search_src AS
SELECT ce.id AS id,
       TRIM(COALESCE(ce.display_name, '') || ' ' || COALESCE(ce.short_title, '')) AS display_name,
       COALESCE(ce.content_description, '') AS content_description,
       TRIM(COALESCE(ce.filename, '') || ' ' || {_FILENAME_PARTS_SQL}) AS filename,
       COALESCE((SELECT group_concat(name, ' ') FROM (
           SELECT bt.name AS name FROM post_tags pt JOIN blog_tags bt ON bt.id = pt.tag_id
           WHERE pt.post_slug = ce.slug)), '') AS tags,
       COALESCE((SELECT group_concat(title, ' ') FROM (
           SELECT p.title AS title FROM project_items pi JOIN projects p ON p.id = pi.project_id
           WHERE pi.post_slug = ce.slug)), '') AS cards,
       COALESCE(ce.description, '') AS description,
       COALESCE(ce.extracted_text, '') AS extracted_text
FROM capture_events ce
"""


def _search_sync_triggers():
    """name -> DDL for every trigger that keeps item_search current."""
    def reindex(where):
        return (f"DELETE FROM item_search WHERE rowid IN (SELECT id FROM item_search_src WHERE {where}); "
                f"INSERT INTO item_search(rowid, {_SEARCH_COLS_SQL}) "
                f"SELECT id, {_SEARCH_COLS_SQL} FROM item_search_src WHERE {where};")

    def of_slug(ref):
        return f"id IN (SELECT id FROM capture_events WHERE slug = {ref})"

    def of_tag(ref):
        return (f"id IN (SELECT ce.id FROM capture_events ce JOIN post_tags pt ON pt.post_slug = ce.slug "
                f"WHERE pt.tag_id = {ref})")

    def of_card(ref):
        return (f"id IN (SELECT ce.id FROM capture_events ce JOIN project_items pi ON pi.post_slug = ce.slug "
                f"WHERE pi.project_id = {ref})")

    cols = "display_name, short_title, content_description, filename, description, extracted_text"
    trig = {}

    def add(name, when, table, body):
        trig[name] = f"CREATE TRIGGER IF NOT EXISTS {name} {when} ON {table} BEGIN {body} END"

    # an item is created, edited (only the columns the index reads) or deleted
    add("item_search_ce_ai", "AFTER INSERT", "capture_events", reindex("id = NEW.id"))
    add("item_search_ce_au", f"AFTER UPDATE OF {cols}", "capture_events", reindex("id = NEW.id"))
    add("item_search_ce_ad", "AFTER DELETE", "capture_events", "DELETE FROM item_search WHERE rowid = OLD.id;")
    # a tag is attached/detached, an item is added to/removed from a card
    for table, short in (("post_tags", "pt"), ("project_items", "pi")):
        add(f"item_search_{short}_ai", "AFTER INSERT", table, reindex(of_slug("NEW.post_slug")))
        add(f"item_search_{short}_ad", "AFTER DELETE", table, reindex(of_slug("OLD.post_slug")))
    # a tag is renamed or deleted, a card is retitled or deleted: every item wearing it is re-indexed
    add("item_search_bt_au", "AFTER UPDATE OF name", "blog_tags", reindex(of_tag("NEW.id")))
    add("item_search_bt_ad", "AFTER DELETE", "blog_tags", reindex(of_tag("OLD.id")))
    add("item_search_pr_au", "AFTER UPDATE OF title", "projects", reindex(of_card("NEW.id")))
    add("item_search_pr_ad", "AFTER DELETE", "projects", reindex(of_card("OLD.id")))
    return trig


def _ensure_search_schema(conn):
    """Creates the FTS5 table, its source view and its sync triggers if absent. Idempotent and
    cheap, so both processes run it at every boot (the MCP server must be able to write items
    even when the web app hasn't run the backfill yet). Does NOT fill the index: that is the
    `search_index_543` migration (rebuild_search_index). #667: runs inside init_db's schema
    transaction, so two processes never create these objects at the same moment."""
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS item_search USING fts5({_SEARCH_COLS_SQL}, "
            "content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2')")
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            f"cannot create the search index table item_search: {e}. This needs SQLite >= 3.43 built with "
            f"FTS5; this Python has SQLite {sqlite3.sqlite_version} (#543).") from e
    # #542: the view and triggers are re-created when their definition changed (short_title joined
    # the display_name column), so an existing DB never keeps the old ones. sqlite_master stores the
    # statement without "IF NOT EXISTS"; compare with whitespace collapsed.
    def _norm(sql):
        return " ".join((sql or "").replace("IF NOT EXISTS ", "").split())

    stored = {r["name"]: r["sql"] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('view', 'trigger') AND name LIKE 'item_search%'")}
    if "item_search_src" in stored and _norm(stored["item_search_src"]) != _norm(_SEARCH_VIEW):
        for name in _search_sync_triggers():
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("DROP VIEW IF EXISTS item_search_src")
        stored = {}
    conn.execute(_SEARCH_VIEW)
    for name, ddl in _search_sync_triggers().items():
        if name in stored and _norm(stored[name]) != _norm(ddl):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(ddl)


def rebuild_search_index():
    """Re-reads every item into the search index: empties it and refills it from item_search_src.
    Safe to run any time, any number of times (the result is the same). Returns
    (items_indexed, seconds)."""
    conn = get_conn()
    t0 = time.monotonic()
    try:
        conn.execute("DELETE FROM item_search")
        conn.execute(f"INSERT INTO item_search(rowid, {_SEARCH_COLS_SQL}) "
                     f"SELECT id, {_SEARCH_COLS_SQL} FROM item_search_src")
        n = conn.execute("SELECT COUNT(*) FROM item_search_docsize").fetchone()[0]
        conn.commit()
        return n, time.monotonic() - t0
    finally:
        conn.close()


def check_search_index():
    """{items, indexed, missing, stale} comparing the index to the items (docsize holds one row per
    indexed rowid). Both lists empty means in step; the fix for drift is rebuild_search_index().
    A read."""
    conn = get_conn()
    try:
        have = {r[0] for r in conn.execute("SELECT id FROM item_search_docsize")}
        want = {r[0] for r in conn.execute("SELECT id FROM capture_events")}
        return {"items": len(want), "indexed": len(have), "missing": sorted(want - have), "stale": sorted(have - want)}
    finally:
        conn.close()


_SEARCH_WORD_RE = re.compile(r"[^\W_]+")
_SEARCH_MAX_WORDS = 16


def _search_match(query):
    """The FTS5 MATCH string for what a person typed, or None when it holds no searchable word.
    Every run of letters/digits becomes a quoted prefix term ("tun"* finds "Tuning"), all of them
    required (AND). Quoting each word means quotes, `*`, `-`, `:`, parentheses and the words AND /
    OR / NOT / NEAR are searched as plain text, never read as FTS5 syntax. Unicode letters work
    (the tokenizer folds accents, so "cafe" finds "café")."""
    words = _SEARCH_WORD_RE.findall(query or "")[:_SEARCH_MAX_WORDS]
    return " ".join(f'"{w}"*' for w in words) or None


# #762: what a result found by the filename fallback carries in `search_matched` (the API and the MCP
# pass it on as `matched`; a word-start hit has no key here and reads "word_start" there).
SEARCH_MATCHED_WORD_START = "word_start"
SEARCH_MATCHED_FILENAME = "inside_filename"
SEARCH_FALLBACK_MIN_CHARS = 3  # a shorter run-together needle ("el", "x1") would match nearly every name


def _search_fold(text):
    """Lower case, accents folded (as the FTS side's remove_diacritics), only letters and digits kept:
    "Élcamino_4.JPEG" -> "elcamino4jpeg". Used on both sides of the filename fallback."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(_SEARCH_WORD_RE.findall("".join(c for c in decomposed if not unicodedata.combining(c)))).casefold()


def _search_fallback_needle(query):
    """The query's words run together and folded ("el camino" -> "elcamino"), or None when too short.
    Only letters and digits survive, so `%`, `_` and every other character are never a wildcard."""
    needle = _search_fold(" ".join(_SEARCH_WORD_RE.findall(query or "")[:_SEARCH_MAX_WORDS]))
    return needle if len(needle) >= SEARCH_FALLBACK_MIN_CHARS else None


def _search_filename_fallback(conn, needle, clauses, params, limit):
    """#762: ids of the items whose filename CONTAINS `needle` (folded both sides), newest first,
    under exactly the caller's visibility/filter `clauses`, at most `limit`. Done in Python rather than
    with LIKE so the accent folding matches the FTS side; it only runs when word-start search found
    nothing, and reads two columns of the visible rows."""
    where = " AND ".join(clauses + ["ce.filename IS NOT NULL", "ce.filename != ''"])
    hits = []
    for r in conn.execute(f"SELECT ce.id AS id, ce.filename AS filename FROM capture_events ce WHERE {where} "
                          "ORDER BY ce.timestamp DESC, ce.id DESC", params):
        if needle in _search_fold(r["filename"]):
            hits.append(r["id"])
            if len(hits) >= limit:
                break
    return hits


def search(query=None, tags=None, uploaded_by=None, limit=50, include_redacted=False, include_brand=False,
           include_superseded=True, filename_fallback=False, uploaded_by_user_id=None):
    """Keyword/filter search over capture_events. With a `query`, best match first (#543: relevance,
    a hit in a name or tag outranks one inside OCR text; ties newest first). Without one, most recent
    first.

    #543: `query` is looked up in the item_search index (see "Search index" above): every name the
    item has (display_name, content_description, filename), its tag-tree tags (#555), its
    card titles, the uploader's note and the extracted text. Words are prefix-matched and ANDed; the
    match is on whole words/word starts, not on any substring. A query with no letters or digits in
    it ("***") finds nothing. `tags` (any-of tag names, any case, matched on the tag tree including
    every tag under a named one, #555) and `limit` are applied in SQL.

    #282: redacted rows are excluded by default, same as every other
    list/browse query here (list_unfiled_items, list_recent_items_by_type, ...) --
    a redacted item is reachable only by its direct /object/<slug> link
    and the admin pane's list_redacted() view. `include_redacted=True` is
    the escape hatch for a caller that uses this as "enumerate every row"
    rather than as a search -- today just the two delete-all paths, which
    must not orphan redacted rows. The maintenance scripts' own
    `db.search(limit=<huge>)` discovery passes deliberately keep the
    default: they either work on the stored file (which a redacted row no
    longer has -- thumbnail/EXIF backfills) or only touch youtube/URL rows,
    which have no stored file and so can never be redacted in the first
    place.

    #417: brand assets (is_brand_asset=1) are likewise excluded by default,
    so branding art lives only in the Brand Vault + its own project and
    doesn't clog general search/browse. `include_brand=True` re-includes
    them. Unlike include_redacted, the enumerate-everything callers must
    set include_brand=True explicitly (the two delete-all paths -- else a
    brand asset survives a full reset as an orphaned row + storage file --
    and every `db.search(limit=<huge>)` maintenance/discovery pass, since a
    brand asset is a real stored image that still needs thumbnail/date
    backfills, unlike a fileless redacted row).

    #762 `filename_fallback=True` (GET /api/search, MCP constructicon_search): when the word-start
    search finds NOTHING, look for the query's words run together inside the filenames ("el camino"
    finds "Elcamino4.jpeg"), case- and accent-insensitive, under exactly the same clauses (visibility,
    brand, superseded, uploader, tags, limit), newest first. Each such row carries
    `search_matched = SEARCH_MATCHED_FILENAME` so the caller can say so. Off by default: /api/gallery
    pairs search() with list_uploaders' word-start totals, which must agree.
    """
    match = None
    if query:
        match = _search_match(query)
        if match is None:
            return []
    conn = get_conn()
    try:
        clauses, params = [], []
        if not include_redacted:
            # #443: restricted types ride with redaction: the enumerate-
            # everything callers (include_redacted=True) still see them.
            # #603: the same browse clause hides flagged-sensitive items from anyone but their owner/an admin.
            clauses.append("ce.redacted = 0" + policy.sql_browse_clause("ce."))
        if not include_brand:
            clauses.append("ce.is_brand_asset = 0")
        if not include_superseded:
            # #477: browse views pass False so only the current revision of a chain lists;
            # search itself keeps finding old revisions (callers mark them superseded).
            clauses.append(_not_superseded("ce.").replace(" AND ", "", 1))
        if uploaded_by:
            # Matches either the exact Source string or rows whose Source starts
            # with `uploaded_by` as its group prefix (see source_group()) — lets
            # callers filter by either a full Source string or the short grouping
            # key the gallery views link with (e.g. "Hooptie J (me)").
            clauses.append("(ce.tech = ? OR ce.tech LIKE ? OR ce.tech LIKE ?)")
            params += [uploaded_by, f"{uploaded_by} —%", f"{uploaded_by} -%"]
        if uploaded_by_user_id is not None:
            # #560: the per-user gallery: the items whose recorded uploader is this account (never the
            # free-text Source label).
            clauses.append("ce.uploaded_by_user_id = ?")
            params.append(int(uploaded_by_user_id))
        if tags:
            # #543: in SQL, so the LIMIT below applies after the filter. #555: on the TAG TREE (the one
            # tag store): an item carrying a tag with one of these names (any case), or any tag under
            # one, anywhere in the tree. A card's linked tag counts (it is a tree tag, though no chip).
            wanted = list(dict.fromkeys(str(t).strip().lower() for t in tags if str(t).strip()))
            if not wanted:
                return []
            clauses.append(
                "EXISTS (SELECT 1 FROM post_tags pt WHERE pt.post_slug = ce.slug AND pt.tag_id IN ("
                "WITH RECURSIVE sub(id) AS (SELECT id FROM blog_tags WHERE lower(name) IN "
                f"({', '.join('?' * len(wanted))}) UNION SELECT bt.id FROM blog_tags bt JOIN sub ON bt.parent_id = sub.id) "
                "SELECT id FROM sub))")
            params += wanted
        base_clauses, base_params = list(clauses), list(params)  # #762: the fallback's exact filter
        if match:
            clauses.insert(0, "item_search MATCH ?")
            params.insert(0, match)
            weights = ", ".join(str(w) for w in SEARCH_WEIGHTS)
            sql = (f"SELECT ce.* FROM item_search JOIN capture_events ce ON ce.id = item_search.rowid "
                   f"WHERE {' AND '.join(clauses)} ORDER BY bm25(item_search, {weights}), ce.timestamp DESC LIMIT ?")
        else:
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT ce.* FROM capture_events ce {where} ORDER BY ce.timestamp DESC LIMIT ?"
        params.append(int(limit))
        try:
            rows = conn.execute(sql, params).fetchall()
            needle = _search_fallback_needle(query) if (match and not rows and filename_fallback) else None
            if needle:
                ids = _search_filename_fallback(conn, needle, base_clauses or ["1"], base_params, int(limit))
                if ids:
                    by_id = {r["id"]: r for r in conn.execute(
                        f"SELECT ce.* FROM capture_events ce WHERE ce.id IN ({','.join('?' * len(ids))})", ids)}
                    found = [_row_to_dict(by_id[i]) for i in ids if i in by_id]
                    for row in found:
                        row["search_matched"] = SEARCH_MATCHED_FILENAME
                    return found
        except sqlite3.OperationalError as e:
            raise errors.AppError("search_failed", f"search for {query!r} failed in SQLite: {e}", status=500,
                                  details={"query": query, "match": match}) from e
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
