"""Processing-pipeline state: the caption queue, OCR status, hashes and embeddings, extracted
text and text sources, and the caption worklists.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import json
import time

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import policy
from .conn import get_conn, _row_to_dict, transaction


# --- #388: processing-drawer status source -------------------------------
# Two thin read helpers returning the same column shape. The caller (app.py's
# /api/processing) derives the per-stage state, since that needs object_types.
_PROCESSING_COLS = (
    "slug, filename, display_name, short_title, content_description, media_type, "
    "ocr_status, type_metadata, (embedding IS NOT NULL) AS has_embedding, timestamp, "
    "sensitive, uploaded_by_user_id"  # #603: the item policy needs these (processing drawer)
)


def list_processing_candidates(within_seconds=86400, limit=500):
    """Recent, non-redacted rows that might still be mid-pipeline (OCR/caption/
    embed) — the candidate set for the processing drawer. Bounded by recency +
    LIMIT so a huge backlog can't make this unbounded; the caller filters to
    the ones actually still in flight."""
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_PROCESSING_COLS} FROM capture_events "
            "WHERE redacted=0 AND timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
            (time.time() - within_seconds, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_processing_rows_by_slugs(slugs):
    """Same shape as list_processing_candidates, for specific slugs — the
    frontend's session items, so the drawer can show them through to done even
    after they've settled and dropped out of the in-flight set."""
    slugs = [s for s in (slugs or []) if s]
    if not slugs:
        return []
    conn = get_conn()
    try:
        placeholders = ",".join("?" * len(slugs))
        rows = conn.execute(
            f"SELECT {_PROCESSING_COLS} FROM capture_events "
            f"WHERE redacted=0 AND slug IN ({placeholders})",
            slugs,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Caption queue (#549): the MCP process enqueues, the web process's worker drains ---

def enqueue_caption(slug, start_step=0, cascade=True, mode="caption", batch_id=None):
    """Queues one item for web's caption worker. mode 'caption' = caption it (and suggest a short
    title with it); 'title' = only propose a short title (#542 backfill), written into `batch_id`.
    A caption request replaces a queued title proposal for the same item (it suggests a title
    too); a title proposal never displaces a queued caption."""
    conn = get_conn()
    try:
        if mode == "title":
            conn.execute(
                "INSERT INTO caption_queue (slug, start_step, cascade, enqueued_at, mode, batch_id) "
                "VALUES (?, 0, 1, ?, 'title', ?) ON CONFLICT(slug) DO NOTHING",
                (slug, time.time(), batch_id))
            conn.commit()
            return
        conn.execute(
            "INSERT INTO caption_queue (slug, start_step, cascade, enqueued_at, mode, batch_id) "
            "VALUES (?, ?, ?, ?, 'caption', NULL) "
            "ON CONFLICT(slug) DO UPDATE SET start_step = excluded.start_step, "
            "cascade = excluded.cascade, enqueued_at = excluded.enqueued_at, mode = 'caption', batch_id = NULL",
            (slug, int(start_step), 1 if cascade else 0, time.time()))
        conn.commit()
    finally:
        conn.close()


def peek_caption_queue():
    """Oldest queued caption (not removed: it is dequeued only after it has run, so a crash
    mid-caption leaves it queued for the next boot). #542: a real caption goes before any queued
    short-title proposal, so a backfill never holds up a new upload's caption."""
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM caption_queue ORDER BY CASE mode WHEN 'title' THEN 1 ELSE 0 END, "
                         "enqueued_at, slug LIMIT 1").fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def dequeue_caption(slug):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM caption_queue WHERE slug = ?", (slug,))
        conn.commit()
    finally:
        conn.close()


def list_orphan_pending_captions(media_types, uploaded_before):
    """#592: caption-capable, non-redacted rows whose caption status is 'pending' but that have no
    caption_queue row and were added before `uploaded_before` (epoch seconds): a caption that was
    in flight when its process died. `media_types` = caption-capable type keys (NULL reads as image)."""
    types = sorted(set(media_types))
    if not types:
        return []
    type_ok = f"(c.media_type IN ({', '.join('?' * len(types))})" + (" OR c.media_type IS NULL OR c.media_type = ''" if "image" in types else "") + ")"
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT c.* FROM capture_events c WHERE c.redacted = 0 AND " + type_ok +
            " AND json_extract(c.type_metadata, '$.auto_caption_status') = 'pending'"
            " AND c.timestamp < ? AND NOT EXISTS (SELECT 1 FROM caption_queue q WHERE q.slug = c.slug)"
            " ORDER BY c.timestamp",
            (*types, float(uploaded_before))).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_pending_ocr():
    """Rows whose OCR never finished — normally just a brief in-flight window,
    but a process restart while a background OCR task was queued or running
    leaves a row stuck here forever unless something re-triggers it."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM capture_events WHERE ocr_status = 'pending'").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


OCR_UNSUPPORTED = "unsupported"  # #724: nothing OCR can read (no text source, no picture); not a failure


def _ocr_note_sql(note):
    """#724: SQL (and its parameter) that sets or clears type_metadata.ocr_note in place, in the
    same UPDATE as the status (json_set / json_remove, so a concurrent caption write to another
    key isn't lost). A row whose type_metadata isn't valid JSON is left as it is."""
    if note:
        return ("type_metadata = CASE WHEN json_valid(type_metadata) "
                "THEN json_set(type_metadata, '$.ocr_note', ?) ELSE type_metadata END"), (note,)
    return ("type_metadata = CASE WHEN json_valid(type_metadata) "
            "THEN json_remove(type_metadata, '$.ocr_note') ELSE type_metadata END"), ()


def set_ocr_status(slug, status, note=None):
    """Setting status to "pending" also stamps ocr_started_at — lets the
    watchdog measure how long the *current* attempt has been running,
    independent of how old the upload itself is.

    #724: `note` is the plain reason for status 'unsupported' (stored as type_metadata.ocr_note,
    shown as "Not compatible with OCR: <note>"); 'failed' may carry one too (a corrupt file says
    so: "corrupt or unreadable .doc: ..."); and (b2) so may 'done', when the text came with a
    warning (the reader failed and OCR read the picture instead, or only part of the file could
    be read). 'pending', or no note, clears a stale note."""
    if status == OCR_UNSUPPORTED and not note:
        raise ValueError("ocr_status 'unsupported' needs a note saying why")
    note_sql, note_args = _ocr_note_sql(note if status in (OCR_UNSUPPORTED, "failed", "done") else None)
    conn = get_conn()
    try:
        if status == "pending":
            conn.execute(f"UPDATE capture_events SET ocr_status = ?, ocr_started_at = ?, {note_sql} WHERE slug = ?",
                         (status, time.time(), *note_args, slug))
        else:
            conn.execute(f"UPDATE capture_events SET ocr_status = ?, {note_sql} WHERE slug = ?",
                         (status, *note_args, slug))
        conn.commit()
    finally:
        conn.close()


def _set_media_type(slug, media_type):
    """#448: retype a row to a different media_type. Used by core/ingest.py's retype()."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET media_type = ? WHERE slug = ?", (media_type, slug))
        conn.commit()
    finally:
        conn.close()


def list_stale_pending_ocr(older_than_seconds):
    """Rows stuck at ocr_status='pending' for suspiciously long — the
    watchdog re-fires these rather than assuming they're just queued behind
    a big batch forever."""
    conn = get_conn()
    try:
        cutoff = time.time() - older_than_seconds
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE ocr_status = 'pending' AND ocr_started_at < ?", (cutoff,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def set_perceptual_hash(slug, phash):
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET perceptual_hash = ? WHERE slug = ?", (phash, slug))
        conn.commit()
    finally:
        conn.close()


def set_embedding(slug, embedding_bytes):
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET embedding = ? WHERE slug = ?", (embedding_bytes, slug))
        conn.commit()
    finally:
        conn.close()


def list_hash_and_embedding_candidates(exclude_slug):
    """(slug, perceptual_hash, embedding) for every other row that has at
    least one of the two signals — used for live similarity comparison,
    not cached, so there's nothing to invalidate as new rows come in."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT slug, perceptual_hash, embedding FROM capture_events "
            "WHERE slug != ? AND (perceptual_hash IS NOT NULL OR embedding IS NOT NULL)",
            (exclude_slug,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_extracted_text(slug, text, source=None):
    """#724: `source` is where the text came from (the type's text_source, e.g. 'office'), stored
    as type_metadata.text_source in the same UPDATE; None clears a stale one (text OCR'd from a
    picture, or emptied). A row whose type_metadata isn't valid JSON keeps it as it is."""
    if source:
        meta_sql = ("type_metadata = CASE WHEN type_metadata IS NULL OR type_metadata = '' "
                    "THEN json_object('text_source', ?) WHEN json_valid(type_metadata) "
                    "THEN json_set(type_metadata, '$.text_source', ?) ELSE type_metadata END")
        meta_args = (source, source)
    else:
        meta_sql = ("type_metadata = CASE WHEN json_valid(type_metadata) "
                    "THEN json_remove(type_metadata, '$.text_source') ELSE type_metadata END")
        meta_args = ()
    conn = get_conn()
    try:
        conn.execute(f"UPDATE capture_events SET extracted_text = ?, {meta_sql} WHERE slug = ?",
                     (text, *meta_args, slug))
        conn.commit()
    finally:
        conn.close()


def set_text_source_fields(slug, fields, resettle=False):
    """#724 (e): pipeline bookkeeping for a type's network text source (a YouTube transcript,
    core/transcripts.py): merge `fields` into type_metadata ({key: value}, None removes the key),
    in one transaction that re-reads the row first (so a concurrent write to another key isn't
    lost). With `resettle`, a row whose OCR has settled also gets its extracted_text / text_source
    / ocr_status rebuilt from its type's sources and the picture text already stored
    (core/text_sources.resettle, no OCR re-run), and an 'unsupported' row's ocr_note follows the
    type's current reason ("no captions"). A pending row is left to its OCR run, a 'failed' row
    keeps its error until Retry, unreadable JSON is left alone. Returns the resettle change (or
    None)."""
    from .. import object_types, text_sources
    with transaction() as tx:
        r = tx.conn.execute(
            "SELECT slug, media_type, filename, stored_filename, external_url, extracted_text, ocr_status, "
            "type_metadata FROM capture_events WHERE slug = ?", (slug,)).fetchone()
        if r is None:
            return None
        row = dict(r)
        try:
            meta = json.loads(row["type_metadata"] or "{}")
        except ValueError:  # silent-ok: unreadable JSON is never overwritten; the caller's fetch is dropped
            print(f"set_text_source_fields: {slug} has unreadable type_metadata; left as it is", flush=True)
            return None
        if not isinstance(meta, dict):
            return None
        for key, value in (fields or {}).items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        row["type_metadata"] = meta
        change = None
        text, status = row["extracted_text"], row["ocr_status"]
        if resettle:
            spec = object_types.get_object_type(row.get("media_type"))
            change = text_sources.resettle(row, spec, object_types.ocr_unsupported_note(row, spec))
            if change is not None:
                meta = text_sources.resettled_meta(meta, change)
                text, status = change["text"], change["status"]
            if status == OCR_UNSUPPORTED:
                meta["ocr_note"] = object_types.ocr_unsupported_note({**row, "type_metadata": meta}, spec)
        tx.conn.execute("UPDATE capture_events SET extracted_text = ?, ocr_status = ?, type_metadata = ? WHERE slug = ?",
                        (text, status, json.dumps(meta), slug))
        return change


def list_due_text_fetches(media_types, now, limit):
    """#724 (e): slugs of rows of `media_types` waiting for a network text fetch: transcript_status
    'queued' (never tried), or 'retry' with transcript_retry_at passed. Not redacted, not pending
    OCR (that run fetches itself). Oldest-due first."""
    if not media_types:
        return []
    marks = ", ".join("?" for _ in media_types)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT slug FROM capture_events WHERE media_type IN ({marks}) AND COALESCE(redacted, 0) = 0 "
            "AND COALESCE(ocr_status, '') != 'pending' AND json_valid(type_metadata) AND ("
            "json_extract(type_metadata, '$.transcript_status') = 'queued' OR "
            "(json_extract(type_metadata, '$.transcript_status') = 'retry' "
            " AND json_extract(type_metadata, '$.transcript_retry_at') <= ?)) "
            "ORDER BY COALESCE(json_extract(type_metadata, '$.transcript_retry_at'), 0), id LIMIT ?",
            (*media_types, now, int(limit))).fetchall()
        return [r["slug"] for r in rows]
    finally:
        conn.close()


def list_unaccepted_captions(limit=500, offset=0):
    """Rows with an auto_caption suggestion that hasn't been accepted into
    content_description and hasn't been skipped — the confirm_caption bulk
    review queue (#409). Mirrors curator_needs._count_unaccepted_captions'
    filter, plus the auto_caption_dismissed skip flag.
    #542: plus the rows with a short-title suggestion (auto_caption_title) and no short_title or
    rename yet, not skipped (auto_caption_title_dismissed): the backfill's proposals for items
    whose long caption used to be their title, and the title a new caption came with."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE redacted=0 AND (("
            "json_extract(type_metadata, '$.auto_caption') IS NOT NULL "
            "AND json_extract(type_metadata, '$.auto_caption') != '' "
            "AND (content_description IS NULL OR content_description = '') "
            "AND json_extract(type_metadata, '$.auto_caption_dismissed') IS NULL"
            ") OR ("
            "json_extract(type_metadata, '$.auto_caption_title') IS NOT NULL "
            "AND json_extract(type_metadata, '$.auto_caption_title') != '' "
            "AND (short_title IS NULL OR short_title = '') AND (display_name IS NULL OR display_name = '') "
            "AND json_extract(type_metadata, '$.auto_caption_title_dismissed') IS NULL"
            ")) ORDER BY timestamp DESC, slug LIMIT ? OFFSET ?",  # #722: slug breaks ties, so MCP pages are stable
            (limit, max(0, int(offset))),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_short_title_candidates():
    """#542: rows with a content_description and no short_title (the seed migration reads them; it
    decides which descriptions read as a name)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE (short_title IS NULL OR short_title = '') "
            "AND content_description IS NOT NULL AND TRIM(content_description) != '' ORDER BY id").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_long_caption_items(media_types, limit=1000):
    """#542: the items whose title USED to be their long caption: no rename, no short title, a
    content_description, a caption-capable type (a short-title proposal needs a picture), not
    redacted, no short-title proposal queued, written or waiting in review (skipped ones count as
    answered). Newest first. The caller filters by the item policy. media_types None = any type."""
    types = sorted(set(media_types)) if media_types is not None else None
    if types is not None and not types:
        return []
    type_sql = f"AND COALESCE(media_type, 'image') IN ({','.join('?' for _ in types)}) " if types else ""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE redacted = 0 "
            "AND (display_name IS NULL OR display_name = '') AND (short_title IS NULL OR short_title = '') "
            "AND content_description IS NOT NULL AND TRIM(content_description) != '' "
            + type_sql +
            "AND COALESCE(json_extract(type_metadata, '$.short_title_proposal_status'), '') NOT IN ('queued', 'done') "
            "AND COALESCE(json_extract(type_metadata, '$.auto_caption_title'), '') = '' "
            "ORDER BY timestamp DESC LIMIT ?", (*(types or ()), int(limit))).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_done_captions():
    """#743: every non-redacted row whose caption status is `done` and that has a suggestion (the
    mid-word scan filters them in Python; there are a few thousand at most)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE redacted = 0 "
            "AND json_extract(type_metadata, '$.auto_caption_status') = 'done' "
            "AND COALESCE(json_extract(type_metadata, '$.auto_caption'), '') != '' ORDER BY timestamp DESC").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_needs_caption(media_types, include_failed=True, limit=50, count_only=False):
    """#585/#588: caption-capable rows that have no caption yet and no accepted description: the
    caption status is absent, or (include_failed) "failed"/"skipped". Pending rows are in flight,
    done rows have their caption (waiting in review or accepted), so neither is listed. Redacted
    rows and restricted types never appear (policy.sql_browse_clause). `media_types` is the set of
    caption-capable type keys; a NULL media_type reads as "image", as everywhere else.
    count_only returns just the number of matches (limit ignored)."""
    types = sorted(set(media_types))
    if not types:
        return 0 if count_only else []
    status = "json_extract(type_metadata, '$.auto_caption_status')"
    status_ok = f"({status} IS NULL OR {status} = ''" + (f" OR {status} IN ('failed', 'skipped')" if include_failed else "") + ")"
    type_ok = f"(media_type IN ({', '.join('?' * len(types))})" + (" OR media_type IS NULL OR media_type = ''" if "image" in types else "") + ")"
    where = ("WHERE redacted = 0" + policy.sql_browse_clause() + f" AND {type_ok} "
             "AND (content_description IS NULL OR content_description = '') "
             f"AND {status_ok}")
    conn = get_conn()
    try:
        if count_only:
            return conn.execute(f"SELECT COUNT(*) FROM capture_events {where}", types).fetchone()[0]
        rows = conn.execute(
            "SELECT slug, display_name, short_title, filename, content_description, media_type, type_metadata, tags, "
            f"extracted_text, timestamp FROM capture_events {where} ORDER BY timestamp DESC LIMIT ?",
            (*types, int(limit)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
