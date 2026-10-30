"""Unpack the Crate (#605): archive -> card conversion job rows.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import json
import time

from .conn import get_conn


# --- #605 "Unpack the Crate": archive -> card conversion jobs (core/unpack.py owns the writes) ------

_UNPACK_JSON = ("progress", "result")


def _unpack_job_dict(r):
    if r is None:
        return None
    d = dict(r)
    for k in _UNPACK_JSON:
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except (TypeError, ValueError):
            d[k] = {"unreadable": True}  # silent-ok: shown as such; the job row itself says what it was
    return d


def _insert_unpack_job(archive_slug, batch_id, actor, shape, depth, channel=None, via_token_id=None):
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO unpack_jobs (archive_slug, batch_id, actor, shape, depth, channel, via_token_id, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (archive_slug, batch_id, actor, shape, int(depth or 0), channel, via_token_id, time.time()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _update_unpack_job(job_id, **fields):
    """Sets the named columns (progress / result as dicts). Returns the number of rows changed."""
    if not fields:
        return 0
    cols, vals = [], []
    for k, v in fields.items():
        if k not in ("status", "progress", "result", "error", "started_at", "finished_at"):
            raise ValueError(f"unpack_jobs has no writable column {k!r}")
        cols.append(f"{k} = ?")
        vals.append(json.dumps(v) if k in _UNPACK_JSON else v)
    conn = get_conn()
    try:
        n = conn.execute(f"UPDATE unpack_jobs SET {', '.join(cols)} WHERE id = ?", (*vals, job_id)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _claim_unpack_job(job_id):
    """queued -> running, atomically (two workers never both run one). True when this call won."""
    conn = get_conn()
    try:
        n = conn.execute("UPDATE unpack_jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                         (time.time(), job_id)).rowcount
        conn.commit()
        return n == 1
    finally:
        conn.close()


def get_unpack_job(job_id):
    conn = get_conn()
    try:
        return _unpack_job_dict(conn.execute("SELECT * FROM unpack_jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()


def list_unpack_jobs(status=None, archive_slug=None, limit=50):
    """Newest first. `status` a string or a tuple of them."""
    sql, args = "SELECT * FROM unpack_jobs WHERE 1 = 1", []
    if status:
        st = (status,) if isinstance(status, str) else tuple(status)
        sql += f" AND status IN ({', '.join('?' * len(st))})"
        args += list(st)
    if archive_slug:
        sql += " AND archive_slug = ?"
        args.append(archive_slug)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    conn = get_conn()
    try:
        return [_unpack_job_dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
