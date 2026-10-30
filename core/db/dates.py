"""The reads behind approximate item dates (#725) and the install's first upload.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import threading

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import timeline
from .conn import change_fingerprint, DB_PATH, get_conn, _table_exists


# --- #725: the install's first upload (timeline's cutoff for "real" file mtimes) ---------------
# core/timeline.py counts a file's own modified time as a real date only when it is from before the
# install's first upload. That is MIN(capture_events.timestamp): derived, never stored, so a fresh
# install and every throwaway test DB get their own. Cached per DB file until anything is written
# (change_fingerprint), so a grid of 2,000 rows costs two stat() calls per row, not a query.
_first_upload_cache = {"key": None, "value": None}
_first_upload_lock = threading.Lock()


def first_upload_epoch():
    """The install's first upload (UTC unix seconds), or None while it holds no items."""
    key = (str(DB_PATH), change_fingerprint())
    with _first_upload_lock:
        if _first_upload_cache["key"] == key and key[1] is not None:
            return _first_upload_cache["value"]
    conn = get_conn()
    try:
        value = conn.execute("SELECT MIN(timestamp) FROM capture_events").fetchone()[0]
    finally:
        conn.close()
    with _first_upload_lock:
        _first_upload_cache.update(key=key, value=value)
    return value


timeline.set_first_upload_provider(first_upload_epoch)


# --- #725: the reads behind core/item_dates.py (an undated item's approximate date) ----------
_DATE_COLS = ("ce.slug, ce.media_type, ce.sensitive, ce.redacted, ce.timestamp, ce.content_date, "
              "ce.display_date_override, ce.source_modified_at, "
              "CASE WHEN json_valid(ce.type_metadata) THEN json_extract(ce.type_metadata, '$.date_made') END AS date_made")


def date_lending_rows():
    """#725: (cards, links) for every card's REAL-dated range in ONE pass. `cards`: id, slug,
    title, writeup_slug and the Timeline start/end overrides of every card. `links`: one row per
    card membership with the item's date columns only (never its text or embedding): project_id,
    `link_order` (project_items.rowid: the first one per item is the card it was first filed into,
    as db.first_card_titles), the date columns, `date_made` (type_metadata.date_made) and
    `blank_doc` (an empty written post, #672: never part of a span)."""
    conn = get_conn()
    try:
        cards = [dict(r) for r in conn.execute(
            "SELECT id, slug, title, kind, writeup_slug, start_date_override, end_date_override FROM projects")]
        links = [dict(r) for r in conn.execute(
            f"SELECT pi.project_id, pi.rowid AS link_order, {_DATE_COLS}, "
            "CASE WHEN ce.media_type = 'document' AND json_valid(ce.type_metadata) "
            "THEN trim(COALESCE(json_extract(ce.type_metadata, '$.body'), '')) = '' ELSE 0 END AS blank_doc "
            "FROM project_items pi JOIN capture_events ce ON ce.slug = pi.post_slug")]
        return cards, links
    finally:
        conn.close()


def family_member_pairs():
    """#772: every (family_id, member_id) membership row of the group cards, in one query (the
    member-inclusive spans of core/item_dates.py). [] before the family_members table exists."""
    conn = get_conn()
    try:
        if not _table_exists(conn, "family_members"):
            return []
        return [(r["family_id"], r["member_id"]) for r in
                conn.execute("SELECT family_id, member_id FROM family_members")]
    finally:
        conn.close()


def item_date_rows():
    """#725: the date columns (as date_lending_rows) of every non-redacted item, for finding the
    undated ones without loading every row's text."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(f"SELECT {_DATE_COLS} FROM capture_events ce WHERE ce.redacted = 0")]
    finally:
        conn.close()
