"""Pending decisions (#240) and the Curator queue's defer/dismiss state (#519).

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import json
import time

from .conn import get_conn
from .changelog import ImageLog


# --- Pending decisions (#240) ---
# A small generic "don't auto-decide, ask the owner" queue. One row per open
# question about one capture_events row: `kind` names the trigger (today only
# "project_match" — core/automatch.py's ambiguous multi-project name match),
# `payload` is a freeform JSON bag whose shape is per-kind (for project_match:
# {"candidate_project_ids": [...], "matched_text": "..."}), and resolving
# stamps resolved_at plus whatever was decided into payload["resolution"].
# Deliberately not a project_match-specific table: a future "ask, don't
# guess" case (an ambiguous OCR client match, a duplicate-looking upload...)
# adds a new `kind` and a payload shape, not a schema migration. Kept to
# exactly what #240 needs beyond that — no priorities, no assignment, no
# expiry.

def _pending_row(row):
    d = dict(row)
    d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
    return d


def add_pending_decision(kind, post_slug, payload):
    """Queues one open decision. If the same (kind, post_slug) already has an
    UNRESOLVED entry, its payload is replaced instead of a duplicate being
    queued (a re-upload / retry shouldn't ask the same question twice).
    Returns the decision id either way."""
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM pending_decisions WHERE kind = ? AND post_slug = ? AND resolved_at IS NULL",
            (kind, post_slug),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE pending_decisions SET payload = ?, created_at = ? WHERE id = ?",
                (json.dumps(payload or {}), time.time(), existing["id"]),
            )
            decision_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO pending_decisions (kind, post_slug, payload, created_at) VALUES (?, ?, ?, ?)",
                (kind, post_slug, json.dumps(payload or {}), time.time()),
            )
            decision_id = cur.lastrowid
        conn.commit()
        return decision_id
    finally:
        conn.close()


def queue_decision_once(kind, post_slug, payload, conn=None):
    """Queues a decision UNLESS any row -- open OR resolved -- of this kind and
    post_slug already exists (V2 cards 4.1). Unlike add_pending_decision (which
    only dedupes unresolved rows), re-running a migration can therefore never
    re-ask a question the owner already answered. Returns the new decision id,
    or None when skipped. Pass `conn` to join a caller's transaction (the caller
    commits); otherwise this opens and commits its own."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM pending_decisions WHERE kind = ? AND post_slug = ?", (kind, post_slug)
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO pending_decisions (kind, post_slug, payload, created_at) VALUES (?, ?, ?, ?)",
            (kind, post_slug, json.dumps(payload or {}), time.time()),
        )
        if own:
            conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def list_all_pending_decisions(kind_prefix=None, post_slug=None):
    """Every decision row, open or resolved, optionally narrowed to kinds
    starting with `kind_prefix` and/or one post_slug. Oldest first."""
    conn = get_conn()
    try:
        sql = "SELECT * FROM pending_decisions WHERE 1=1"
        args = []
        if kind_prefix:
            sql += " AND kind LIKE ?"
            args.append(kind_prefix + "%")
        if post_slug:
            sql += " AND post_slug = ?"
            args.append(post_slug)
        rows = conn.execute(sql + " ORDER BY created_at ASC, id ASC", args).fetchall()
        return [_pending_row(r) for r in rows]
    finally:
        conn.close()


def get_pending_decision(decision_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM pending_decisions WHERE id = ?", (decision_id,)).fetchone()
        return _pending_row(row) if row else None
    finally:
        conn.close()


def list_pending_decisions(kind=None):
    """Every UNRESOLVED decision, oldest first (the owner should see what's
    been waiting longest at the top), optionally filtered by kind."""
    conn = get_conn()
    try:
        if kind is None:
            rows = conn.execute("SELECT * FROM pending_decisions WHERE resolved_at IS NULL ORDER BY created_at ASC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pending_decisions WHERE resolved_at IS NULL AND kind = ? ORDER BY created_at ASC", (kind,)
            ).fetchall()
        return [_pending_row(r) for r in rows]
    finally:
        conn.close()


def count_pending_decisions():
    conn = get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM pending_decisions WHERE resolved_at IS NULL").fetchone()["n"]
        return n
    finally:
        conn.close()


def _resolve_pending_decision(decision_id, resolution=None, log=None):
    """Marks a decision resolved, recording what was chosen (any JSON-able
    value — for project_match, the list of project ids applied, possibly
    empty for "none of these") inside payload["resolution"]. Resolved rows
    are kept, not deleted, so the audit trail of what got auto-asked and
    what the owner answered survives. Returns the updated row, or None."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM pending_decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"]) if row["payload"] else {}
        payload["resolution"] = resolution
        now = time.time()
        if log:
            # V2 cards 3.13: `log` = {op, actor, batch_id}; the resolution is imaged so it can be undone.
            il = ImageLog(log["op"], log["actor"], log.get("batch_id"), conn=conn)
            il.update("pending_decisions", {"id": decision_id}, {"resolved_at": now, "payload": json.dumps(payload)})
            il.flush()
        else:
            conn.execute(
                "UPDATE pending_decisions SET resolved_at = ?, payload = ? WHERE id = ?",
                (now, json.dumps(payload), decision_id),
            )
        conn.commit()
        return get_pending_decision(decision_id)
    finally:
        conn.close()


# --- Curator queue state: Defer and Dismiss (Stage 3, #519) ---
# Nudges and computed needs are derived on-demand (never stored); stored decisions live
# in pending_decisions. What IS stored here is the owner's choice about an item, keyed by
# a stable string key: 'decision:<id>' (a stored question), '<kind>:project:<id>' /
# '<kind>:global' (a nudge) or 'need:<need>:<card or hobby slug>' (a computed need).
#   action 'dismiss' = gone for good (nudges and needs only, never a question)
#   action 'defer'   = moved to the Deferred section, no timer; stays until answered or
#                      brought back. Replaces the old timed 'snooze' (init_db migrates
#                      any snooze still running into a defer).
# Both go through ImageLog so each is a change-log row and can be undone.

CURATOR_DISMISS = "dismiss"
CURATOR_DEFER = "defer"


def set_curator_state(key, action, actor=None, batch_id=None, affected_slugs=None):
    """Set an item's queue state to 'dismiss' or 'defer' (replacing whatever it had).
    Returns True when something changed."""
    if action not in (CURATOR_DISMISS, CURATOR_DEFER):
        raise ValueError(f"Unknown curator state {action!r}")
    changed = False
    with ImageLog(action, actor, batch_id, affected_slugs=affected_slugs) as il:
        existing = il.get("curator_dismissals", {"nudge_key": key})
        if existing is not None:
            if existing["action"] == action:
                return False
            il.delete("curator_dismissals", {"nudge_key": key})
        il.insert("curator_dismissals", {"nudge_key": key},
                  {"action": action, "snooze_until": None, "created_at": time.time()})
        changed = True
    return changed


def clear_curator_defer(key, actor=None, batch_id=None, affected_slugs=None):
    """Bring a deferred item back into the main queue. Only a 'defer' row is removed
    (a dismissal is not undone by this). Returns True when something changed."""
    changed = False
    with ImageLog("bring_back", actor, batch_id, affected_slugs=affected_slugs) as il:
        existing = il.get("curator_dismissals", {"nudge_key": key})
        if existing is not None and existing["action"] == CURATOR_DEFER:
            il.delete("curator_dismissals", {"nudge_key": key})
            changed = True
    return changed


def list_curator_states():
    """{key: 'dismiss' | 'defer'} for every stored queue state."""
    conn = get_conn()
    try:
        return {r["nudge_key"]: r["action"] for r in conn.execute("SELECT nudge_key, action FROM curator_dismissals")}
    finally:
        conn.close()


def list_active_curator_dismissals():
    """{nudge_key: row dict} of permanent dismissals (what the nudge list hides).
    Deferred items are NOT in here: they stay in the list, flagged, so the queue can
    show them in its Deferred section."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM curator_dismissals WHERE action = 'dismiss'").fetchall()
        return {r["nudge_key"]: dict(r) for r in rows}
    finally:
        conn.close()
