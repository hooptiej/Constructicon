"""Connections and transactions: DB_PATH, get_conn, transaction(), read_session() and the
ambient shared connection they hand out; plus _row_to_dict and _table_exists, the row and table
helpers the other submodules share.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import functools
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


# #453: overridable so the DB can live in its own bind-mounted DIRECTORY.
# WAL mode keeps -wal/-shm next to the DB file; with only the file
# bind-mounted, each container (web, mcp) got private copies of those and
# the two processes corrupted the shared DB. Default unchanged for local runs.
DB_PATH = Path(os.environ.get("CONSTRUCTICON_DB_PATH") or Path(__file__).resolve().parent.parent.parent / "imagerepo.db")


def get_conn():
    # While this thread is inside `transaction()` (V2 cards piece 6) every helper that
    # asks for a connection gets the shared transaction connection instead, so a
    # composite card operation (split, merge, bulk, undo) is one atomic unit and can be
    # dry-run by rolling the whole thing back.
    shared = getattr(_ambient, "conn", None)
    if shared is not None:
        return shared
    # timeout=30: wait up to 30 seconds if DB is locked by another writer (default 5s).
    # check_same_thread=False: safe here since we create a fresh connection per call.
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Enable WAL (Write-Ahead Logging) for better concurrency.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# --- Ambient transactions (V2 cards piece 6) ---------------------------------------
# `with transaction() as tx:` opens ONE write transaction on this thread. Every
# db helper that calls get_conn() inside the block shares it: commit()/rollback()/
# close() on the shared handle are no-ops and `BEGIN` is swallowed, so existing
# helpers keep working unchanged but their writes land in (and read back from) the
# one transaction. Leaving the block commits; an exception, or dry_run=True, rolls
# the whole thing back. Blocks nest via SAVEPOINTs (tx.savepoint() for per-item
# rollback in a bulk call). Thread-local: other threads are unaffected (they wait
# on SQLite's write lock for the short time the transaction is open).
_ambient = threading.local()


class _SharedConn:
    """Stands in for a sqlite3 connection inside transaction(). Delegates everything
    except the transaction-control calls, which the transaction() block owns."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "END")):
            return self._conn.execute("SELECT 1")
        return self._conn.execute(sql, *args)

    def executemany(self, sql, seq):
        return self._conn.executemany(sql, seq)

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def executescript(self, *_a, **_k):
        raise RuntimeError("executescript would commit the surrounding transaction")

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Tx:
    """Handle yielded by transaction(); see savepoint()."""

    def __init__(self, conn):
        self.conn = conn
        self._n = 0

    @contextmanager
    def savepoint(self):
        """Nested unit: kept on normal exit, rolled back (and the exception re-raised)
        on error. Use `try/except` around the `with` to carry on past a failed item."""
        self._n += 1
        name = f"sp_{self._n}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")


@contextmanager
def transaction(dry_run=False, timeout=30):
    """See the block comment above. dry_run=True rolls back on exit instead of committing.
    Re-entrant: a nested block runs inside a savepoint of the outer one. `timeout` is how many
    seconds BEGIN IMMEDIATE (and every statement after it) waits for another process's write lock
    before failing with "database is locked"; ignored by a nested block."""
    outer = getattr(_ambient, "tx", None)
    if outer is not None:
        try:
            with outer.savepoint():
                yield outer
                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:  # silent-ok: the dry run's own sentinel; the rollback is the point
            pass
        return
    real = sqlite3.connect(DB_PATH, timeout=timeout, check_same_thread=False, isolation_level=None)
    real.row_factory = sqlite3.Row
    real.execute("PRAGMA journal_mode=WAL")
    real.execute("BEGIN IMMEDIATE")
    tx = _Tx(_SharedConn(real))
    prev_conn = getattr(_ambient, "conn", None)  # a read_session() this block sits inside
    _ambient.memo = None  # writes are coming: a read_session's memo (below) must not outlive them
    _ambient.conn, _ambient.tx = tx.conn, tx
    try:
        yield tx
    except BaseException:
        _ambient.conn, _ambient.tx = prev_conn, None
        real.execute("ROLLBACK")
        real.close()
        raise
    _ambient.conn, _ambient.tx = prev_conn, None
    try:
        real.execute("ROLLBACK" if dry_run else "COMMIT")
    finally:
        real.close()


def in_transaction():
    """True inside a transaction() on this thread (#725: derived-data caches skip uncommitted state)."""
    return getattr(_ambient, "tx", None) is not None


@contextmanager
def read_session():
    """(#524) One shared connection for a read-heavy computation. get_conn() opens and closes
    a fresh connection per call (~0.8 ms each), and a whole Curator queue build makes about
    2,500 of them: roughly 2 seconds of pure connect/close. Inside this block every helper
    that calls get_conn() gets the one connection instead. No write lock is taken (unlike
    transaction()): statements autocommit, so a write made inside still lands, and other
    writers are never blocked. Re-entrant, and a no-op when this thread is already inside a
    transaction() or another read_session()."""
    if getattr(_ambient, "conn", None) is not None:
        yield
        return
    real = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
    real.row_factory = sqlite3.Row
    real.execute("PRAGMA journal_mode=WAL")
    _ambient.conn = _SharedConn(real)
    _ambient.memo = {}  # per-session cache for list_project_items (read-only work, so it can't go stale)
    try:
        yield
    finally:
        _ambient.conn = None
        _ambient.memo = None
        real.close()


def in_read_session(fn):
    """Decorator: run `fn` inside read_session()."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with read_session():
            return fn(*args, **kwargs)
    return wrapper


def change_fingerprint():
    """(#524) A cheap token that changes whenever ANY process commits to the database: the
    mtime and size of the main file and its WAL. Used to cache derived data (the Curator
    queue) for as long as nothing at all has been written, with no list of 'writes that
    matter' to keep in sync. Returns a tuple, or None if the files can't be read."""
    out = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(f"{DB_PATH}{suffix}")
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


class _DryRunRollback(Exception):
    """Internal: unwinds a nested dry-run block's savepoint."""


def _row_to_dict(row):
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    d["type_metadata"] = json.loads(d["type_metadata"]) if d.get("type_metadata") else {}
    return d


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone() is not None
