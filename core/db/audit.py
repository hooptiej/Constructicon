"""The request audit log and the sensitive-item access log.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import json
import time

from .. import actor as actor_ctx
from .conn import get_conn


# --- #604 follow-up 7: the sensitive-item access log (core/access_log.py) ---

def insert_access_log(slug, actor, how, at, fold_seconds=0):
    """One access row (pipeline/infra log, like the request log). When the same actor opened the same
    item through the same door within `fold_seconds`, nothing is written (returns False)."""
    conn = get_conn()
    try:
        if fold_seconds and conn.execute(
                "SELECT 1 FROM item_access_log WHERE slug = ? AND actor = ? AND how = ? AND at > ? LIMIT 1",
                (slug, actor, how, at - fold_seconds)).fetchone():
            return False
        conn.execute("INSERT INTO item_access_log (slug, actor, how, at) VALUES (?, ?, ?, ?)", (slug, actor, how, at))
        conn.commit()
        return True
    finally:
        conn.close()


def list_access_log(slug, limit=100):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT actor, how, at FROM item_access_log WHERE slug = ? ORDER BY at DESC, id DESC LIMIT ?",
            (slug, int(limit))).fetchall()]
    finally:
        conn.close()


def access_log_summary(slugs):
    """{slug: {count, last_at, last_actor}} for the given slugs (absent = never opened)."""
    if not slugs:
        return {}
    conn = get_conn()
    try:
        out = {}
        marks = ", ".join("?" * len(slugs))
        for r in conn.execute(
                f"SELECT slug, COUNT(*) AS n, MAX(at) AS last_at FROM item_access_log WHERE slug IN ({marks}) "
                "GROUP BY slug", list(slugs)).fetchall():
            last = conn.execute("SELECT actor FROM item_access_log WHERE slug = ? ORDER BY at DESC, id DESC LIMIT 1",
                                (r["slug"],)).fetchone()
            out[r["slug"]] = {"count": r["n"], "last_at": r["last_at"], "last_actor": last["actor"] if last else None}
        return out
    finally:
        conn.close()


# --- Audit log ---
# Captures mutating API requests for debugging/recovery after failures.

def insert_audit_log(method, path, form_body, status_code, error_detail=None, affected_slugs=None, actor=None,
                     required_role=None, client_ip=None):
    """Insert a row into the audit_log table. form_body should be a dict (will be
    JSON-serialized). affected_slugs can be a list of slugs or None. Automatically
    records the current timestamp. #560: `actor` (default: the current actor context)
    fills the request-log row's actor column. #557: `required_role` is the route's role
    label (None when no route matched, e.g. a 404 or a request the guard refused).
    #646: `client_ip` is the client's address (web/proxy_trust.client_ip)."""
    conn = get_conn()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO audit_log (method, path, form_body, affected_slugs, status_code, error_detail, timestamp, actor, "
            "required_role, client_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                method,
                path,
                json.dumps(form_body),
                json.dumps(affected_slugs or []),
                status_code,
                error_detail,
                now,
                actor_ctx.resolve(actor),
                required_role,
                client_ip,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_recent_audit_logs(limit=100):
    """Fetch the most recent audit_log rows (most recent first). Returns a list
    of dicts with all columns. form_body and affected_slugs are parsed from JSON."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                **dict(row),
                "form_body": json.loads(row["form_body"]) if row["form_body"] else {},
                "affected_slugs": json.loads(row["affected_slugs"]) if row["affected_slugs"] else [],
            }
            for row in rows
        ]
    finally:
        conn.close()
