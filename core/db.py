"""SQLite index for Constructicon. One row per upload."""

import functools
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from . import actor as actor_ctx, card_rules, errors, policy, timeline

_log = logging.getLogger("constructicon.db")

# #453: overridable so the DB can live in its own bind-mounted DIRECTORY.
# WAL mode keeps -wal/-shm next to the DB file; with only the file
# bind-mounted, each container (web, mcp) got private copies of those and
# the two processes corrupted the shared DB. Default unchanged for local runs.
DB_PATH = Path(os.environ.get("CONSTRUCTICON_DB_PATH") or Path(__file__).resolve().parent.parent / "imagerepo.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    timestamp REAL NOT NULL,
    tech TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    extracted_text TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    tags TEXT NOT NULL DEFAULT '[]',
    redacted INTEGER NOT NULL DEFAULT 0,
    filename TEXT,
    stored_filename TEXT,
    file_size INTEGER,
    source_modified_at REAL,
    ocr_status TEXT,
    ocr_started_at REAL,
    perceptual_hash TEXT,
    media_type TEXT NOT NULL DEFAULT 'image',
    external_url TEXT,
    content_description TEXT,
    content_date REAL,
    type_metadata TEXT NOT NULL DEFAULT '{}',
    display_name TEXT,
    short_title TEXT,
    agent_notes TEXT,
    provenance TEXT,
    highlight INTEGER NOT NULL DEFAULT 0,
    is_brand_asset INTEGER NOT NULL DEFAULT 0,
    brand_role TEXT
);
-- #704: the imagerepo-era columns source, client, ticket_id, artifact_link and icon, and the
-- clients / client_domains tables, are gone. An existing DB loses them in
-- _drop_dead_columns_704 (web boot), which refuses to drop one that still holds real data.
CREATE TABLE IF NOT EXISTS capture_event_relations (
    slug_a TEXT NOT NULL,
    slug_b TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (slug_a, slug_b)
);
CREATE TABLE IF NOT EXISTS item_revisions (
    old_slug TEXT PRIMARY KEY,
    new_slug TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- UNIQUE: an item supersedes at most one other, so a chain is linear (A -> B -> C), never a fork.
CREATE UNIQUE INDEX IF NOT EXISTS idx_item_revisions_new ON item_revisions(new_slug);
CREATE TABLE IF NOT EXISTS blog_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    parent_id INTEGER REFERENCES blog_tags(id)
);
CREATE INDEX IF NOT EXISTS idx_blog_tags_parent ON blog_tags(parent_id);
CREATE TABLE IF NOT EXISTS post_tags (
    post_slug TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES blog_tags(id),
    PRIMARY KEY (post_slug, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag_id);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cover_slug TEXT,
    cover_project_id INTEGER REFERENCES projects(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    tag_id INTEGER REFERENCES blog_tags(id),
    parent_id INTEGER REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS project_items (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    post_slug TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, post_slug)
);
CREATE INDEX IF NOT EXISTS idx_project_items_slug ON project_items(post_slug);
CREATE TABLE IF NOT EXISTS project_relations (
    slug_a TEXT NOT NULL,
    slug_b TEXT NOT NULL,
    type   TEXT NOT NULL DEFAULT 'related',
    note   TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (slug_a, slug_b, type)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- #562: per-install identity (owner name/label, export site title, publish targets).
-- core/install_config.py owns reads (cached) and writes (imaged, undoable). Never secrets.
CREATE TABLE IF NOT EXISTS install_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL
);
-- #562: per-hobby settings. shows_physical_piece = items in this hobby's cards get the
-- PHYSICAL PIECE fields (core/physical_piece.py); was a name match on "Traditional Media".
-- #596 adds synopsis / flavor (the hobby card's text box) in init_db's ALTER loop.
CREATE TABLE IF NOT EXISTS hobby_settings (
    hobby_tag_id INTEGER PRIMARY KEY REFERENCES blog_tags(id),
    shows_physical_piece INTEGER NOT NULL DEFAULT 0
);
-- #554 (batch 5): every URL slug a hobby has had (written by hobbies.rename, never imaged and never
-- dropped by an undo, like hobby_label_known), so /hobby/<old slug> 308-redirects to the hobby's
-- current address instead of breaking. A slug that is some hobby's CURRENT slug always wins.
CREATE TABLE IF NOT EXISTS hobby_slug_history (
    slug TEXT PRIMARY KEY,
    hobby_tag_id INTEGER NOT NULL REFERENCES blog_tags(id),
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    form_body TEXT NOT NULL DEFAULT '{}',
    affected_slugs TEXT NOT NULL DEFAULT '[]',
    status_code INTEGER,
    error_detail TEXT,
    timestamp REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    post_slug TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pending_decisions_open ON pending_decisions(kind, resolved_at);
CREATE TABLE IF NOT EXISTS blog_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    cover_slug TEXT,
    content_date REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blog_entry_projects (
    entry_id INTEGER NOT NULL REFERENCES blog_entries(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    sort_order INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (entry_id, project_id)
);
CREATE TABLE IF NOT EXISTS blog_entry_items (
    entry_id INTEGER NOT NULL REFERENCES blog_entries(id),
    post_slug TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (entry_id, post_slug)
);
CREATE INDEX IF NOT EXISTS idx_blog_entry_items_slug ON blog_entry_items(post_slug);
CREATE TABLE IF NOT EXISTS curator_dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nudge_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    snooze_until REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_curator_dismissals_nudge_key ON curator_dismissals(nudge_key);
CREATE TABLE IF NOT EXISTS caption_queue (
    slug TEXT PRIMARY KEY,
    start_step INTEGER NOT NULL DEFAULT 0,
    cascade INTEGER NOT NULL DEFAULT 1,
    enqueued_at REAL NOT NULL,
    mode TEXT NOT NULL DEFAULT 'caption',
    batch_id TEXT
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at REAL NOT NULL
);
-- #541 phase B: a deleted (or redacted) item's files sit in <storage>/.trash/<batch_id>/ for
-- TRASH_DAYS (core/items.py) so the delete can be undone; the web worker purges them after.
-- One row per item per batch; purged_at set = the files are gone for good (undo refuses).
-- expires_at NULL = a redact HOLD: kept until the owner recovers or permanently deletes it, never
-- purged by the hourly pass or "Empty trash now".
CREATE TABLE IF NOT EXISTS trash (
    batch_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    stored_filename TEXT,
    has_original INTEGER NOT NULL DEFAULT 0,
    has_thumb INTEGER NOT NULL DEFAULT 0,
    dir TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    reason TEXT NOT NULL DEFAULT 'delete',
    created_at REAL NOT NULL,
    expires_at REAL,
    purged_at REAL,
    embedding BLOB,
    PRIMARY KEY (batch_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_trash_expires ON trash(purged_at, expires_at);
-- #467 step 1: user accounts and server-side sessions (core/users.py owns every write).
-- password_hash is "scrypt$n$r$p$salt$hash" (NULL = no usable password until a reset). It is
-- never imaged into the change log and never returned by a listing.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT,
    role TEXT NOT NULL,
    password_hash TEXT,
    created_at REAL NOT NULL,
    last_login_at REAL,
    disabled INTEGER NOT NULL DEFAULT 0
);
-- token_hash = sha256 of the cookie token: the token itself is never stored.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
-- #604 follow-up 7 / #603: who opened, previewed or downloaded a SENSITIVE item (a restricted type
-- or the per-item flag). One row per access (core/access_log.py; repeats by the same actor through
-- the same door within a few minutes are folded into one). Ordinary items are never logged.
CREATE TABLE IF NOT EXISTS item_access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    actor TEXT NOT NULL,
    how TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_access_log_slug ON item_access_log(slug, at);
-- #604 step 3: the members of a PRIVATE card (projects.private) or hobby (hobby_settings.private),
-- each a viewer or an editor of it. Only core/sharing.py writes it (imaged, undoable). The owner
-- (created_by_user_id) and admins see a private card without a row here.
CREATE TABLE IF NOT EXISTS share_members (
    target_kind TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (target_kind, target_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_share_members_user ON share_members(user_id);
-- #604 step 5: per-user MCP tokens, several per user, each named ("windows claude"). Only the sha256
-- of the token is stored (like sessions); core/mcp_tokens.py owns every write. Never imaged.
CREATE TABLE IF NOT EXISTS mcp_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    rotated_at REAL,
    last_used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user ON mcp_tokens(user_id);
-- #605 "Unpack the Crate": one archive -> card conversion, run in the background by the web
-- process (#549: the MCP only queues one). core/unpack.py owns every write. status: queued |
-- running | done | partial (finished, some members failed) | failed (stopped; see `error`).
-- `progress` / `result` are JSON. `channel` / `via_token_id`: how the starter reached the app and their
-- personal MCP key (#560 / #778), replayed when the worker runs it, so members get the right credit. Not imaged: the conversion's own rows are in the change log
-- under `batch_id`, which is what undo reverses.
CREATE TABLE IF NOT EXISTS unpack_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_slug TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    shape TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    channel TEXT,
    via_token_id INTEGER,
    status TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_unpack_jobs_status ON unpack_jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_unpack_jobs_archive ON unpack_jobs(archive_slug, id);
"""

# Provenance (capture_events.provenance, #341): the options live in the provenance_options
# table since #529 (core/provenance_options.py seeds and owns them).

# --- Brand asset roles (capture_events.brand_role) — #350 ---
# brand_role is freeform text (custom values allowed). The suggested list the picker offers is
# editable in Admin since #556: the vocab_terms table, owned by core/vocab.py (vocab 'brand_role').

# --- Hobby statuses (blog_tags.hobby_status) — #360 ---
# Controlled vocab for hobby activity. Only meaningful when is_hobby=1.
# V2 cards 3.3: a two-value MANUAL switch. 'dormant'/'abandoned' are deprecated
# aliases of 'inactive' (card_rules.HOBBY_DEPRECATED_ALIASES), accepted for one release.
HOBBY_STATUSES = list(card_rules.HOBBY_ACTIVITIES)

# --- Source (capture_events.tech) ---
# `tech` used to record which technician uploaded a screenshot in imagerepo
# (the original project), when it was a real multi-tech tool gated behind auth.
# Auth is gone (Phase 1) and this is a single-owner site now, so the column has
# been repurposed as a "Source" label: who or what actually added the row, and
# how. Every value written into `tech` should be one of these four shapes
# (SOURCE_MIGRATED is a template — fill in `<source>`). The owner's two come from
# the install's own config (#562, core/install_config.py owner_label): on the
# owner's installs exactly "Hooptie J (me) — manual upload" as before; on a fresh
# install "Owner — manual upload" until an owner is set. #560: `tech` is only the free-text
# Source NOTE now: WHO is `uploaded_by_user_id` (from the actor, #604) and HOW is `channel` (below);
# nothing decides identity, ownership or a gallery from it.
def source_manual_upload():
    from . import install_config  # lazy: install_config imports this module
    return f"{install_config.owner_label()} — manual upload"


def source_automated_upload():
    from . import install_config
    return f"{install_config.owner_label()} — automated upload"


SOURCE_CLAUDE = "Claude"
SOURCE_AUTHORED = "Claude — authored"  # Not written by anything yet — reserved for a
# future script that generates original content (e.g. a write-up) rather than
# migrating existing content from somewhere else. See SOURCE_MIGRATED for that case.


def source_migrated_from(source):
    """"Claude — migrated from <source>" — for automated migration scripts
    that bring in existing content from elsewhere (e.g. source="hooptiej.github.io"
    for scripts/archive/backfill_from_hooptiej_site.py)."""
    return f"Claude — migrated from {source}"


# #560: the CHANNEL (capture_events.channel) is how an item arrived; the Source label (`tech`) above
# stays the free-text note. A label in source_migrated_from's shape names where migrated content came
# from, so it sets the channel "migration:<source>"; anything else keeps the context's channel. Only
# ever descriptive: no identity, permission or ownership is decided from a channel or a label.
_MIGRATED_LABEL = re.compile(r"^Claude [—-] migrated from (.+)$", re.S)
CHANNEL_MAX = 120


def migration_source(label):
    """"<source>" for a "Claude — migrated from <source>" label, else None."""
    m = _MIGRATED_LABEL.match((label or "").strip())
    return " ".join(m.group(1).split())[:CHANNEL_MAX - 10] if m else None


def channel_for(label, base_channel):
    """The channel to record for an item with Source label `label` made through `base_channel`."""
    src = migration_source(label)
    return f"migration:{src}" if src else base_channel


CHANNEL_LABELS = {"web": "the web app", "mcp": "the MCP", "script": "a script", "system": "the app itself",
                  "migration": "a migration", "unknown": "not recorded"}


def channel_label(channel):
    """A channel as a person reads it: "the web app", "the MCP", "a migration from hooptiej.com"."""
    if not channel:
        return "not recorded"
    if channel.startswith("migration:"):
        return f"a migration from {channel[len('migration:'):]}"
    return CHANNEL_LABELS.get(channel, channel)


# Known "who" prefixes a Source string can start with — used to bucket the
# compact gallery views (home page panes, /gallery/user/<x>) under a short
# grouping key instead of showing/URL-encoding the full sentence-length
# Source string in a narrow layout. The full string is still stored as-is in
# `tech` and always shown in full on the object detail page.
def source_groups():
    """The owner's label (install config, #562) first, then "Claude". The neutral fallback
    ("Owner") is kept as a group too once a real label is set, so uploads made before the
    owner was configured still group."""
    from . import install_config
    groups = [install_config.owner_label(), SOURCE_CLAUDE]
    if install_config.FALLBACK_OWNER not in groups:
        groups.append(install_config.FALLBACK_OWNER)
    return groups


def source_group(tech):
    """Short grouping key for a Source string, e.g. "<owner label> — manual
    upload" groups under "<owner label>". Falls back to the value unchanged
    if it doesn't start with a known prefix (covers legacy rows from before
    this repurposing)."""
    if not tech:
        return tech
    for group in source_groups():
        if tech == group or tech.startswith(group + " —") or tech.startswith(group + " -"):
            return group
    return tech


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


class _DryRunRollback(Exception):
    """Internal: unwinds a nested dry-run block's savepoint."""


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


def _trash_expires_nullable(conn):
    """Redact holds (items.redact) have no expiry, so trash.expires_at must allow NULL. A trash
    table created by the first phase-B schema has it NOT NULL; rebuild it once (idempotent).
    #667: only called inside init_db's schema transaction, so the check and the rebuild happen
    under one write lock (two processes used to both see NOT NULL and both rebuild; the second
    one could hit "there is already another table or index with this name: trash_old")."""
    info = {r["name"]: r for r in conn.execute("PRAGMA table_info(trash)")}
    if not info or not info["expires_at"]["notnull"]:
        return
    conn.execute("DROP INDEX IF EXISTS idx_trash_expires")
    conn.execute("ALTER TABLE trash RENAME TO trash_old")
    conn.execute("""CREATE TABLE trash (
        batch_id TEXT NOT NULL, slug TEXT NOT NULL, stored_filename TEXT,
        has_original INTEGER NOT NULL DEFAULT 0, has_thumb INTEGER NOT NULL DEFAULT 0,
        dir TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, title TEXT,
        reason TEXT NOT NULL DEFAULT 'delete', created_at REAL NOT NULL, expires_at REAL,
        purged_at REAL, embedding BLOB, PRIMARY KEY (batch_id, slug))""")
    conn.execute("INSERT INTO trash SELECT batch_id, slug, stored_filename, has_original, has_thumb, dir, "
                 "size_bytes, title, reason, created_at, expires_at, purged_at, embedding FROM trash_old")
    conn.execute("DROP TABLE trash_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trash_expires ON trash(purged_at, expires_at)")


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


# --- Tag chips: a cache derived from the tag tree (#555) ---------------------------------------
# Owner decision 2026-10-18: ONE tag store, the tree (blog_tags + post_tags). An item's chips (the
# names its TAGS box shows, `row["tags"]` everywhere) are DERIVED: the names of its post_tags tags,
# in the order they were attached, minus the linked tag (projects.tag_id) of any card the item is
# on (card membership shows as the card, a link, never as a chip). `capture_events.tags` stays for
# one release as a cache of exactly that list, and nothing writes it as truth: these triggers
# recompute it whenever anything it depends on changes (a tag attached or detached, a file put on
# or taken off a card, a tag renamed, a card's linked tag changed or the card deleted, a row
# inserted, a direct write to the column), on every path: the tag service, undo, the pipeline,
# scripts. The cache is never imaged (core/tags.py writes post_tags; undo restores post_tags and
# the triggers follow). The triggers exist only once the `tags_one_store_555` migration has run:
# before it, the column still holds free text that migration has to merge into the tree first.
MIGRATION_555 = "tags_one_store_555"
CHIP_TRIGGER_PREFIX = "item_chips_"


def chips_sql(slug_ref):
    """SQL for one item's derived chip list (a JSON array of names) given an SQL slug reference."""
    return ("COALESCE((SELECT json_group_array(name) FROM (SELECT bt.name AS name FROM post_tags pt "
            f"JOIN blog_tags bt ON bt.id = pt.tag_id WHERE pt.post_slug = {slug_ref} AND NOT EXISTS ("
            "SELECT 1 FROM project_items pi JOIN projects p ON p.id = pi.project_id "
            f"WHERE pi.post_slug = {slug_ref} AND p.tag_id = pt.tag_id) ORDER BY pt.rowid)), '[]')")


def _chip_triggers():
    """name -> DDL for every trigger that keeps capture_events.tags equal to the derived chips."""
    def refresh(where):
        return f"UPDATE capture_events SET tags = {chips_sql('capture_events.slug')} WHERE {where};"

    p = CHIP_TRIGGER_PREFIX
    trig = {}

    def add(name, when, table, body, cond=""):
        trig[p + name] = f"CREATE TRIGGER IF NOT EXISTS {p}{name} {when} ON {table}{cond} BEGIN {body} END"

    for table, short in (("post_tags", "pt"), ("project_items", "pi")):
        add(f"{short}_ai", "AFTER INSERT", table, refresh("slug = NEW.post_slug"))
        add(f"{short}_ad", "AFTER DELETE", table, refresh("slug = OLD.post_slug"))
    add("bt_au", "AFTER UPDATE OF name", "blog_tags",
        refresh("slug IN (SELECT post_slug FROM post_tags WHERE tag_id = NEW.id)"))
    add("pr_au", "AFTER UPDATE OF tag_id", "projects",
        refresh("slug IN (SELECT post_slug FROM project_items WHERE project_id = NEW.id)"))
    add("pr_ad", "AFTER DELETE", "projects",
        refresh("slug IN (SELECT post_slug FROM project_items WHERE project_id = OLD.id)"))
    add("ce_ai", "AFTER INSERT", "capture_events", refresh("id = NEW.id"))
    # A direct write of the column (an old row image, a stray raw UPDATE) is put back to the derived
    # value. With recursive triggers off (SQLite's default) this trigger's own UPDATE doesn't re-fire it.
    add("ce_au", "AFTER UPDATE OF tags", "capture_events", refresh("id = NEW.id"),
        cond=f" WHEN NEW.tags IS NOT {chips_sql('NEW.slug')}")
    return trig


def _ensure_chip_triggers(conn, force=False):
    """Creates (or re-creates, when their definition changed) the #555 chip-cache triggers, once
    the tags_one_store_555 migration is recorded (`force`: the migration itself, before it is).
    Idempotent; both processes run it at boot."""
    if not force and not conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION_555,)).fetchone():
        return False
    stored = {r["name"]: r["sql"] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE ?", (CHIP_TRIGGER_PREFIX + "%",))}

    def _norm(sql):
        return " ".join((sql or "").replace("IF NOT EXISTS ", "").split())

    for name, ddl in _chip_triggers().items():
        if name in stored and _norm(stored[name]) != _norm(ddl):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(ddl)
    return True


def _refresh_chip_cache(conn, slugs=None):
    """Rewrites capture_events.tags from the tree for `slugs` (None = every row). The cache is
    derived; this is the migration's one full pass and a repair. Returns the rows rewritten."""
    sql = f"UPDATE capture_events SET tags = {chips_sql('capture_events.slug')} WHERE tags IS NOT {chips_sql('capture_events.slug')}"
    if slugs is None:
        return conn.execute(sql).rowcount
    slugs = list(slugs)
    if not slugs:
        return 0
    return conn.execute(sql + f" AND slug IN ({','.join('?' for _ in slugs)})", slugs).rowcount


def check_chip_cache():
    """[slug] whose cached chips differ from the tree (empty = in step). A read."""
    conn = get_conn()
    try:
        return [r["slug"] for r in conn.execute(
            f"SELECT slug FROM capture_events WHERE tags IS NOT {chips_sql('capture_events.slug')}")]
    finally:
        conn.close()


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


# #667: how long init_db waits for another process's write lock before its schema step (see there).
SCHEMA_LOCK_TIMEOUT = 600


def _exec_script(conn, script):
    """Runs a multi-statement DDL script one statement at a time. Not conn.executescript(): that
    COMMITs any open transaction first, which would end init_db's schema transaction (#667) half
    way through and reopen the race. sqlite3.complete_statement decides where each statement ends
    (so a ';' inside a comment, a string or a trigger body doesn't split it)."""
    buf = ""
    for piece in script.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            if any(line.strip() not in ("", ";") and not line.strip().startswith("--") for line in buf.splitlines()):
                conn.execute(buf)
            buf = ""


def _add_column(conn, table, column, ddl_type):
    """ALTER TABLE <table> ADD COLUMN <column> <ddl_type>, for init_db's "missing? add it" steps."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    except sqlite3.OperationalError as e:
        # #667 belt and braces: init_db checks and adds under one write lock, so another process
        # can't add this column between our check and our ALTER. If one ever does anyway, the
        # column we wanted is there, so that is success. Only this exact error for this exact
        # column; anything else (a bad type, a locked DB, a missing table) still raises.
        if str(e) != f"duplicate column name: {column}":
            raise
        print(f"init_db: {table}.{column} was already added by another process (#667); carrying on", flush=True)


def init_db(migrate=True):
    """Boot-time schema setup.

    #549: ONE process owns data migrations -- the web app. `migrate=True` (the default, also
    what scripts/tests get) runs schema DDL and then every pending one-time data migration
    (see MIGRATIONS / run_pending_migrations). The MCP server passes `migrate=False`: it only
    runs the idempotent schema DDL below (CREATE IF NOT EXISTS / ADD COLUMN) so its queries
    never hit a missing column, and logs any migration web hasn't applied yet.

    #667: both processes boot at the same moment after an upgrade, so the schema step is ONE
    `transaction()` (BEGIN IMMEDIATE, SQLite's write lock taken before the first read). The
    second process waits for the first to commit, then reads the finished schema and has nothing
    left to do: every "is it there? no -> add it" check below happens under the lock. The wait is
    up to SCHEMA_LOCK_TIMEOUT, not the usual 30 s, because the web app's data migrations (each in
    its own transaction, after this) can hold the lock for a while on a big archive (e.g. a
    search-index rebuild); the MCP then just boots a little later instead of crash-looping on
    "database is locked"."""
    waited = time.monotonic()
    with transaction(timeout=SCHEMA_LOCK_TIMEOUT) as tx:
        waited = time.monotonic() - waited
        if waited > 1:
            print(f"init_db: waited {waited:.1f}s for another process's write lock before the schema step", flush=True)
        conn = tx.conn
        _exec_script(conn, SCHEMA)
        _trash_expires_nullable(conn)
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(capture_events)")}
        for column, ddl_type in (("file_size", "INTEGER"), ("source_modified_at", "REAL"), ("ocr_status", "TEXT"), ("ocr_started_at", "REAL"), ("perceptual_hash", "TEXT")):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        # display_date_override (Timeline feature): manual override for an object's
        # position on the timeline. NULL means "use the computed default" — see
        # core/timeline.py's resolve_item_date.
        for column, ddl_type in (("display_date_override", "REAL"),):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        # media_type is a loose classifier ('image' | 'youtube' | 'document', or anything else a
        # caller wants) — deliberately no CHECK constraint. See core/object_types.py for the
        # registry that gives each value a real spec (thumbnail strategy, OCR eligibility,
        # per-type metadata fields); a media_type with no registered spec just falls back to
        # object_types.DEFAULT_SPEC rather than erroring. Existing rows predate this column and
        # are all screenshots, so they default to 'image' below.
        if "media_type" not in existing_columns:
            _add_column(conn, "capture_events", "media_type", "TEXT NOT NULL DEFAULT 'image'")
        for column, ddl_type in (("external_url", "TEXT"), ("content_description", "TEXT"), ("content_date", "REAL")):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        # type_metadata: a freeform JSON bag for per-object-type properties that
        # don't fit the generic columns above (see core/object_types.py's
        # MetadataField) — e.g. a future PDF's page count, an STL's dimensions.
        # Deliberately one shared column rather than a new ALTER TABLE per type,
        # so registering a new object type never requires a schema migration.
        if "type_metadata" not in existing_columns:
            _add_column(conn, "capture_events", "type_metadata", "TEXT NOT NULL DEFAULT '{}'")
        # tag_id: links a project to a blog_tags row (cards.create links the root
        # tag of the same name at creation; the link is the id, so the names can
        # drift apart afterwards) so tagging an object with a project
        # also surfaces it through the site's ordinary tag-based browsing (any
        # consumer of list_posts_for_tag; the home page's old ?tag=<slug> filter is
        # gone, its pills filter by ?hobby= since #370). Existing pre-#1 projects (e.g. the ones
        # from scripts/archive/seed_example_projects.py) predate this and simply have
        # tag_id = NULL — they still work everywhere, they just aren't reachable
        # via a tag filter until someone links one up by hand.
        existing_project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
        if "tag_id" not in existing_project_columns:
            _add_column(conn, "projects", "tag_id", "INTEGER REFERENCES blog_tags(id)")
        # display_name (#11): an optional per-object title override (core/item_title.py). Its twin,
        # the per-object `icon` override, was never used and is dropped by #704.
        # short_title (#542, owner decision 2026-10-18): the item's short name, between the rename and
        # the filename in the title chain; the long caption (content_description) is never the title.
        # Schema DDL (both processes); the `short_title_seed_542` migration fills it once.
        for column, ddl_type in (("display_name", "TEXT"), ("short_title", "TEXT")):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        # #542 / #743: the caption queue also carries short-title proposals (mode 'title') and the
        # change-log batch they belong to, so one undo takes a whole backfill back.
        existing_queue_columns = {row["name"] for row in conn.execute("PRAGMA table_info(caption_queue)")}
        for column, ddl_type in (("mode", "TEXT NOT NULL DEFAULT 'caption'"), ("batch_id", "TEXT")):
            if column not in existing_queue_columns:
                _add_column(conn, "caption_queue", column, ddl_type)
        # parent_id (#133): support for nested projects — projects can now have a
        # parent project, enabling a simple hierarchy. Mirrors the same self-referencing
        # pattern as blog_tags.parent_id for consistency.
        if "parent_id" not in existing_project_columns:
            _add_column(conn, "projects", "parent_id", "INTEGER REFERENCES projects(id)")
        # Create the index after the column is guaranteed to exist
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_parent ON projects(parent_id)")
        # cover_project_id (#356): a project can borrow a child project's cover instead of
        # hoisting grandchild objects. Mutually exclusive with cover_slug — see update_project.
        if "cover_project_id" not in existing_project_columns:
            _add_column(conn, "projects", "cover_project_id", "INTEGER REFERENCES projects(id)")
        # writeup_slug (#156): optional reference to a document-type capture_event that
        # serves as the project's write-up/article. Follows the same pattern as cover_slug —
        # points at a capture_events.slug with no FK constraint.
        if "writeup_slug" not in existing_project_columns:
            _add_column(conn, "projects", "writeup_slug", "TEXT")
        # is_hobby (#360): marks a blog_tags row as a hobby (top-level organizing tier).
        # Hobbies are just tags with is_hobby=1 and an optional hobby_status.
        existing_blog_tags_columns = {row["name"] for row in conn.execute("PRAGMA table_info(blog_tags)")}
        if "is_hobby" not in existing_blog_tags_columns:
            _add_column(conn, "blog_tags", "is_hobby", "INTEGER NOT NULL DEFAULT 0")
        if "hobby_status" not in existing_blog_tags_columns:
            _add_column(conn, "blog_tags", "hobby_status", "TEXT")
        # group_code (V2 cards 3.9): the 2-4 char hobby code shown on a card (COL, 3DP, RCA).
        if "group_code" not in existing_blog_tags_columns:
            _add_column(conn, "blog_tags", "group_code", "TEXT")
        # project_hobbies (#360): many-to-many join between projects and hobbies (tags).
        # A project can be associated with multiple hobbies via project_hobbies.
        _exec_script(conn, """
        CREATE TABLE IF NOT EXISTS project_hobbies (
            project_id INTEGER NOT NULL REFERENCES projects(id),
            hobby_tag_id INTEGER NOT NULL REFERENCES blog_tags(id),
            PRIMARY KEY (project_id, hobby_tag_id)
        );
        CREATE INDEX IF NOT EXISTS idx_project_hobbies_hobby ON project_hobbies(hobby_tag_id);
        """)
        # start_date_override/end_date_override (Timeline feature): a project is a
        # span, not a moment — these manually override its computed start/end
        # (derived from its items' effective dates otherwise). NULL means "use the
        # computed default" — see core/timeline.py's resolve_project_span.
        for column, ddl_type in (("start_date_override", "REAL"), ("end_date_override", "REAL")):
            if column not in existing_project_columns:
                _add_column(conn, "projects", column, ddl_type)
        # agent_notes (#206): reserved space for agent-authored working notes, separate
        # from owner-facing content. Used for Claude working state (e.g., "this is a
        # Fusion 360 screenshot, not user-facing" or "already inventoried, skip on re-run").
        # Never exposed through public HTTP API — agent-only scratch space.
        if "agent_notes" not in existing_columns:
            _add_column(conn, "capture_events", "agent_notes", "TEXT")
        # projects.agent_notes (#745): the same private scratch field on a CARD. Schema DDL like
        # every other column (both processes, so the MCP that writes it never waits on web's
        # migrations); NULL = none, so nothing to backfill. It never rides in a card row the readers
        # below hand out (_card_row strips it); only get_card_agent_notes reads it.
        if "agent_notes" not in existing_project_columns:
            _add_column(conn, "projects", "agent_notes", "TEXT")
        # provenance + highlight (#341): Curator Stage 1 data model. provenance is a
        # controlled-vocab extensible field describing how the object came to be (found,
        # created, documented, result, reference, design). highlight is a boolean
        # flag for "cool/unique" objects, featured in export.
        for column, ddl_type in (("provenance", "TEXT"), ("highlight", "INTEGER NOT NULL DEFAULT 0")):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        # is_brand_asset + brand_role (#350): Brand kit data model. is_brand_asset flags
        # reusable branding objects (logo, logotype, icon, color, etc.); brand_role is
        # a freeform label describing their purpose.
        for column, ddl_type in (("is_brand_asset", "INTEGER NOT NULL DEFAULT 0"), ("brand_role", "TEXT")):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        # --- V2 cards, piece 1 (docs/design/v2-cards.md 3.1, 3.2, 3.13) ---
        # kind: what sort of card this is (project/thing/action/family/collection/event).
        # No CHECK -- core/card_rules.validate_kind is the gate (same stance as media_type).
        # activity/stage/stop_reason: the live (and only) status. The legacy v1
        # projects.status column was retired in #545 and dropped in #760
        # (_drop_project_status_760, web boot).
        for column, ddl_type in (
            ("kind", "TEXT NOT NULL DEFAULT 'project'"),
            ("activity", "TEXT"),
            ("stage", "TEXT"),
            ("stop_reason", "TEXT"),
        ):
            if column not in existing_project_columns:
                _add_column(conn, "projects", column, ddl_type)
        # V2 cards piece 5 (3.4, 3.5, 3.12): whereabouts (+ note), card-level provenance
        # (+ credit) and the card highlight flag. All empty/0 after migration; nothing is
        # guessed. Validation lives in core/card_rules (no CHECK constraints).
        for column, ddl_type in (
            ("whereabouts", "TEXT"),
            ("whereabouts_note", "TEXT"),
            ("provenance", "TEXT"),
            ("provenance_credit", "TEXT"),
            ("highlight", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing_project_columns:
                _add_column(conn, "projects", column, ddl_type)
        # V2 cards piece 6 (3.10): manual home override for the breadcrumb / export link.
        # NULL = automatic (parent, then first family, then first hobby). Not curated.
        for column, ddl_type in (("home_kind", "TEXT"), ("home_ref", "INTEGER")):
            if column not in existing_project_columns:
                _add_column(conn, "projects", column, ddl_type)
        # #596: the card face's text box. synopsis (a few hand-written sentences) and flavor (one
        # italic line) are set through cards.set_text; writeup_lead is a CACHE of the write-up's
        # opening paragraph(s) (plain text, clamped), kept by the cards service whenever the
        # write-up's body or writeup_slug changes, so a card list never reads write-up bodies.
        for column, ddl_type in (("synopsis", "TEXT"), ("flavor", "TEXT"), ("writeup_lead", "TEXT")):
            if column not in existing_project_columns:
                _add_column(conn, "projects", column, ddl_type)
        # #596: a hobby's own synopsis / flavor live with its other per-hobby settings.
        existing_hobby_settings_columns = {row["name"] for row in conn.execute("PRAGMA table_info(hobby_settings)")}
        # #686: cover_slug = the hobby's chosen cover (a capture_events slug, NULL = automatic: its
        # highlighted project's cover, else its most recent project's; see cards.resolve_hobby_cover).
        # #554 (batch 5): cover_project_id = the member card whose cover the hobby borrows (and
        # follows when that card's cover changes), like projects.cover_project_id. NULL = none;
        # cover_slug and cover_project_id are never both set (hobbies.set_cover).
        for column, ddl_type in (("synopsis", "TEXT"), ("flavor", "TEXT"), ("cover_slug", "TEXT"),
                                 ("cover_project_id", "INTEGER")):
            if column not in existing_hobby_settings_columns:
                _add_column(conn, "hobby_settings", column, ddl_type)
        # #604 step 1: ownership. The user who uploaded an item / created a card, hobby or blog entry
        # (users.id), set at write time from the actor context; NULL = no signed-in user did it (the
        # install token, the MCP, a script, the system) = admin-owned. #603 (#604 step 2): the
        # per-item "This is sensitive" flag, plus who set it (the actor string) and when.
        for column, ddl_type in (("uploaded_by_user_id", "INTEGER"), ("sensitive", "INTEGER NOT NULL DEFAULT 0"),
                                 ("sensitive_by", "TEXT"), ("sensitive_at", "REAL")):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        if "created_by_user_id" not in existing_project_columns:
            _add_column(conn, "projects", "created_by_user_id", "INTEGER")
        if "created_by_user_id" not in existing_hobby_settings_columns:
            _add_column(conn, "hobby_settings", "created_by_user_id", "INTEGER")
        existing_blog_entry_columns = {row["name"] for row in conn.execute("PRAGMA table_info(blog_entries)")}
        if "created_by_user_id" not in existing_blog_entry_columns:
            _add_column(conn, "blog_entries", "created_by_user_id", "INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_capture_events_sensitive ON capture_events(sensitive)")
        # #560: `channel` = HOW an item arrived (web | mcp | script | system | migration:<source>), split
        # from the free-text Source label `tech` (kept as the Source note); filled for old rows once by
        # the `capture_channel_560` migration. #778: the personal MCP key (mcp_tokens.id) a user's item,
        # card, hobby, blog entry or change-log row was made with; NULL = none (the web, the install
        # token). The credit text is rendered from the token's current name at read time.
        for column, ddl_type in (("channel", "TEXT"), ("uploaded_via_token_id", "INTEGER")):
            if column not in existing_columns:
                _add_column(conn, "capture_events", column, ddl_type)
        if "created_via_token_id" not in existing_project_columns:
            _add_column(conn, "projects", "created_via_token_id", "INTEGER")
        if "created_via_token_id" not in existing_hobby_settings_columns:
            _add_column(conn, "hobby_settings", "created_via_token_id", "INTEGER")
        if "created_via_token_id" not in existing_blog_entry_columns:
            _add_column(conn, "blog_entries", "created_via_token_id", "INTEGER")
        # #605: an unpack job replays its starter's channel and personal MCP key in the web worker
        # (an install that ran an earlier build of the branch has the table without them).
        existing_unpack_columns = {row["name"] for row in conn.execute("PRAGMA table_info(unpack_jobs)")}
        for column, ddl_type in (("channel", "TEXT"), ("via_token_id", "INTEGER")):
            if existing_unpack_columns and column not in existing_unpack_columns:
                _add_column(conn, "unpack_jobs", column, ddl_type)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_capture_events_owner ON capture_events(uploaded_by_user_id)")
        # #604 step 3: a PRIVATE card or hobby (0 = open, the default). Who else may see it is
        # share_members; core/policy.py decides, core/sharing.py writes.
        if "private" not in existing_project_columns:
            _add_column(conn, "projects", "private", "INTEGER NOT NULL DEFAULT 0")
        if "private" not in existing_hobby_settings_columns:
            _add_column(conn, "hobby_settings", "private", "INTEGER NOT NULL DEFAULT 0")
        # Change log: audit_log grows nullable columns so core operations can record
        # row images (before/after) for audit + undo. Direct HTTP callers keep getting
        # the old request-log row (these columns NULL there).
        existing_audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)")}
        # #557: required_role = the role label of the route a request-log row is about (web/roles.py).
        # #646: client_ip = the request's client address (the real client behind a trusted proxy,
        # web/proxy_trust.py); NULL on change-log rows and on request-log rows from before #646.
        for column, ddl_type in (("op", "TEXT"), ("actor", "TEXT"), ("batch_id", "TEXT"),
                                 ("mutations", "TEXT"), ("undone_by", "INTEGER"), ("required_role", "TEXT"),
                                 ("client_ip", "TEXT"), ("via_token_id", "INTEGER")):
            if column not in existing_audit_columns:
                _add_column(conn, "audit_log", column, ddl_type)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_batch ON audit_log(batch_id)")
        # family_members (V2 cards 3.6): many-to-many membership for kind=family and
        # kind=collection cards. Not nesting: membership never moves or copies files.
        _exec_script(conn, """
        CREATE TABLE IF NOT EXISTS family_members (
            family_id  INTEGER NOT NULL REFERENCES projects(id),
            member_id  INTEGER NOT NULL REFERENCES projects(id),
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at REAL    NOT NULL,
            PRIMARY KEY (family_id, member_id)
        );
        CREATE INDEX IF NOT EXISTS idx_family_members_member ON family_members(member_id);
        """)
        # Editable provenance lists (#529): the table is schema (both processes); its seed rows
        # are the one-time `provenance_options_seed_529` migration below.
        from . import provenance_options
        conn.execute(provenance_options.DDL)
        # Editable brand roles + wallpaper tags (#556): schema here, seed = `vocab_seed_556` below.
        from . import vocab
        conn.execute(vocab.DDL)
        _rebuild_project_relations_typed(conn)
        _ensure_search_schema(conn)  # #543: after every column it reads exists
        _ensure_chip_triggers(conn)  # #555: the derived chip cache (once its migration has run)
    # leaving the block commits the whole schema step (an exception rolls all of it back)
    if migrate:
        run_pending_migrations()
        _run_drop_dead_columns_704()  # #704: web only, every boot; never refuses to start
        _run_drop_project_status_760()  # #760: the same, for the retired projects.status
    else:
        pending = pending_migrations()
        if pending:
            print(f"schema_migrations: {len(pending)} migration(s) pending ({', '.join(pending)}); "
                  "web owns data migrations and hasn't booted on this DB yet", flush=True)


# --- One-time data migrations (#549) ----------------------------------------------------
# Each step is a named function that runs at most once ever per DB: check-and-apply-and-record
# happen inside ONE `transaction()` (BEGIN IMMEDIATE), so a concurrent boot blocks on the write
# lock, then sees the row and skips. Every step is ALSO still idempotent on its own (they all
# were before this table existed), which is what makes an existing DB safe: on prod/test the
# first run of each step finds its effect already present, changes nothing (an owner decision
# already resolved is never re-queued -- queue_decision_once skips open OR resolved rows), and
# just records itself. Only the web process calls run_pending_migrations.

def _mig_drop_legacy_uploads():
    # Pre-generic-schema table from before the capture_events rework -- sample data only, safe
    # to drop rather than migrate. Confirmed with Jason 2026-08-27.
    get_conn().execute("DROP TABLE IF EXISTS uploads")


def _mig_curator_snooze_519():
    # #519: the timed snooze is gone, replaced by Defer (no wait time). Snoozes still in
    # effect become deferrals; expired ones are dropped (they had already come back).
    conn = get_conn()
    conn.execute("UPDATE curator_dismissals SET action = 'defer', snooze_until = NULL "
                 "WHERE action = 'snooze' AND snooze_until IS NOT NULL AND snooze_until > ?", (time.time(),))
    conn.execute("DELETE FROM curator_dismissals WHERE action = 'snooze'")


def _mig_audit_scrub_559():
    # #559: scrub saved setting values out of old audit rows. Before the route-driven
    # redaction, POST /api/settings logged {"key": ..., "value": <the secret>}. New rows are
    # redacted at write time, so this only ever needs to run once.
    conn = get_conn()
    for row in conn.execute("SELECT id, form_body FROM audit_log WHERE path = '/api/settings'").fetchall():
        try:
            body = json.loads(row["form_body"]) if row["form_body"] else {}
        except (ValueError, TypeError):  # silent-ok: an unreadable row is scrubbed whole by the else below
            body = None
        if isinstance(body, dict):
            if "value" not in body or body["value"] == "[REDACTED]":
                continue
            body["value"] = "[REDACTED]"
        elif body is not None and not body:
            continue
        else:
            body = {"_body": "[REDACTED]"}
        conn.execute("UPDATE audit_log SET form_body = ? WHERE id = ?", (json.dumps(body), row["id"]))


# #559: the paths whose audit rows may hold a secret (see web/request_guard.py AUDIT_ROUTE_RULES;
# core can't import web, so the list is repeated here and scripts/test_audit_secrets_559.py checks
# the two stay in step). Matched after stripping trailing slashes.
_AUDIT_SECRET_NAME_ONLY_559 = {"/api/settings"}
_AUDIT_SECRET_NO_BODY_559 = {"/api/auth/login", "/api/auth/setup", "/api/account/password", "/api/users"}
_AUDIT_SECRET_NO_BODY_RE_559 = re.compile(r"^/api/users/[^/]+/password$")
_AUDIT_SETTING_NAME_559 = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_AUDIT_MARKER_FIELDS_559 = {"_unparsed", "content_type", "bytes", "_body"}
_AUDIT_NOT_LOGGED_559 = "not logged: this route carries secrets or file bodies"
_AUDIT_CODE_559 = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _scrubbed_audit_body_559(path, raw):
    """The form_body to store for an old audit row on a secret-bearing path, or None to leave the
    row alone. Idempotent: scrubbing a scrubbed body returns None."""
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, TypeError):  # silent-ok: an unreadable body on a secret route is replaced whole below
        body = None
    if isinstance(body, dict) and not body:
        return None  # nothing was sent
    if isinstance(body, dict) and set(body) <= _AUDIT_MARKER_FIELDS_559:
        return None  # already a marker (unparsed / not logged / redacted): holds no field values
    if path in _AUDIT_SECRET_NAME_ONLY_559 and isinstance(body, dict):
        new = {k: (v if k == "key" and isinstance(v, str) and _AUDIT_SETTING_NAME_559.match(v) else "[REDACTED]")
               for k, v in body.items()}
    elif path in _AUDIT_SECRET_NAME_ONLY_559:
        new = {"_body": "[REDACTED]"}
    else:
        new = {"_body": _AUDIT_NOT_LOGGED_559}
    return None if new == body else json.dumps(new)


def _scrubbed_audit_error_559(raw):
    """A refused request's reason on a secret-bearing path keeps only its code (what the live
    middleware does since #583); None = leave it. A bare code-like word is already just a code."""
    if not raw or raw == "[REDACTED]":
        return None
    code, sep, _ = raw.partition(": ")
    if sep:
        return code
    return None if _AUDIT_CODE_559.match(raw) else "[REDACTED]"


def _mig_audit_scrub_secret_routes_559():
    # #559: the first scrub (audit_scrub_settings_559) only looked at the exact path
    # '/api/settings' and only rewrote the `value` field. This one covers every secret-bearing
    # route and its trailing-slash spelling, and every field. Rewrites ONLY audit_log.form_body
    # and audit_log.error_detail of rows on those paths. Never prints a value, only a count.
    conn = get_conn()
    scrubbed = 0
    rows = conn.execute(
        "SELECT id, path, form_body, error_detail FROM audit_log "
        "WHERE path LIKE '/api/settings%' OR path LIKE '/api/auth/%' OR path LIKE '/api/account/password%' "
        "OR path LIKE '/api/users%'").fetchall()
    for row in rows:
        path = (row["path"] or "").rstrip("/")
        if path not in _AUDIT_SECRET_NAME_ONLY_559 and path not in _AUDIT_SECRET_NO_BODY_559 \
                and not _AUDIT_SECRET_NO_BODY_RE_559.match(path):
            continue
        new_body = _scrubbed_audit_body_559(path, row["form_body"])
        new_error = _scrubbed_audit_error_559(row["error_detail"])
        if new_body is None and new_error is None:
            continue
        conn.execute("UPDATE audit_log SET form_body = ?, error_detail = ? WHERE id = ?",
                     (row["form_body"] if new_body is None else new_body,
                      row["error_detail"] if new_error is None else new_error, row["id"]))
        scrubbed += 1
    print(f"schema_migrations: audit_scrub_secret_routes_559 scrubbed {scrubbed} of {len(rows)} audit rows on secret-bearing paths", flush=True)


def _mig_provenance_options_seed_529():
    # INSERT OR IGNORE only: never renames, un-retires or duplicates anything the owner changed.
    from . import provenance_options
    provenance_options.ensure_table_and_seed(get_conn())


def _mig_vocab_seed_556():
    # INSERT OR IGNORE only: the old db.BRAND_ROLES / WALLPAPER_TAG_NAMES constants become the lists.
    from . import vocab
    vocab.ensure_seed(get_conn())


def _mig_install_config_seed_562():
    # #562: an install that already has content gets today's hard-wired owner values, so it sees
    # no change; a fresh (empty) install gets nothing owner-specific. See core/install_config.py.
    from . import install_config
    install_config.seed_existing_install(get_conn())


def _mig_hobby_physical_piece_562():
    # #562: the PHYSICAL PIECE fields used to show for the hobby NAMED "Traditional Media". It's a
    # per-hobby flag now; the hobby of that name (if this install has one) keeps it switched on.
    from . import changes, physical_piece
    conn = get_conn()
    rows = conn.execute("SELECT id, slug FROM blog_tags WHERE is_hobby = 1 AND lower(trim(name)) = ?",
                        (physical_piece.LEGACY_HOBBY_NAME,)).fetchall()
    for r in rows:
        if conn.execute("INSERT OR IGNORE INTO hobby_settings (hobby_tag_id, shows_physical_piece) VALUES (?, 1)",
                        (r["id"],)).rowcount:
            insert_change_log(conn, "migration_hobby_physical_piece_562", changes.ACTOR_MIGRATION,
                              [changes.row_image("hobby_settings", {"hobby_tag_id": r["id"]}, None,
                                                 {"hobby_tag_id": r["id"], "shows_physical_piece": 1})],
                              batch_id=changes.new_batch_id(), affected_slugs=[r["slug"]])


def _mig_empty_file_ocr_done_587():
    # #587 item 1: a zero-byte file has nothing to read, so OCR settles it as done with empty text
    # (core/ocr.py does that for new uploads). Re-settle the rows that an earlier version marked
    # failed: only rows whose stored file is really zero bytes on disk. Idempotent.
    from . import storage
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, stored_filename FROM capture_events "
        "WHERE ocr_status = 'failed' AND stored_filename IS NOT NULL AND file_size = 0").fetchall()
    for r in rows:
        try:
            empty = storage.path_for(r["stored_filename"]).stat().st_size == 0
        except OSError:  # silent-ok: file missing or unreadable: not provably empty, leave the row failed
            empty = False
        if empty:
            conn.execute("UPDATE capture_events SET ocr_status = 'done', extracted_text = '' WHERE slug = ?",
                         (r["slug"],))


def _mig_ocr_unsupported_724():
    # #724: an item with nothing OCR can read (no text source and no picture: a web link today)
    # used to settle as 'failed', a red lamp for something that never went wrong. core/ocr.py now
    # settles those as 'unsupported' with a reason; re-settle the old failed rows the same way.
    # A row qualifies only by object_types.nothing_to_read(row, spec), the exact test new OCR runs use: its
    # type has no text_extract_fn AND no picture is expected for it. So an image (its own file is
    # the picture), a YouTube video (a fetched thumbnail), a PDF or any type with a text source
    # is never touched: their 'failed' is a real error and stays. Redacted rows are left alone.
    # Idempotent: a second run finds no failed row that qualifies.
    from . import object_types
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, media_type, filename, stored_filename, external_url, type_metadata FROM capture_events "
        "WHERE ocr_status = 'failed' AND COALESCE(redacted, 0) = 0").fetchall()
    moved = {}
    for r in rows:
        row = dict(r)
        try:
            row["type_metadata"] = json.loads(row["type_metadata"] or "{}")
        except ValueError:  # silent-ok: unreadable JSON: not provably "nothing to read", leave the row failed
            continue
        spec = object_types.get_object_type(row.get("media_type"))
        if spec is None or not spec.ocr_capable or not object_types.nothing_to_read(row, spec):
            continue
        note_sql, note_args = _ocr_note_sql(object_types.ocr_unsupported_note(row, spec))
        conn.execute(f"UPDATE capture_events SET ocr_status = ?, {note_sql} WHERE slug = ? AND ocr_status = 'failed'",
                     (OCR_UNSUPPORTED, *note_args, row["slug"]))
        moved[row["media_type"]] = moved.get(row["media_type"], 0) + 1
    print(f"schema_migrations: ocr_unsupported_724 checked {len(rows)} failed row(s), re-settled "
          f"{sum(moved.values())} as unsupported {moved or ''}".rstrip(), flush=True)


def _mig_reread_legacy_office_724b():
    # #724 part b (owner decision 2026-10-17): legacy .doc / .ppt have text readers now, so the rows
    # that settled before them are re-read once: 'failed' (part a and earlier had no reader, or
    # one that couldn't open them) and 'unsupported' (part a's "no readable text yet"). They go back
    # to 'pending' (fresh ocr_started_at, stale ocr_note removed); web's startup self-heal, which runs
    # right after init_db, re-runs OCR on every pending row, and the readers decide afresh (text,
    # "not compatible" with a reason, or "corrupt" with a reason). Matched by the stored file's
    # extension, which is what the readers dispatch on. Redacted rows are left alone. Idempotent:
    # recorded once in schema_migrations, and a second run would only re-queue rows that a re-read
    # settled the same way again.
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug FROM capture_events WHERE ocr_status IN ('failed', ?) AND COALESCE(redacted, 0) = 0 "
        "AND media_type IN ('word', 'powerpoint') "
        "AND (lower(stored_filename) LIKE '%.doc' OR lower(stored_filename) LIKE '%.ppt')",
        (OCR_UNSUPPORTED,)).fetchall()
    note_sql, _ = _ocr_note_sql(None)
    now = time.time()
    for r in rows:
        conn.execute(f"UPDATE capture_events SET ocr_status = 'pending', ocr_started_at = ?, {note_sql} WHERE slug = ?",
                     (now, r["slug"]))
    print(f"schema_migrations: reread_legacy_office_724b re-queued {len(rows)} legacy .doc/.ppt row(s) for OCR",
          flush=True)


def _mig_text_sources_724c():
    # #724 part c (owner decision 2026-10-17: descriptions first, free, no network): a YouTube
    # video's stored description (type_metadata.description) becomes searchable extracted text,
    # concatenated with the thumbnail's OCR text under headed sections, text_source 'description'.
    # New rows get it from core/ocr.py; this re-settles the existing ones once, by the same rule
    # (core/text_sources.resettle), for every type with a text_sources_fn (YouTube today):
    # - a settled row ('done' / 'unsupported') whose sources hold text: extracted_text rebuilt, the
    #   OCR text already stored kept as its "Thumbnail text:" section (never lost);
    # - a settled row with no source text and no thumbnail text: 'unsupported' with the type's note
    #   ("video: no description or captions yet", grey), part a's rule;
    # - pending rows are left to their OCR run, 'failed' rows keep their real error until Retry,
    #   redacted rows and rows with unreadable type_metadata JSON are left alone.
    # No network, no OCR. Idempotent: resettle returns None for a row already settled this way, so a
    # second run changes nothing. The FTS index follows through the UPDATE OF extracted_text trigger.
    from . import object_types, text_sources
    specs = {k: s for k, s in object_types.OBJECT_TYPES.items() if s.ocr_capable and s.text_sources_fn is not None}
    if not specs:
        return
    conn = get_conn()
    marks = ", ".join("?" for _ in specs)
    rows = conn.execute(
        "SELECT slug, media_type, filename, stored_filename, external_url, extracted_text, ocr_status, type_metadata "
        f"FROM capture_events WHERE media_type IN ({marks}) AND ocr_status IN ('done', ?) "
        "AND COALESCE(redacted, 0) = 0", (*specs, OCR_UNSUPPORTED)).fetchall()
    combined = unsupported = 0
    for r in rows:
        row = dict(r)
        try:
            row["type_metadata"] = json.loads(row["type_metadata"] or "{}")
        except ValueError:  # silent-ok: unreadable JSON: leave the row exactly as it is
            continue
        spec = specs[row["media_type"]]
        change = text_sources.resettle(row, spec, object_types.ocr_unsupported_note(row, spec))
        if change is None:
            continue
        meta = text_sources.resettled_meta(row["type_metadata"], change)
        conn.execute("UPDATE capture_events SET extracted_text = ?, ocr_status = ?, type_metadata = ? WHERE slug = ?",
                     (change["text"], change["status"], json.dumps(meta), row["slug"]))
        if change["status"] == OCR_UNSUPPORTED:
            unsupported += 1
        else:
            combined += 1
    print(f"schema_migrations: text_sources_724c checked {len(rows)} settled row(s) of {sorted(specs)}: "
          f"{combined} given their description text, {unsupported} re-settled as unsupported (no text at all)",
          flush=True)


def _mig_link_page_text_724d():
    # #724 part d (owner decision 2026-10-17, "Title + summary, safe fetch"): web links read their
    # page's title and summary now (core/object_types/url.py page_text, through core/safe_fetch.py).
    # The url rows that settled before it ('unsupported' "web link: no readable text yet" from part
    # a, or 'failed' on an install where part a found them first) go back to 'pending' once (fresh
    # ocr_started_at, stale ocr_note removed); web's startup self-heal, right after init_db, re-runs
    # OCR on every pending row. The fetches themselves are polite: core/safe_fetch.py lets one page
    # fetch run at a time per container with a pause between them (CONSTRUCTICON_LINK_FETCH_DELAY_
    # SECONDS, default 2), so a backfill of many links to one site never bursts. No network here.
    # Rows with no external_url, redacted rows and every other type are left alone. Idempotent:
    # recorded once in schema_migrations; a second run would only re-queue rows a fetch settled the
    # same way again.
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug FROM capture_events WHERE media_type = 'url' AND ocr_status IN ('failed', ?) "
        "AND COALESCE(redacted, 0) = 0 AND COALESCE(TRIM(external_url), '') != ''",
        (OCR_UNSUPPORTED,)).fetchall()
    note_sql, _ = _ocr_note_sql(None)
    now = time.time()
    for r in rows:
        conn.execute(f"UPDATE capture_events SET ocr_status = 'pending', ocr_started_at = ?, {note_sql} WHERE slug = ?",
                     (now, r["slug"]))
    print(f"schema_migrations: link_page_text_724d re-queued {len(rows)} web link row(s) to read their page title "
          f"and summary", flush=True)


def _mig_reread_xls_790():
    # #790 (owner decision 2026-10-28): an .xls that holds no Excel workbook (the #720 sampler's stub,
    # an OLE file of something else) now settles grey "not compatible" (spreadsheet.xls_check), like
    # .doc / .ppt, instead of red "corrupt or unreadable .xls". The .xls rows that settled 'failed'
    # before that go back to 'pending' once (fresh ocr_started_at, stale ocr_note removed); web's
    # startup self-heal, right after init_db, re-runs OCR on every pending row and the reader decides
    # afresh: grey for a non-workbook, still red for a real workbook that is damaged (owner: keep the
    # split), text for one that reads. Matched by the stored file's extension, which the reader
    # dispatches on. Redacted rows, 'done' / 'unsupported' rows and every other type are left alone.
    # No file is read here. Idempotent: recorded once in schema_migrations; a second run would only
    # re-queue rows a re-read settled 'failed' again.
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug FROM capture_events WHERE media_type = 'spreadsheet' AND ocr_status = 'failed' "
        "AND COALESCE(redacted, 0) = 0 AND lower(COALESCE(stored_filename, '')) LIKE '%.xls'").fetchall()
    note_sql, _ = _ocr_note_sql(None)
    now = time.time()
    for r in rows:
        conn.execute(f"UPDATE capture_events SET ocr_status = 'pending', ocr_started_at = ?, {note_sql} WHERE slug = ?",
                     (now, r["slug"]))
    print(f"schema_migrations: reread_xls_790 re-queued {len(rows)} failed legacy .xls row(s) for OCR "
          f"({', '.join(r['slug'] for r in rows[:20])}{' ...' if len(rows) > 20 else ''})", flush=True)


CAPTION_BACKFILL_TYPES_556 = ("pdf", "ai")


def _mig_caption_pdf_ai_556():
    # #556 (owner decision 2026-10-28): PDF and Illustrator files are caption-capable now. Their
    # existing rows with no caption go into the normal caption pipeline once, exactly what Admin's
    # "Caption skipped items" (core/captions.queue_skipped) would pick: not redacted, no description
    # (content_description) yet, caption status absent / '' / 'failed' / 'skipped'. Deleted items are
    # not rows any more (they live in the trash), so they are never picked. Each is marked
    # auto_caption_status 'pending' and given a caption_queue row (db.enqueue_caption); web's caption
    # worker drains the queue one at a time behind the lock, the cooldown and the breaker (an open
    # breaker leaves the queue alone until it is reset), like any upload. With captioning off on this
    # install (CAPTION_DISABLED) nothing is queued and the log says how many were left and how to
    # queue them later (Admin > Caption skipped items). No model call here. Idempotent: recorded once
    # in schema_migrations; a second run finds them pending / done and picks nothing.
    from . import captions  # lazy: core.captions imports this module
    types = CAPTION_BACKFILL_TYPES_556
    status = "json_extract(type_metadata, '$.auto_caption_status')"
    conn = get_conn()
    rows = conn.execute(
        f"SELECT slug FROM capture_events WHERE media_type IN ({', '.join('?' * len(types))}) "
        "AND COALESCE(redacted, 0) = 0 AND COALESCE(TRIM(content_description), '') = '' "
        "AND (type_metadata IS NULL OR json_valid(type_metadata)) "
        f"AND ({status} IS NULL OR {status} = '' OR {status} IN ('failed', 'skipped'))", types).fetchall()
    garbled = conn.execute(
        f"SELECT slug FROM capture_events WHERE media_type IN ({', '.join('?' * len(types))}) "
        "AND type_metadata IS NOT NULL AND NOT json_valid(type_metadata)", types).fetchall()
    if garbled:  # never silent: these can't be marked pending without losing what the column holds
        print(f"schema_migrations: caption_pdf_ai_556 left out {len(garbled)} PDF / Illustrator row(s) whose "
              f"type_metadata isn't valid JSON: {', '.join(r['slug'] for r in garbled[:20])}; caption them "
              "from their page (Generate) once fixed", flush=True)
    if captions.DISABLED:
        print(f"schema_migrations: caption_pdf_ai_556 queued NOTHING: captions are off on this install "
              f"(CAPTION_DISABLED), so {len(rows)} PDF / Illustrator item(s) wait uncaptioned. Turn captioning "
              "on, then use Admin > Caption skipped items to queue them.", flush=True)
        return
    for r in rows:
        conn.execute("UPDATE capture_events SET type_metadata = json_set(COALESCE(type_metadata, '{}'), "
                     "'$.auto_caption_status', 'pending') WHERE slug = ?", (r["slug"],))
        enqueue_caption(r["slug"])
    print(f"schema_migrations: caption_pdf_ai_556 queued {len(rows)} PDF / Illustrator item(s) for a caption "
          "(the web worker runs them one at a time)", flush=True)


def _mig_transcripts_724e():
    # #724 part e (owner decision 2026-10-17: transcripts through youtube-transcript-api): every
    # existing row of a type with a network text source (YouTube today, ObjectTypeSpec.text_fetch_fn)
    # that has never had a transcript attempt is marked type_metadata.transcript_status = 'queued'.
    # No network here and no OCR re-run: web's _transcript_loop (web/app.py) works the queued rows a
    # few per pass, one fetch at a time (core/transcripts.py), and folds each result into the row's
    # stored text (core/text_sources.resettle). Rows that already have a transcript_status (an OCR
    # run since part e landed), redacted rows and rows with unreadable type_metadata are left alone.
    # Idempotent: recorded once in schema_migrations, and a second run finds no row without a status.
    from . import object_types
    types = [k for k, s in object_types.OBJECT_TYPES.items() if getattr(s, "text_fetch_fn", None) is not None]
    if not types:
        return
    conn = get_conn()
    marks = ", ".join("?" for _ in types)
    cur = conn.execute(
        "UPDATE capture_events SET type_metadata = json_set(CASE WHEN COALESCE(type_metadata, '') = '' THEN '{}' "
        "ELSE type_metadata END, '$.transcript_status', 'queued') "
        f"WHERE media_type IN ({marks}) AND COALESCE(redacted, 0) = 0 "
        "AND (COALESCE(type_metadata, '') = '' OR (json_valid(type_metadata) AND json_type(type_metadata) = 'object' "
        "AND json_extract(type_metadata, '$.transcript_status') IS NULL))", types)
    print(f"schema_migrations: transcripts_724e queued {cur.rowcount} row(s) of {sorted(types)} for a transcript "
          f"attempt (fetched in the background, one at a time)", flush=True)


def _mig_writeup_lead_596():
    # #596: fill the projects.writeup_lead cache (the card face's write-up excerpt) once for every
    # card that has a write-up. From then on the cards service keeps it in step with every write-up
    # save. Derived data, recomputable at any time, so it isn't change-logged. Idempotent.
    from . import cards
    conn = get_conn()
    for r in conn.execute("SELECT id, writeup_slug FROM projects WHERE writeup_slug IS NOT NULL").fetchall():
        conn.execute("UPDATE projects SET writeup_lead = ? WHERE id = ?",
                     (cards.writeup_lead(r["writeup_slug"]), r["id"]))


def _mig_reextract_utf16_text_607():
    # #607 part 3: text files in UTF-16 / UTF-32 (what Windows writes for `gpresult /h`, `powercfg
    # /batteryreport`, `msinfo32`) were read as UTF-8 and stopped almost at once, so their extracted
    # text was a few characters (150 KB report -> 3 characters) and they were unsearchable. The readers
    # now detect the encoding; this re-extracts the rows already stored wrong, once. A row qualifies
    # when its text is far shorter than its file (under 1/8 of the bytes), the file itself reads as
    # UTF-16/32 (BOM, or the NUL heuristic) and its type has a text reader; the new text replaces the
    # old when that is NUL-ridden (the signature of UTF-16 read as UTF-8) or shorter. Note SQLite's
    # length() stops at the first NUL, which is why a 233 KB report reads as "3 characters" in SQL.
    # Idempotent (a second run finds nothing to fix).
    from . import object_types, storage
    from .object_types import _textstats
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, media_type, stored_filename, file_size, extracted_text FROM capture_events "
        "WHERE stored_filename IS NOT NULL AND file_size >= 64 "
        "AND length(COALESCE(extracted_text, '')) * 8 < file_size").fetchall()
    checked = fixed = 0
    for r in rows:
        spec = object_types.get_object_type(r["media_type"])
        if spec is None or spec.text_extract_fn is None:
            continue
        checked += 1
        try:
            path = storage.path_for(r["stored_filename"])
            codec, _label = _textstats.sniff_encoding(path)
        except OSError:  # silent-ok: file missing or unreadable: nothing to re-read, leave the row as it is
            continue
        if not codec.startswith(("utf-16", "utf-32")):
            continue
        new_text = (spec.text_extract_fn(dict(r)) or "").strip()
        old_text = r["extracted_text"] or ""
        # The old reader's output for a UTF-16 file is full of NUL characters (SQLite's length() stops at the
        # first one, which is how these rows look "short"); that, or simply being shorter, means replace.
        if "\x00" in old_text or len(new_text) > len(old_text):
            conn.execute("UPDATE capture_events SET extracted_text = ? WHERE slug = ?", (new_text, r["slug"]))
            fixed += 1
    print(f"reextract_utf16_text_607: {len(rows)} short-text rows, {checked} with a text reader, {fixed} re-extracted", flush=True)

_UPLOAD_FILE_FIELD = re.compile(r"^<file: (.*)>$", re.S)
OWNERSHIP_BACKFILL_WINDOW = 600  # seconds between an item row's timestamp and its request-log row


def _ownership_backfill(conn):
    """#604 step 1: fill the new ownership columns once from the audit log, for what a SIGNED-IN user
    made before ownership was recorded at write time. Only `actor = user:<name>` rows count, and only
    when that user still exists; everything else stays NULL (= admin-owned, the safe default).
      * items: the request-log row of a successful `POST /api/upload` (its logged `file` field, the
        filename) or `POST /api/content` (its `external_url`), matched to the not-yet-owned item
        with that filename / URL created within OWNERSHIP_BACKFILL_WINDOW before the request was
        logged (the nearest one);
      * cards: `create_card` change-log rows (the projects row they inserted), plus the card's own
        write-up item made in the same create;
      * hobbies: `hobby_create` rows that turned a tag INTO a hobby (hobby_settings.created_by_user_id);
      * blog entries: `blog_entry_create` rows (the blog_entries row they inserted).
    Never overwrites a value already set. Idempotent. Returns the counts per kind."""
    users_by_name = {r["username"].lower(): r["id"] for r in conn.execute("SELECT id, username FROM users")}

    def uid(actor):
        if not (actor or "").startswith("user:"):
            return None
        return users_by_name.get(actor[5:].lower())

    def body(text):
        try:
            data = json.loads(text) if text else {}
        except ValueError:  # silent-ok: an unreadable request body can't name a file; that row is skipped
            return {}
        return data if isinstance(data, dict) else {}

    counts = {"items": 0, "cards": 0, "card_writeups": 0, "hobbies": 0, "blog_entries": 0}
    for r in conn.execute("SELECT timestamp, actor, path, form_body FROM audit_log WHERE op IS NULL AND method = 'POST' "
                          "AND path IN ('/api/upload', '/api/content') AND status_code = 200 AND actor LIKE 'user:%' "
                          "ORDER BY id").fetchall():
        owner = uid(r["actor"])
        if owner is None:
            continue
        fields = body(r["form_body"])
        if r["path"] == "/api/upload":
            m = _UPLOAD_FILE_FIELD.match(str(fields.get("file") or ""))
            column, value = "filename", (m.group(1) if m else None)
        else:
            column, value = "external_url", fields.get("external_url")
        if not value:
            continue
        hit = conn.execute(
            f"SELECT slug FROM capture_events WHERE uploaded_by_user_id IS NULL AND {column} = ? "
            "AND timestamp BETWEEN ? AND ? ORDER BY ABS(timestamp - ?) LIMIT 1",
            (value, r["timestamp"] - OWNERSHIP_BACKFILL_WINDOW, r["timestamp"] + 1, r["timestamp"])).fetchone()
        if hit:
            conn.execute("UPDATE capture_events SET uploaded_by_user_id = ? WHERE slug = ?", (owner, hit["slug"]))
            counts["items"] += 1

    def changes_by_user(op):
        for r in conn.execute("SELECT actor, mutations FROM audit_log WHERE op = ? AND actor LIKE 'user:%' "
                              "AND mutations IS NOT NULL ORDER BY id", (op,)).fetchall():
            owner = uid(r["actor"])
            if owner is None:
                continue
            try:
                muts = json.loads(r["mutations"]) or []
            except ValueError:  # silent-ok: a change-log row with unreadable images can't name what it made
                continue
            yield owner, [m for m in muts if isinstance(m, dict)]

    for owner, muts in changes_by_user("create_card"):
        for m in muts:
            if m.get("table") == "projects" and m.get("before") is None and (m.get("key") or {}).get("id"):
                card_id = m["key"]["id"]
                cur = conn.execute("UPDATE projects SET created_by_user_id = ? WHERE id = ? AND created_by_user_id IS NULL",
                                   (owner, card_id))
                if cur.rowcount:
                    counts["cards"] += 1
                    card = conn.execute("SELECT writeup_slug, created_at FROM projects WHERE id = ?", (card_id,)).fetchone()
                    if card and card["writeup_slug"]:
                        counts["card_writeups"] += conn.execute(
                            "UPDATE capture_events SET uploaded_by_user_id = ? WHERE slug = ? AND uploaded_by_user_id IS NULL "
                            "AND ABS(timestamp - ?) < 60", (owner, card["writeup_slug"], card["created_at"])).rowcount
    for owner, muts in changes_by_user("hobby_create"):
        for m in muts:
            after, before = m.get("after") or {}, m.get("before")
            if m.get("table") != "blog_tags" or after.get("is_hobby") != 1:
                continue
            if before is not None and before.get("is_hobby") == 1:
                continue  # re-marked an existing hobby: not its creation
            tag_id = (m.get("key") or {}).get("id")
            if not tag_id or not conn.execute("SELECT 1 FROM blog_tags WHERE id = ? AND is_hobby = 1", (tag_id,)).fetchone():
                continue
            conn.execute("INSERT OR IGNORE INTO hobby_settings (hobby_tag_id) VALUES (?)", (tag_id,))
            counts["hobbies"] += conn.execute(
                "UPDATE hobby_settings SET created_by_user_id = ? WHERE hobby_tag_id = ? AND created_by_user_id IS NULL",
                (owner, tag_id)).rowcount
    for owner, muts in changes_by_user("blog_entry_create"):
        for m in muts:
            if m.get("table") == "blog_entries" and m.get("before") is None and (m.get("key") or {}).get("id"):
                counts["blog_entries"] += conn.execute(
                    "UPDATE blog_entries SET created_by_user_id = ? WHERE id = ? AND created_by_user_id IS NULL",
                    (owner, m["key"]["id"])).rowcount
    return counts


def _mig_ownership_backfill_604():
    # #604 step 1: who made what, from the audit log (see _ownership_backfill). One change-log row
    # carries the counts (no row images: derived data, recomputable from the log itself).
    from . import changes
    conn = get_conn()
    counts = _ownership_backfill(conn)
    insert_change_log(conn, "migration_ownership_backfill_604", changes.ACTOR_MIGRATION, [],
                      batch_id=changes.new_batch_id(), details={"counts": counts})
    print(f"schema_migrations: ownership_backfill_604 counts {counts}", flush=True)


class MigrationDeferred(Exception):
    """Raised by a migration that must NOT run yet (its precondition isn't met, e.g. the owner's account
    can't be told apart). run_pending_migrations rolls its transaction back (nothing written), prints the
    reason loudly, leaves it unrecorded (so it tries again at the next start) and carries on booting."""


def _request_log_items(conn):
    """{slug: request actor} for items matched to the request-log row of the successful `POST
    /api/upload` (logged `file` field = the filename) or `POST /api/content` (its `external_url`) that
    made them: the nearest item with that name / URL created up to OWNERSHIP_BACKFILL_WINDOW before the
    row was logged, each item matched once. The actor may be None (rows from before actors were logged:
    the single-owner, no-login era), "owner-ui", "token" (the install token), "user:<name>", ..."""
    def body(text):
        try:
            data = json.loads(text) if text else {}
        except ValueError:  # silent-ok: an unreadable request body can't name a file; that row is skipped
            return {}
        return data if isinstance(data, dict) else {}

    out = {}
    for r in conn.execute("SELECT timestamp, actor, path, form_body FROM audit_log WHERE op IS NULL AND method = 'POST' "
                          "AND path IN ('/api/upload', '/api/content') AND status_code = 200 ORDER BY id").fetchall():
        fields = body(r["form_body"])
        if r["path"] == "/api/upload":
            m = _UPLOAD_FILE_FIELD.match(str(fields.get("file") or ""))
            column, value = "filename", (m.group(1) if m else None)
        else:
            column, value = "external_url", fields.get("external_url")
        if not value:
            continue
        for hit in conn.execute(
                f"SELECT slug FROM capture_events WHERE {column} = ? AND timestamp BETWEEN ? AND ? "
                "ORDER BY ABS(timestamp - ?)",
                (value, r["timestamp"] - OWNERSHIP_BACKFILL_WINDOW, r["timestamp"] + 1, r["timestamp"])).fetchall():
            if hit["slug"] not in out:
                out[hit["slug"]] = r["actor"]
                break
    return out


def _owner_manual_label(conn):
    """The owner's manual-upload Source label as `source_manual_upload()` builds it, read on `conn`
    (install_config: owner_label, else owner_name, else "Owner") + " — manual upload"."""
    from . import install_config  # lazy: install_config imports this module
    vals = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM install_config WHERE key IN ('owner_label', 'owner_name')")}
    who = (vals.get("owner_label") or "").strip() or (vals.get("owner_name") or "").strip() or install_config.FALLBACK_OWNER
    return f"{who} — manual upload", (vals.get("owner_name") or "").strip()


def owner_account(conn=None):
    """#560: the owner's user account, for attributing the owner's own old uploads. Returns
    (user row, how) or raises MigrationDeferred saying why it can't be told:
      * install_config.owner_name set -> the ONE enabled admin whose display name or username is that
        name (any case); none or several -> refused (a name that matches nobody is ambiguous, never
        guessed);
      * owner_name unset -> the install's only enabled admin; none or several -> refused."""
    own = conn is None
    conn = conn or get_conn()
    try:
        _label, owner_name = _owner_manual_label(conn)
        admins = [dict(r) for r in conn.execute(
            "SELECT id, username, display_name FROM users WHERE role = 'admin' AND COALESCE(disabled, 0) = 0 "
            "ORDER BY id")]
    finally:
        if own:
            conn.close()
    if owner_name:
        want = owner_name.lower()
        hits = [a for a in admins if want in ((a["display_name"] or "").strip().lower(), a["username"].lower())]
        if len(hits) == 1:
            return hits[0], f"the admin named {owner_name!r} (install config owner_name)"
        if not hits:
            raise MigrationDeferred(
                f"the install's owner is {owner_name!r} (Admin > Install, owner name), but no enabled admin has "
                f"that display name or username ({len(admins)} admin(s): "
                f"{', '.join(a['username'] for a in admins) or 'none'}). Set the owner name to the owner's "
                "display name, or give the owner's account that display name, then restart the web app.")
        raise MigrationDeferred(
            f"{len(hits)} enabled admins match the owner name {owner_name!r} "
            f"({', '.join(a['username'] for a in hits)}); give them different display names, then restart.")
    if len(admins) == 1:
        return admins[0], "the install's only admin (no owner name is set)"
    raise MigrationDeferred(
        f"no owner name is set (Admin > Install) and the install has {len(admins)} enabled admins, so the "
        "owner's account can't be told; set the owner name, then restart the web app.")


def _mig_capture_channel_560():
    # #560: capture_events.channel (how an item arrived) for every row made before it was recorded at
    # write time. Fill-only (channel IS NULL), idempotent, one change-log row with the count per rule.
    # Rules, first that applies wins (see CLAUDE.md "Who and how"):
    #   1. a Source label "Claude — migrated from <X>"            -> migration:<X>
    #   2. its upload_tags / automatch_tags change-log row (made with the item): that row's actor
    #      (mcp -> mcp; script -> script; the web's actors -> web)
    #   3. matched to a successful POST /api/upload or /api/content request-log row -> web
    #   4. a card's write-up: the actor of the create_card row that made the card -> its channel
    #   5. the label alone: the owner's "— manual upload" (only the web routes write it) -> web,
    #      "— automated upload" -> script, "Claude — authored" / "Claude" (the MCP's default) -> mcp
    #   6. otherwise -> unknown (said so, never guessed)
    from . import changes
    conn = get_conn()
    rows = conn.execute("SELECT slug, tech, timestamp FROM capture_events WHERE channel IS NULL").fetchall()
    counts = {"rows": len(rows), "migration_label": 0, "change_log": 0, "request_log": 0, "writeup": 0,
              "label": 0, "unknown": 0}
    if not rows:
        insert_change_log(conn, "migration_capture_channel_560", changes.ACTOR_MIGRATION, [],
                          batch_id=changes.new_batch_id(), details={"counts": counts})
        print(f"schema_migrations: capture_channel_560 nothing to fill {counts}", flush=True)
        return
    when = {r["slug"]: r["timestamp"] for r in rows}
    by_log = {}
    for r in conn.execute("SELECT actor, affected_slugs, timestamp FROM audit_log WHERE op IN "
                          "('upload_tags', 'automatch_tags') ORDER BY id").fetchall():
        try:
            slugs = json.loads(r["affected_slugs"] or "[]")
        except ValueError:  # silent-ok: an unreadable slug list names no item; the row is skipped
            continue
        for s in slugs if isinstance(slugs, list) else []:
            if s in when and s not in by_log and abs((r["timestamp"] or 0) - when[s]) <= 120 and r["actor"]:
                by_log[s] = actor_ctx.channel_for_actor(r["actor"])
    by_request = _request_log_items(conn)
    by_writeup = {}
    for r in conn.execute("SELECT actor, mutations FROM audit_log WHERE op = 'create_card' AND mutations IS NOT NULL "
                          "ORDER BY id").fetchall():
        try:
            muts = json.loads(r["mutations"]) or []
        except ValueError:  # silent-ok: unreadable images can't name the card; its write-up falls to the label rules
            continue
        for m in muts if isinstance(muts, list) else []:
            if isinstance(m, dict) and m.get("table") == "projects" and m.get("before") is None:
                card = conn.execute("SELECT writeup_slug FROM projects WHERE id = ?",
                                    ((m.get("key") or {}).get("id"),)).fetchone()
                if card and card["writeup_slug"] and r["actor"]:
                    by_writeup.setdefault(card["writeup_slug"], actor_ctx.channel_for_actor(r["actor"]))
    from . import install_config
    groups = source_groups()
    manual = {f"{g} — manual upload" for g in groups if g != SOURCE_CLAUDE} | {f"{install_config.FALLBACK_OWNER} — manual upload"}
    automated = {f"{g} — automated upload" for g in groups if g != SOURCE_CLAUDE}
    for r in rows:
        slug, tech = r["slug"], (r["tech"] or "").strip()
        src = migration_source(tech)
        if src:
            channel, rule = f"migration:{src}", "migration_label"
        elif slug in by_log:
            channel, rule = by_log[slug], "change_log"
        elif slug in by_request:
            channel, rule = actor_ctx.CHANNEL_WEB, "request_log"
        elif slug in by_writeup:
            channel, rule = by_writeup[slug], "writeup"
        elif tech in manual:
            channel, rule = actor_ctx.CHANNEL_WEB, "label"
        elif tech in automated:
            channel, rule = actor_ctx.CHANNEL_SCRIPT, "label"
        elif tech in (SOURCE_AUTHORED, SOURCE_CLAUDE):
            channel, rule = actor_ctx.CHANNEL_MCP, "label"
        else:
            channel, rule = "unknown", "unknown"
        counts[rule] += 1
        conn.execute("UPDATE capture_events SET channel = ? WHERE slug = ? AND channel IS NULL", (channel, slug))
    per_channel = {c: n for c, n in conn.execute(
        "SELECT channel, COUNT(*) FROM capture_events GROUP BY channel ORDER BY 2 DESC").fetchall()}
    insert_change_log(conn, "migration_capture_channel_560", changes.ACTOR_MIGRATION, [],
                      batch_id=changes.new_batch_id(), details={"counts": counts, "per_channel": per_channel})
    print(f"schema_migrations: capture_channel_560 filled {len(rows)} row(s) by rule {counts}; "
          f"now per channel {per_channel}", flush=True)


def _owner_upload_backfill(conn):
    """#560 (owner decision 2026-10-25, answer (a)): the owner's own old uploads become the owner's.
    An item counts when ALL hold: no uploader recorded yet (fill-only), its Source label is exactly the
    owner's manual-upload label (`source_manual_upload()`, e.g. "Hooptie J (me) — manual upload"), it
    arrived through the web (channel 'web', from _mig_capture_channel_560), and its upload request (when
    the request log has it) was not made by ANOTHER signed-in user. Uploads made with the install token
    under that label count too (owner, 2026-10-30: "Give them to me too"; they were the owner's scripts),
    counted in `of_which_install_token`. Returns (counts, assigned slugs); raises MigrationDeferred when
    there is something to assign but the owner's account can't be told (owner_account)."""
    label, _owner_name = _owner_manual_label(conn)
    rows = conn.execute("SELECT slug, channel FROM capture_events WHERE uploaded_by_user_id IS NULL AND tech = ?",
                        (label,)).fetchall()
    counts = {"label": label, "candidates": len(rows), "assigned": 0, "of_which_install_token": 0,
              "left_other_user": 0, "left_not_web": 0, "owner": None}
    if not rows:
        return counts, []
    owner, how = owner_account(conn)
    counts["owner"] = {"id": owner["id"], "username": owner["username"], "how": how}
    by_request = _request_log_items(conn)
    picked = []
    for r in rows:
        req = by_request.get(r["slug"])
        if r["channel"] != actor_ctx.CHANNEL_WEB:
            counts["left_not_web"] += 1
        elif isinstance(req, str) and req.startswith("user:") and req[5:].lower() != owner["username"].lower():
            counts["left_other_user"] += 1
        else:
            picked.append(r["slug"])
            if req == actor_ctx.ACTOR_TOKEN:
                counts["of_which_install_token"] += 1
    for i in range(0, len(picked), 500):
        part = picked[i:i + 500]
        conn.execute(f"UPDATE capture_events SET uploaded_by_user_id = ? WHERE uploaded_by_user_id IS NULL "
                     f"AND slug IN ({', '.join('?' * len(part))})", [owner["id"], *part])
    counts["assigned"] = len(picked)
    return counts, picked


def _mig_owner_upload_backfill_560():
    # #560: see _owner_upload_backfill. One change-log row with the counts and the slugs it set (no row
    # images: the slugs are the way back, `UPDATE ... SET uploaded_by_user_id = NULL WHERE slug IN (...)`).
    # Idempotent: fill-only, a second run finds nothing. Deferred (not recorded, retried every start)
    # while the owner's account is ambiguous.
    from . import changes
    conn = get_conn()
    counts, slugs = _owner_upload_backfill(conn)
    insert_change_log(conn, "migration_owner_uploads_560", changes.ACTOR_MIGRATION, [],
                      batch_id=changes.new_batch_id(), details={"counts": counts, "slugs": slugs})
    print(f"schema_migrations: owner_upload_backfill_560 counts {counts}", flush=True)


# #760: the one-time v1 -> v2 card migrations (v2c_1_kind_status, v2c_2_hobbies,
# v2c_5_referenced_provenance; core/card_migration.py) were removed: both installs are V2 and every
# new card is created with a stage, so they had nothing left to do. Their schema_migrations rows stay
# on existing DBs (harmless); a fresh install never runs them. (v2c_3 / v2c_4 went in #562.)
def _mig_date_guess_backfill_725():
    # #725: undated items (no real date: the upload time and a post-install file mtime don't count)
    # get the high-confidence date guesses (core/date_guess.py: a date in the file name, the file's
    # own embedded date such as a PDF CreationDate or a container date, a stored YouTube publish
    # date) as content_date, with the source recorded, as ONE undoable batch (core/items.py
    # apply_dates, op item_date_guess, actor migration). No network: nothing is fetched. Idempotent:
    # fill-only, so a second run finds those items dated and skips them. The rest stay undated:
    # they inherit their card's range, or the Curator shows them as needs_date.
    from . import actor as actor_ctx, item_dates, items
    with actor_ctx.acting_as(actor_ctx.ACTOR_MIGRATION):
        plan = item_dates.auto_plan(read_files=True)
        by_source = {}
        for _row, g in plan:
            by_source[g.source] = by_source.get(g.source, 0) + 1
        if plan:
            res = items.apply_dates([(r["slug"], g.epoch, item_dates.source_record(g)) for r, g in plan])
            applied, batch = len(res.data["applied"]), res.batch_id
        else:
            applied, batch = 0, None
    print(f"schema_migrations: date_guess_backfill_725 dated {applied} undated item(s) from in-file evidence "
          f"{by_source} (batch {batch}; undo it with POST /api/changes/<batch>/undo)", flush=True)


def _mig_search_index_543():
    # #543: fills the FTS5 search index for every item already in the DB (new/changed items are
    # kept current by the triggers from here on). Idempotent: 'rebuild' discards and re-creates the
    # index, so a second run changes nothing. Reports the item count and the time it took.
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_index_543 indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_short_title_seed_542():
    # #542 (owner decision 2026-10-18): the long caption is no longer the title. Every item whose
    # content_description already reads as a name (one line; at most item_title.SHORT_TITLE_MAX
    # characters for a content's own title, item_title.CAPTION_AS_TITLE_MAX for an accepted caption)
    # gets it as its short_title, so its name doesn't change. A longer accepted caption is left for
    # the short-title proposals (captions.propose_short_titles). ONE imaged change-log row as actor
    # `migration` (the generic undo refuses migration rows; the pre-deploy snapshot is the way back).
    # Idempotent: fill-only, a second run finds nothing.
    from . import actor as actor_ctx, items
    with actor_ctx.acting_as(actor_ctx.ACTOR_MIGRATION):
        res = items.seed_short_titles()
    print(f"schema_migrations: short_title_seed_542 gave {len(res.data['seeded'])} item(s) a short title from "
          f"their own title-shaped description; {res.data['left_long']} long caption(s) left for proposals "
          f"(batch {res.batch_id})", flush=True)


def _mig_tags_one_store_555():
    # #555 (owner decisions 2026-10-18): one tag store, the tree. Every free-text chip an item carries
    # that its tree tags don't is merged into the tree (core/tags.py merge_free_text_into_tree: the
    # rule is there), as ONE imaged change-log row, actor `migration`, counts in `details`. Then the
    # chip-cache triggers go on and every row's `tags` is rewritten from the tree. Idempotent: a
    # second run finds the column equal to the tree and merges nothing.
    from . import actor as actor_ctx, tags
    with actor_ctx.acting_as(actor_ctx.ACTOR_MIGRATION):
        res = tags.merge_free_text_into_tree()
    conn = get_conn()
    _ensure_chip_triggers(conn, force=True)
    rewritten = _refresh_chip_cache(conn)
    d = res.data
    print(f"schema_migrations: tags_one_store_555 examined {d['items']} item(s), {d['disagreeing']} disagreed; "
          f"{d['links_added']} tag link(s) added to {d['items_changed']} item(s), {d['tags_created']} tag(s) created, "
          f"{d['skipped_card_names']} card-name chip(s) left to the card; {rewritten} chip cache row(s) rewritten "
          f"(batch {res.batch_id})", flush=True)


def _mig_search_tags_tree_555():
    # #555: the search view's tags column reads the tree only (the free-text column is a cache now).
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_tags_tree_555 re-indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_search_filename_parts_762():
    # #762: the search view's filename column now also holds the name split at its CamelCase and
    # letter/digit boundaries ("ElCamino.jpeg" -> "El Camino jpeg"); re-read every item once.
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_filename_parts_762 re-indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_search_short_title_542():
    # #542: the search view's name column now carries short_title too; re-read every item once.
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_short_title_542 re-indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_archive_stats_605():
    # #605: archive items stored no member counts unless they were tars (#485): zip and 7z returned {}
    # from the type's embedded_metadata_fn, which is why the work install's Venza manual showed an
    # empty type_metadata. Fill the stats (core/object_types/archive.py archive_stats: counts, sizes,
    # by_type, max_depth) once for every archive row that has none, fill-only (the same merge the
    # upload path uses; nothing is overwritten). Read through a parser child (#644), so a hostile
    # file costs one bounded child. A file that is gone or can't be read is counted and named in the
    # log, never skipped silently. Idempotent: a second run finds every row filled (or unreadable).
    from . import parse_limits, paths
    from .object_types import archive
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, stored_filename, type_metadata FROM capture_events WHERE media_type = 'archive' "
        "AND stored_filename IS NOT NULL AND stored_filename != '' AND COALESCE(redacted, 0) = 0").fetchall()
    filled, missing, failed = 0, [], []
    for r in rows:
        try:
            meta = json.loads(r["type_metadata"] or "{}") or {}
        except (TypeError, ValueError):
            meta = {}  # silent-ok: a garbled column is treated as empty; the merge below keeps nothing it can't read
        if archive.STATS_KEY in meta:
            continue
        path = paths.storage_dir() / r["stored_filename"]
        if not path.is_file():
            missing.append(r["slug"])
            continue
        try:
            stats = parse_limits.call(archive.archive_stats, path, kind=parse_limits.BACKGROUND,
                                      what="archive stats", key=r["stored_filename"])
        except Exception as e:
            failed.append(f"{r['slug']} ({e})")
            continue
        if stats:
            _update_content_metadata(r["slug"], type_metadata={archive.STATS_KEY: stats})
            filled += 1
    print(f"schema_migrations: archive_stats_605 filled {filled} archive item(s); "
          f"{len(missing)} with no stored file{(': ' + ', '.join(missing[:20])) if missing else ''}; "
          f"{len(failed)} unreadable{(': ' + '; '.join(failed[:20])) if failed else ''}", flush=True)


MIGRATIONS = [
    ("drop_legacy_uploads", _mig_drop_legacy_uploads),
    ("curator_snooze_519", _mig_curator_snooze_519),
    ("audit_scrub_settings_559", _mig_audit_scrub_559),
    ("provenance_options_seed_529", _mig_provenance_options_seed_529),
    ("install_config_seed_562", _mig_install_config_seed_562),
    ("hobby_physical_piece_562", _mig_hobby_physical_piece_562),
    ("empty_file_ocr_done_587", _mig_empty_file_ocr_done_587),
    ("writeup_lead_596", _mig_writeup_lead_596),
    ("reextract_utf16_text_607", _mig_reextract_utf16_text_607),

    ("ownership_backfill_604", _mig_ownership_backfill_604),
    ("audit_scrub_secret_routes_559", _mig_audit_scrub_secret_routes_559),
    ("search_index_543", _mig_search_index_543),
    ("ocr_unsupported_724", _mig_ocr_unsupported_724),
    ("reread_legacy_office_724b", _mig_reread_legacy_office_724b),
    ("text_sources_724c", _mig_text_sources_724c),
    ("link_page_text_724d", _mig_link_page_text_724d),
    ("transcripts_724e", _mig_transcripts_724e),
    ("date_guess_backfill_725", _mig_date_guess_backfill_725),
    ("short_title_seed_542", _mig_short_title_seed_542),
    ("search_short_title_542", _mig_search_short_title_542),
    (MIGRATION_555, _mig_tags_one_store_555),
    ("search_tags_tree_555", _mig_search_tags_tree_555),
    ("search_filename_parts_762", _mig_search_filename_parts_762),
    ("reread_xls_790", _mig_reread_xls_790),
    ("caption_pdf_ai_556", _mig_caption_pdf_ai_556),
    ("vocab_seed_556", _mig_vocab_seed_556),
    ("capture_channel_560", _mig_capture_channel_560),
    ("owner_upload_backfill_560", _mig_owner_upload_backfill_560),  # after the channel fill: it reads it
    ("archive_stats_605", _mig_archive_stats_605),
]


def applied_migrations():
    conn = get_conn()
    try:
        return {r["name"]: r["applied_at"] for r in conn.execute("SELECT name, applied_at FROM schema_migrations")}
    finally:
        conn.close()


def pending_migrations():
    done = applied_migrations()
    return [name for name, _fn in MIGRATIONS if name not in done]


def run_pending_migrations():
    """Runs every migration whose schema_migrations row is absent, in order, recording each.
    Returns the list of names applied by THIS call."""
    ran = []
    for name, fn in MIGRATIONS:
        if name in applied_migrations():  # cheap pre-check outside the lock
            continue
        try:
            with transaction() as tx:
                if tx.conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)).fetchone():
                    continue  # another process applied it while we waited for the lock
                t0 = time.monotonic()
                fn()
                tx.conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)", (name, time.time()))
        except MigrationDeferred as exc:
            # #560: refused on purpose; the transaction rolled back, so nothing it did is kept. Loud, not fatal.
            msg = (f"schema_migrations: DEFERRED {name}: nothing was changed, because {exc} "
                   "It stays pending and runs again at the next start.")
            print(msg, flush=True)
            _log.warning(msg)
            continue
        ran.append(name)
        print(f"schema_migrations: applied {name} ({time.monotonic() - t0:.2f}s)", flush=True)
    return ran


# --- #704: drop the dead imagerepo-era columns and tables, with a safeguard ---------------------
# capture_events.source / client / ticket_id / artifact_link / icon and the clients / client_domains
# tables rode along from the imagerepo fork; nothing reads them any more (link vs file comes from
# the type's is_file, a hotlink is always /f/<slug>). Owner decision 2026-10-17: drop them. But an
# install could hold real data in one of them, so this never drops blind: each column / table is
# counted first and KEPT, with a loud warning, when it holds anything real. Runs at every web boot
# (init_db, migrate=True; never the MCP): a kept column is re-checked and dropped once its data is
# cleared, and the warning repeats until then. Idempotent: once everything is gone it only reads the
# schema. The first run is recorded in schema_migrations (MIGRATION_704); a run that drops or keeps
# anything writes one change-log row (op `migration_drop_dead_columns_704`) with the counts.
# The pre-deploy ZFS snapshot (scripts/deploy.sh) is the backup. SQLite >= 3.35 has DROP COLUMN;
# the app already refuses to start below 3.43 (the FTS5 search index, #543), so no rebuild fallback.
MIGRATION_704 = "drop_dead_columns_704"
DEAD_COLUMNS_704 = ("source", "client", "ticket_id", "artifact_link", "icon")
DEAD_TABLES_704 = ("clients", "client_domains")
# `source` held how a row was captured ('screenshot' by the dead uploader app / a file, 'external' for
# a link); those two values carry nothing `is_file` doesn't. Anything else counts as real data.
DEAD_SOURCE_VALUES_704 = ("screenshot", "external")
# The rows the old ensure_special_clients() seeded (category 'special', no nickname): not real data.
PLACEHOLDER_CLIENTS_704 = ("Unknown", "Not Business", "Internal Infrastructure")
_ABSOLUTE_F_LINK_704 = re.compile(r"^https?://[^/\s]+(/f/[^/?#\s]+)$")

# How to find a column's real values, for the warning (and the admin who reads it).
_DEAD_DATA_QUERY_704 = {
    "source": "SELECT slug, source FROM capture_events WHERE source IS NOT NULL AND source NOT IN ('', %s)"
              % ", ".join(f"'{v}'" for v in DEAD_SOURCE_VALUES_704),
    "client": "SELECT slug, client FROM capture_events WHERE client IS NOT NULL AND client != ''",
    "ticket_id": "SELECT slug, ticket_id FROM capture_events WHERE ticket_id IS NOT NULL AND ticket_id != ''",
    "icon": "SELECT slug, icon FROM capture_events WHERE icon IS NOT NULL AND icon != ''",
    "artifact_link": "SELECT slug, artifact_link FROM capture_events WHERE artifact_link IS NOT NULL "
                     "AND artifact_link != '' AND artifact_link != '/f/' || slug",
    "clients": "SELECT * FROM clients WHERE NOT (category = 'special' AND name IN (%s) AND COALESCE(nickname, '') = '')"
               % ", ".join(f"'{v}'" for v in PLACEHOLDER_CLIENTS_704),
    "client_domains": "SELECT * FROM client_domains",
}


def _artifact_link_is_derived(slug, link):
    """True when `link` is exactly the row's own hotlink, /f/<slug>, relative or absolute."""
    if link == f"/f/{slug}":
        return True
    m = _ABSOLUTE_F_LINK_704.match(link or "")
    return bool(m) and m.group(1) == f"/f/{slug}"


def dead_data_report_704(conn=None):
    """Read-only: {name: {"kind": "column"|"table", "present": bool, "real": n, "rows": n}} for each
    #704 column (capture_events) and table. `real` = rows holding real data by the rules above
    (0 for an absent one); `rows` = rows with any value at all (columns) / all rows (tables)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(capture_events)")}
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out = {}
        for col in DEAD_COLUMNS_704:
            info = {"kind": "column", "present": col in cols, "real": 0, "rows": 0}
            if info["present"]:
                info["rows"] = conn.execute(
                    f"SELECT COUNT(*) FROM capture_events WHERE {col} IS NOT NULL AND {col} != ''").fetchone()[0]
                if col == "artifact_link":
                    info["real"] = sum(1 for r in conn.execute(_DEAD_DATA_QUERY_704[col])
                                       if not _artifact_link_is_derived(r["slug"], r["artifact_link"]))
                else:
                    info["real"] = conn.execute(f"SELECT COUNT(*) FROM ({_DEAD_DATA_QUERY_704[col]})").fetchone()[0]
            out[col] = info
        for table in DEAD_TABLES_704:
            info = {"kind": "table", "present": table in tables, "real": 0, "rows": 0}
            if info["present"]:
                info["rows"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                query = _DEAD_DATA_QUERY_704[table]
                if table == "clients" and "nickname" not in {r["name"] for r in conn.execute("PRAGMA table_info(clients)")}:
                    query = query.replace("COALESCE(nickname, '')", "''")  # a clients table from before nickname
                info["real"] = conn.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0]
            out[table] = info
        return out
    finally:
        if own:
            conn.close()


def _drop_capture_columns(conn, columns):
    """ALTER TABLE capture_events DROP COLUMN for each of `columns`, on the caller's transaction.
    DROP COLUMN refuses an indexed column, and re-checks every view and trigger in the schema, so the
    indexes on these columns go first and the search index's view + triggers (#543) are dropped and
    re-created around it (they don't read these columns; this keeps them valid whatever SQLite checks).
    The FTS table itself is untouched, so the index needs no rebuild."""
    for idx in [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'capture_events' AND sql IS NOT NULL")]:
        idx_cols = {r["name"] for r in conn.execute(f"PRAGMA index_info({idx})")}
        if idx_cols & set(columns):
            conn.execute(f"DROP INDEX IF EXISTS {idx}")
    for name in list(_search_sync_triggers()) + list(_chip_triggers()):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DROP VIEW IF EXISTS item_search_src")
    for col in columns:
        conn.execute(f"ALTER TABLE capture_events DROP COLUMN {col}")
    _ensure_search_schema(conn)
    _ensure_chip_triggers(conn)


def _drop_dead_columns_704():
    """The #704 step (see the comment above MIGRATION_704). Returns {"dropped": [...], "kept":
    {name: real_rows}, "report": dead_data_report_704 before the drop, "first_run": bool}."""
    from . import changes
    with transaction() as tx:
        conn = tx.conn
        report = dead_data_report_704(conn)
        first_run = conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION_704,)).fetchone() is None
        drop_cols = [c for c in DEAD_COLUMNS_704 if report[c]["present"] and not report[c]["real"]]
        drop_tables = [t for t in DEAD_TABLES_704 if report[t]["present"] and not report[t]["real"]]
        kept = {n: i["real"] for n, i in report.items() if i["present"] and i["real"]}
        if drop_cols:
            _drop_capture_columns(conn, drop_cols)
        for table in drop_tables:
            conn.execute(f"DROP TABLE {table}")
        dropped = drop_cols + drop_tables
        if dropped or (kept and first_run):
            insert_change_log(conn, "migration_drop_dead_columns_704", changes.ACTOR_MIGRATION, [],
                              batch_id=changes.new_batch_id(),
                              details={"dropped": dropped, "kept": kept,
                                       "counts": {n: {"rows": i["rows"], "real": i["real"]}
                                                  for n, i in report.items() if i["present"]}})
        if first_run:
            conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)", (MIGRATION_704, time.time()))
    if dropped:
        counts = ", ".join("%s=%d" % (n, report[n]["rows"]) for n in dropped)
        print(f"schema_migrations: {MIGRATION_704} dropped {', '.join(dropped)} (rows with a value: {counts})",
              flush=True)
    for name, n in kept.items():
        where = "capture_events." + name if report[name]["kind"] == "column" else "the table " + name
        print(f"WARNING {MIGRATION_704}: KEPT {where}: {n} row(s) hold real data, so it was NOT dropped. "
              f"Nothing in the app reads or writes it any more. Look at them with: {_DEAD_DATA_QUERY_704[name]}; "
              "save what matters elsewhere, clear them, and restart the web app: this check runs at every boot "
              "and drops it once it is empty.", flush=True)
    return {"dropped": dropped, "kept": kept, "report": report, "first_run": first_run}


def _run_drop_dead_columns_704():
    """init_db's call: the app must start even if the #704 step fails. A failure rolls the whole step
    back (one transaction: nothing is dropped) and is logged loudly with its traceback."""
    try:
        return _drop_dead_columns_704()
    except Exception as e:
        import traceback
        print(f"ERROR {MIGRATION_704}: the step failed and was rolled back (nothing dropped): {e!r}. "
              "The app carries on with the old columns in place; they are no longer read.", flush=True)
        traceback.print_exc()
        return None


# --- #760: drop the retired projects.status column ("Status quo vadis") -------------------------
# #545 made the stage the only status; after it nothing read projects.status (the frozen v1 word,
# which disagreed with the live stage on many cards). Owner decision on #760: drop it. Unlike #704
# there is no "real data" safeguard: the word is retired by decision, not merely unused, so every
# value counts as dead. The same shape as #704 otherwise: runs at every web boot (init_db,
# migrate=True; never the MCP), counts first, and when it drops writes ONE change-log row (op
# `migration_drop_project_status_760`, actor migration, the counts per word in `details`). Once the
# column is gone it only reads the schema. The pre-deploy ZFS snapshot (scripts/deploy.sh) is the
# backup. Row images logged before the drop still name `status`: db.invert_image restores only the
# columns the table has now (#704), so those undos keep working.
MIGRATION_760 = "drop_project_status_760"


def _drop_project_status_760():
    """Returns {"dropped": bool, "rows": n, "by_word": {word: n}, "first_run": bool}."""
    from . import changes
    with transaction() as tx:
        conn = tx.conn
        first_run = conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION_760,)).fetchone() is None
        present = "status" in {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        rows, by_word = 0, {}
        if present:
            by_word = {(r["status"] if r["status"] is not None else "(null)"): r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM projects GROUP BY status ORDER BY status")}
            rows = sum(by_word.values())
            # Same care as _drop_capture_columns: no index may cover the column, and the search /
            # chip views and triggers are dropped and re-created around the ALTER so whatever SQLite
            # re-checks stays valid (none of them reads projects.status).
            for idx in [r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'projects' AND sql IS NOT NULL")]:
                if "status" in {r["name"] for r in conn.execute(f"PRAGMA index_info({idx})")}:
                    conn.execute(f"DROP INDEX IF EXISTS {idx}")
            for name in list(_search_sync_triggers()) + list(_chip_triggers()):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute("DROP VIEW IF EXISTS item_search_src")
            conn.execute("ALTER TABLE projects DROP COLUMN status")
            _ensure_search_schema(conn)
            _ensure_chip_triggers(conn)
            insert_change_log(conn, "migration_drop_project_status_760", changes.ACTOR_MIGRATION, [],
                              batch_id=changes.new_batch_id(),
                              details={"dropped": ["projects.status"], "rows": rows, "by_word": by_word})
        if first_run:
            conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)", (MIGRATION_760, time.time()))
    if present:
        print(f"schema_migrations: {MIGRATION_760} dropped projects.status ({rows} card(s) held a frozen v1 word: "
              f"{by_word}; nothing read it since #545)", flush=True)
    return {"dropped": present, "rows": rows, "by_word": by_word, "first_run": first_run}


def _run_drop_project_status_760():
    """init_db's call: the app must start even if the #760 step fails. A failure rolls the whole step
    back (one transaction: nothing is dropped) and is logged loudly with its traceback."""
    try:
        return _drop_project_status_760()
    except Exception as e:
        import traceback
        print(f"ERROR {MIGRATION_760}: the step failed and was rolled back (projects.status NOT dropped): {e!r}. "
              "The app carries on with the old column in place; nothing reads it, and new cards take its "
              "default. The step retries at the next web boot.", flush=True)
        traceback.print_exc()
        return None


def _rebuild_project_relations_typed(conn):
    """V2 cards 3.8: project_relations gets a `type` (and `note`) and its PK becomes
    (slug_a, slug_b, type). SQLite can't change a PK in place, so the table is
    rebuilt in one transaction (DDL is transactional in SQLite, so a failure
    leaves the old table untouched). Idempotent: skipped once the table has a
    `type` column. Every existing row becomes 'related'; v1 stored related links
    in both directions and still does, so the row count and
    list_related_projects are unchanged. Logged as an actor='migration' row.
    #667: init_db calls it inside its own schema transaction (a shared transaction() connection),
    where the commit / BEGIN IMMEDIATE / rollback below are no-ops and the outer write lock already
    makes the check and the rebuild one step; they still do the job on a plain connection."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_relations)")}
    if "type" in cols:
        return False
    conn.commit()  # nothing open here, but BEGIN IMMEDIATE must not nest
    try:
        conn.execute("BEGIN IMMEDIATE")
        if "type" in {r["name"] for r in conn.execute("PRAGMA table_info(project_relations)")}:
            conn.rollback()  # #549: a concurrent boot (web vs MCP) rebuilt it while we waited
            return False
        before = conn.execute("SELECT COUNT(*) AS n FROM project_relations").fetchone()["n"]
        conn.execute("DROP TABLE IF EXISTS project_relations_new")
        conn.execute("""
            CREATE TABLE project_relations_new (
                slug_a TEXT NOT NULL, slug_b TEXT NOT NULL,
                type   TEXT NOT NULL DEFAULT 'related',
                note   TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                PRIMARY KEY (slug_a, slug_b, type)
            )""")
        conn.execute("INSERT INTO project_relations_new (slug_a, slug_b, type, created_at) "
                     "SELECT slug_a, slug_b, 'related', created_at FROM project_relations")
        after = conn.execute("SELECT COUNT(*) AS n FROM project_relations_new").fetchone()["n"]
        if after != before:
            raise RuntimeError(f"project_relations rebuild lost rows: {before} -> {after}")
        conn.execute("DROP TABLE project_relations")
        conn.execute("ALTER TABLE project_relations_new RENAME TO project_relations")
        insert_change_log(
            conn, "migration_v2c_4_links", "migration",
            [{"table": "project_relations", "key": {}, "before": {"rows": before, "typed": False},
              "after": {"rows": after, "type": "related"}}],
            batch_id=uuid.uuid4().hex[:16], affected_slugs=[])
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def _row_to_dict(row):
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    d["type_metadata"] = json.loads(d["type_metadata"]) if d.get("type_metadata") else {}
    return d


def insert_upload(slug, filename, stored_filename, uploaded_by, description="", tags=None,
                   file_size=None, source_modified_at=None, ocr_status=None,
                   media_type="image", external_url=None, content_description=None, content_date=None,
                   type_metadata=None, sensitive=False, short_title=None):
    """Creates a capture_events row. filename/stored_filename are for uploaded files and can be
    None for content that lives elsewhere (media_type='youtube' + external_url, for example) —
    there's no requirement that a row correspond to an actual file on disk.

    media_type/external_url/content_description/content_date describe the content itself,
    separate from description, which is about how/why the row was captured. See the
    capture_events column comments in SCHEMA for the distinction. type_metadata is a freeform
    dict for whatever per-type properties don't fit those generic columns — see
    core/object_types.py's MetadataField and set_type_metadata below.

    #604 step 1: `uploaded_by_user_id` is the signed-in user of the current actor context
    (users.user_id_for_actor), NULL for the token / MCP / scripts / system = admin-owned. This is
    the ONE place an item row is created, so every path (web upload and content, MCP upload,
    import and add_content, a card's write-up) records it the same way. `uploaded_by` above is
    the free-text Source label (`tech`), unrelated.
    #603: `sensitive=True` (the upload dialog's checkbox) sets the flag in the same INSERT, so the
    item is never visible unlocked, not even for a moment.
    #542: a content_description given at creation is the content's OWN title (a video's, a
    write-up's, a link's; never a caption, which arrives later as a suggestion), so it is also the
    short title unless `short_title` is given or the text isn't name-shaped (one line, at most
    item_title.SHORT_TITLE_MAX characters).
    """
    from . import actor as actor_ctx, item_title, users  # lazy: users imports this module
    if short_title is None and item_title.title_shaped(content_description, item_title.SHORT_TITLE_MAX):
        short_title = content_description.strip()
    who = actor_ctx.current_actor()
    owner_id = users.user_id_for_actor(who)
    # #560: HOW it arrived, from the context (never from the free-text Source label, except that a
    # label naming a migration source says where migrated content came from). #778: the personal MCP
    # key, only when the uploader is that key's user.
    channel = channel_for(uploaded_by, actor_ctx.current_channel())
    via_token = actor_ctx.token_id_for(who) if owner_id is not None else None
    conn = get_conn()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO capture_events (slug, timestamp, tech, description, "
            "extracted_text, tags, filename, stored_filename, file_size, source_modified_at, ocr_status, ocr_started_at, "
            "media_type, external_url, content_description, content_date, type_metadata, uploaded_by_user_id, "
            "sensitive, sensitive_by, sensitive_at, short_title, channel, uploaded_via_token_id) "
            "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, now, uploaded_by, description,
             "[]", filename, stored_filename, file_size, source_modified_at, ocr_status,
             now if ocr_status == "pending" else None,
             media_type, external_url, content_description, content_date, json.dumps(type_metadata or {}), owner_id,
             1 if sensitive else 0, who if sensitive else None, now if sensitive else None, short_title or None,
             channel, via_token),
        )
        conn.commit()
    finally:
        conn.close()
    # #555: the tags an upload names go into the tag tree (the one store) through the tag service,
    # imaged (op upload_tags); the row's `tags` column is the derived chip cache.
    if tags:
        from . import tags as tags_svc  # lazy: core.tags imports this module
        tags_svc.attach_on_upload(slug, tags)


def insert_content(slug, uploaded_by, media_type, external_url=None, content_description=None, content_date=None,
                    description="", tags=None, type_metadata=None, sensitive=False):
    """Thin wrapper around insert_upload for rows with no uploaded file — e.g. a YouTube video,
    where the content lives at external_url rather than in local storage. filename/stored_filename
    are left None. ocr_status starts "pending" whenever the type is OCR-capable (see
    core/object_types.py) — same convention as insert_upload's image path — so the background
    OCR pass (against the type's fetched/captured thumbnail, not a local file) picks it up the
    same way an uploaded screenshot does."""
    from . import object_types  # local import: object_types never needs db, so no cycle, but
    # keeping it out of the module-level imports keeps db.py's own dependency footprint (pure
    # stdlib + sqlite3) obvious at a glance.
    spec = object_types.get_object_type(media_type)
    insert_upload(
        slug, None, None, uploaded_by, description=description, tags=tags,
        media_type=media_type, external_url=external_url,
        content_description=content_description, content_date=content_date,
        ocr_status="pending" if spec.ocr_capable else None,
        type_metadata=type_metadata, sensitive=sensitive,
    )


def _set_type_metadata(slug, metadata):
    """Replaces a row's type_metadata dict wholesale — callers that want to
    merge should read get_by_slug(slug)["type_metadata"] first."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET type_metadata = ? WHERE slug = ?", (json.dumps(metadata or {}), slug))
        conn.commit()
    finally:
        conn.close()


def _update_content_metadata(slug, content_description=None, type_metadata=None, short_title=None):
    """Partial update for the two content-description-shaped fields that
    neither update_tags (description/tags — see
    api_update_image in web/app.py) nor rename_object (display_name,
    #11) cover: content_description itself, and type_metadata.

    Added for #54's full YouTube channel sync: correcting a row's
    site-scraped content_description (used as the video's title — see
    core/db.py's insert_content docstring and object_detail.html) with the
    real title from the YouTube Data API, while also recording
    view/like/comment counts (and, when it isn't the channel owner's own
    upload, the uploading channel's name) in type_metadata.

    Unlike set_type_metadata (wholesale replace — the caller is expected to
    read-modify-write itself if it wants to merge), this MERGES the given
    type_metadata into whatever is already stored, keying on top-level dict
    keys. A correction pass re-writing the same fields with the same values
    is what makes re-running #54's sync script a safe no-op; a merge (not a
    plain replace) also means this can be called by more than one future
    writer for the same row (e.g. a stats-only refresh later) without one
    call's fields clobbering another's.

    content_description=None leaves the existing value alone (same
    None-means-"don't touch" convention as update_tags/rename_object);
    passing "" clears it, same as those two.
    """
    existing = get_by_slug(slug)
    if existing is None:
        return None
    new_content_description = content_description if content_description is not None else existing["content_description"]
    new_short_title = short_title if short_title is not None else existing.get("short_title")  # #542: same rule
    if type_metadata:
        merged = dict(existing.get("type_metadata") or {})
        merged.update(type_metadata)
    else:
        merged = existing.get("type_metadata") or {}
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE capture_events SET content_description = ?, type_metadata = ?, short_title = ? WHERE slug = ?",
            (new_content_description, json.dumps(merged), new_short_title or None, slug),
        )
        conn.commit()
        return get_by_slug(slug)
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


def get_by_slug(slug):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM capture_events WHERE slug = ?", (slug,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def find_duplicate(filename, file_size, source_modified_at):
    """A prior capture_event is only treated as a duplicate when filename,
    file size, and the source file's own last-modified time all agree —
    matching just the name or just the size is too easy to collide on by
    coincidence (e.g. two unrelated 'screenshot.png' drops).
    """
    if not filename or file_size is None or source_modified_at is None:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM capture_events WHERE filename = ? AND file_size = ? AND source_modified_at = ? "
            "ORDER BY timestamp ASC LIMIT 1",
            (filename, file_size, source_modified_at),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _rename_object(slug, display_name):
    """Sets an object's display_name override (#11). Passing an empty string clears it back to
    the default title (core/item_title.py) rather than leaving the previous override in place,
    since "" is never a meaningful display name. (#704: the `icon` override is gone.)"""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET display_name = ? WHERE slug = ?", (display_name or None, slug))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _set_provenance(slug, provenance):
    """Sets or clears an object's provenance classification (#341).

    provenance: an ACTIVE key of the editable file list (#529), the row's current value
    (even if retired), or None to clear. Anything else raises
    CardError('bad_provenance') listing the active keys.
    Returns the updated row, or None if not found."""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    from . import provenance_options
    provenance = provenance_options.validate("file", provenance, current=existing.get("provenance"))
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET provenance = ? WHERE slug = ?", (provenance, slug))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _set_highlight(slug, on: bool):
    """Sets or clears an object's highlight flag (#341).

    on: True to mark as highlighted, False to clear.
    Returns the updated row, or None if not found."""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET highlight = ? WHERE slug = ?", (1 if on else 0, slug))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _set_brand_asset(slug, is_brand, brand_role=None):
    """Sets or clears an object's brand asset status and role (#350).

    is_brand: True to flag as brand asset, False to clear.
    brand_role: optional role label (one of BRAND_ROLES or custom). When is_brand
                is falsey, brand_role is also cleared.
    Returns the updated row, or None if not found."""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    try:
        if not is_brand:
            # Clear both flag and role when marking as not a brand asset
            conn.execute("UPDATE capture_events SET is_brand_asset = 0, brand_role = NULL WHERE slug = ?", (slug,))
        else:
            conn.execute(
                "UPDATE capture_events SET is_brand_asset = 1, brand_role = ? WHERE slug = ?",
                (brand_role or None, slug)
            )
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def list_brand_assets():
    """Returns all brand assets (where is_brand_asset=1), non-redacted,
    ordered by brand_role then recency (timestamp desc). Each row includes
    slug, brand_role (the title is core/item_title.py's, #542),
    and availability for thumb/file URLs."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE is_brand_asset = 1 AND redacted = 0 "
            "ORDER BY brand_role, timestamp DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# Tag names the Wallpaper surface (#422) gathers: a small set of synonyms rather than one
# canonical tag (the owner's "Desktop Picture" pile and the archive import's "wallpaper" tag mean
# the same thing). Matched case-insensitively. Editable in Admin since #556: the vocab_terms table
# (vocab 'wallpaper_tag', core/vocab.py; seeded with wallpaper + desktop picture).


def list_wallpapers():
    """Objects carrying any wallpaper tag (vocab_terms, vocab 'wallpaper_tag'), non-redacted,
    newest first (#422). The Wallpaper page's data source (and its zip, #780): a tag-based
    sibling of list_brand_assets (wallpaper is just a tag, not a flag)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ce.* FROM capture_events ce "
            "JOIN post_tags pt ON pt.post_slug = ce.slug "
            "JOIN blog_tags t ON t.id = pt.tag_id "
            "WHERE lower(t.name) IN (SELECT term FROM vocab_terms WHERE vocab = 'wallpaper_tag') "
            "AND ce.redacted = 0 ORDER BY ce.timestamp DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_setting(key):
    """Reads one app_settings value (#55) — a generic key/value store for
    secrets and other app-level settings the app needs to remember across
    restarts (starting with a YouTube Data API key, see core/object_types.py's
    sibling issue #54), so a new integration doesn't need a docker-compose
    env var wired in from outside the app. Returns None if `key` was never
    set. Callers that only need to know whether a value is present (e.g. the
    admin pane's masked status indicator) should call has_setting instead —
    the real value should only ever be read by the code that actually needs
    to use it (never logged, never handed back to a browser)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def has_setting(key):
    """True if `key` currently has a non-empty stored value. This is the
    presence-only check GET /api/settings and the admin pane's "Set" /
    "Not set" indicator use, so the real value never needs to round-trip
    back to the browser just to show whether one exists."""
    return bool(get_setting(key))


def set_setting(key, value):
    """Upserts one app_settings value. An empty/falsy `value` deletes the
    row instead of storing an empty string, so has_setting's presence check
    and "was this ever cleared" agree with each other. Plain-column storage
    (no encryption) — an accepted tradeoff for a single-owner, LAN-only,
    already-unauthenticated app (see web/app.py's module docstring); still
    never logged and never echoed back to a caller."""
    conn = get_conn()
    try:
        if value:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def list_uploaders(query=None):
    """Distinct Source *groups* (see source_group()) matching the given
    filters, each with their total count and most recent capture time — used
    to group the gallery fairly so one prolific uploader can't crowd others
    out of a single global LIMIT. Grouped in Python rather than SQL `GROUP BY
    tech` since Source values are now full sentences (e.g. "Hooptie J (me) —
    manual upload" vs "... — automated upload") and the gallery groups on the
    shorter "who" prefix, not the exact string — row counts here are small
    enough (single-owner site) that this is simpler than fighting SQL string
    slicing.
    """
    conn = get_conn()
    try:
        # #282: these totals sit next to /api/gallery's per-uploader item
        # lists, which come from search() and hide redacted rows -- count
        # the same set so "N items" matches what's actually listed.
        # #603: and the same browse clause search() applies, so a hidden item's description can't be
        # probed through these counts (a ?query= that only matches a sensitive item counts 0).
        # #543: the text filter is the SAME one search() uses (item_search), so a gallery's per-uploader
        # "total" and its listed items agree when a query is given.
        clauses, params = ["ce.redacted = 0" + policy.sql_browse_clause("ce.")], []
        if query:
            match = _search_match(query)
            clauses.append("ce.id IN (SELECT rowid FROM item_search WHERE item_search MATCH ?)" if match else "0")
            if match:
                params.append(match)
        where = f"WHERE {' AND '.join(clauses)}"
        rows = conn.execute(f"SELECT ce.tech AS tech, ce.timestamp AS timestamp FROM capture_events ce {where}",
                            params).fetchall()
        groups = {}
        for r in rows:
            key = source_group(r["tech"])
            g = groups.setdefault(key, {"uploaded_by": key, "total": 0, "most_recent": 0})
            g["total"] += 1
            g["most_recent"] = max(g["most_recent"], r["timestamp"])
        return sorted(groups.values(), key=lambda g: g["most_recent"], reverse=True)
    finally:
        conn.close()


def source_label_exists(label):
    """#560: True when any un-redacted item's Source label is `label` or starts with it as its group
    prefix (the same match as search(uploaded_by=)). Counts hidden items too: it only says the label
    is a real one, so its gallery explains an empty list instead of calling the label unknown."""
    if not label:
        return False
    conn = get_conn()
    try:
        return conn.execute("SELECT 1 FROM capture_events WHERE redacted = 0 AND (tech = ? OR tech LIKE ? OR tech LIKE ?) "
                            "LIMIT 1", (label, f"{label} —%", f"{label} -%")).fetchone() is not None
    finally:
        conn.close()


def list_restricted():
    """#443: every non-redacted row of a restricted type, newest first, for
    the admin pane's restricted list (and the MCP). #603: plus every row flagged
    "This is sensitive" (capture_events.sensitive), of any type."""
    from core import object_types  # lazy: type modules import db
    keys = object_types.restricted_types()
    type_clause = f"media_type IN ({', '.join('?' * len(keys))}) OR " if keys else ""
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM capture_events WHERE redacted = 0 AND ({type_clause}sensitive = 1) "
            "ORDER BY timestamp DESC",
            list(keys),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


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
    from . import object_types, text_sources
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


def _mark_redacted(slug):
    """File removed (sensitive content), metadata kept for future correlation.

    The caller (web/app.py's /api/image/{slug}/redact, the
    constructicon_redact MCP tool) deletes the stored file first; this also
    clears stored_filename to NULL -- the row's one authoritative "is there
    a real file right now" signal, since #282's unmark_redacted can flip
    `redacted` back to 0 without ever being able to restore the actual file.
    Every has_thumb/is_file-style check in web/app.py keys off
    stored_filename for exactly that reason, not off `redacted` (which was
    a sufficient proxy before un-redact existed, and stopped being one the
    moment a row could become un-redacted-but-still-fileless).

    filename (the original human-readable name) is deliberately left alone
    -- "metadata kept for future correlation" means that one, not the
    internal storage path, which has no display purpose once the file it
    pointed to is gone. Since #282 a redacted row is hidden from every
    list/browse/search query and reachable only by its direct /object/<slug>
    link or list_redacted()."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET redacted = 1, stored_filename = NULL WHERE slug = ?", (slug,))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _unmark_redacted(slug):
    """Reverse of mark_redacted (#282): clears the flag so the row rejoins
    ordinary browsing/search. Deliberately does NOT touch stored_filename --
    it's already NULL (mark_redacted cleared it) and stays that way, since
    the file itself was deleted and can't be restored. That's what keeps
    web/app.py's has_thumb/is_file checks correctly file-less after this
    call despite `redacted` now being 0 again."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET redacted = 0 WHERE slug = ?", (slug,))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def list_redacted():
    """Every currently-redacted row, most recently uploaded first (#282).
    The admin pane's "Redacted items" view -- once redacted rows are hidden
    from every other list this is the only place they can be found again
    without already knowing the slug. There's no redacted_at column, so
    this can't say *when* a row was redacted; the audit log has the web
    route's call if that matters."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE redacted = 1 ORDER BY timestamp DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _delete_upload(slug):
    """Full delete — removes the metadata row entirely. Caller is responsible
    for deleting the actual file(s) from storage first.

    Also cleans up every row elsewhere in the schema that references this
    slug — capture_event_relations (already handled here before #16),
    plus project_items and post_tags (#16 — flagged by #1's PR review as a
    pre-existing gap: deleting a slug left its curated-project membership
    and tag links behind as orphans, invisible but never cleaned up since
    nothing ever queries project_items/post_tags for a slug that no longer
    has a capture_events row)."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM capture_events WHERE slug = ?", (slug,))
        conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? OR slug_b = ?", (slug, slug))
        # #477: take the item out of its revision chain, closing the gap (A -> B -> C minus B = A -> C).
        _unlink_revision(conn, slug)
        conn.execute("DELETE FROM project_items WHERE post_slug = ?", (slug,))
        conn.execute("DELETE FROM post_tags WHERE post_slug = ?", (slug,))
        # #240: an open "which project?" question about a row that no longer
        # exists is just noise in the admin pane's queue.
        conn.execute("DELETE FROM pending_decisions WHERE post_slug = ?", (slug,))
        conn.commit()
    finally:
        conn.close()


# --- Item service reads + trash (#541 phase B; the writes go through ImageLog in core/items.py) ---

def item_references(slug):
    """Every row elsewhere that an item delete removes or re-links, as image keys:
    {relations: [(slug_a, slug_b)], successor, predecessor, project_items: [project_id],
    post_tags: [tag_id], blog_entry_items: [entry_id], decisions: [id]}. Same set
    delete_upload cleans, plus the blog-entry links it missed."""
    conn = get_conn()
    try:
        succ = conn.execute("SELECT new_slug FROM item_revisions WHERE old_slug = ?", (slug,)).fetchone()
        pred = conn.execute("SELECT old_slug FROM item_revisions WHERE new_slug = ?", (slug,)).fetchone()
        return {
            "relations": [(r["slug_a"], r["slug_b"]) for r in conn.execute(
                "SELECT slug_a, slug_b FROM capture_event_relations WHERE slug_a = ? OR slug_b = ? ORDER BY rowid",
                (slug, slug))],
            "successor": succ["new_slug"] if succ else None,
            "predecessor": pred["old_slug"] if pred else None,
            "project_items": [r["project_id"] for r in conn.execute(
                "SELECT project_id FROM project_items WHERE post_slug = ? ORDER BY rowid", (slug,))],
            "post_tags": [r["tag_id"] for r in conn.execute(
                "SELECT tag_id FROM post_tags WHERE post_slug = ? ORDER BY rowid", (slug,))],
            "blog_entry_items": [r["entry_id"] for r in conn.execute(
                "SELECT entry_id FROM blog_entry_items WHERE post_slug = ? ORDER BY rowid", (slug,))],
            "decisions": [r["id"] for r in conn.execute(
                "SELECT id FROM pending_decisions WHERE post_slug = ? ORDER BY id", (slug,))],
        }
    finally:
        conn.close()


def get_embedding(slug):
    conn = get_conn()
    try:
        row = conn.execute("SELECT embedding FROM capture_events WHERE slug = ?", (slug,)).fetchone()
        return row["embedding"] if row else None
    finally:
        conn.close()


def get_trash_row(batch_id, slug):
    """One trash entry, embedding included (None when absent)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM trash WHERE batch_id = ? AND slug = ?", (batch_id, slug)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _set_trash_embedding(batch_id, slug, embedding):
    conn = get_conn()
    try:
        conn.execute("UPDATE trash SET embedding = ? WHERE batch_id = ? AND slug = ?", (embedding, batch_id, slug))
        conn.commit()
    finally:
        conn.close()


def list_trash(expired_before=None, holds=None):
    """Unpurged trash entries, oldest first (no embedding). `expired_before` (epoch) keeps only
    those whose expires_at is at or before it. `holds`: None = all, False = ordinary deletes only
    (expires_at set), True = redact holds only (expires_at NULL: kept until the owner decides)."""
    conn = get_conn()
    try:
        sql = ("SELECT batch_id, slug, stored_filename, has_original, has_thumb, dir, size_bytes, title, reason, "
               "created_at, expires_at FROM trash WHERE purged_at IS NULL")
        args = []
        if holds is True:
            sql += " AND expires_at IS NULL"
        elif holds is False:
            sql += " AND expires_at IS NOT NULL"
        if expired_before is not None:
            sql += " AND expires_at IS NOT NULL AND expires_at <= ?"
            args.append(expired_before)
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at, slug", args)]
    finally:
        conn.close()


def get_redact_hold(slug):
    """The live redact hold for `slug` (file still stored in the trash, no expiry), else None."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM trash WHERE slug = ? AND reason = 'redact' AND expires_at IS NULL "
                           "AND purged_at IS NULL ORDER BY created_at DESC LIMIT 1", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def redact_hold_slugs():
    conn = get_conn()
    try:
        return {r["slug"] for r in conn.execute(
            "SELECT slug FROM trash WHERE reason = 'redact' AND expires_at IS NULL AND purged_at IS NULL")}
    finally:
        conn.close()


def _mark_trash_purged(batch_id, slug, when):
    """The entry's files are gone for good; the row stays as a record (and makes undo refuse)."""
    conn = get_conn()
    try:
        conn.execute("UPDATE trash SET purged_at = ?, embedding = NULL WHERE batch_id = ? AND slug = ?",
                     (when, batch_id, slug))
        conn.commit()
    finally:
        conn.close()


def _add_relation(slug_a, slug_b):
    """Symmetric — stored both directions so listing either side's related
    items is a single indexed lookup, not an OR query.

    #16: a "related to" link used to be purely descriptive — it connected
    two slugs but did nothing about either side's categorization. That let
    a related item carry zero tags and zero project membership, which made
    it invisible everywhere tag/project browsing is the only way in (the
    home page's Projects column, a /?tag= filter (since #370 the home pills
    filter by ?hobby= instead), a project's own detail
    page) — it would only ever surface again via a direct /object/<slug>
    link or the raw uploader-grouped Gallery pane on the left of the home
    page, which lists every row unfiltered regardless of tags (see
    home_page's docstring). That raw pane is the "hidden gallery" the issue
    means: it's where an uncategorized item quietly piles up, permanently
    absent from the actual curated browsing surface.

    The fix: relating two items now merges their categorization both ways
    — each side picks up whatever tags and project memberships the other
    side already has (see _sync_relation_categorization below), so as long
    as *either* side already has a tag or project, both sides end up
    visible. If neither side has any tag or project yet, there's nothing to
    inherit and the pair stays uncategorized — this doesn't force tagging
    out of thin air, it just stops "related to" from being a way to silently
    orphan an otherwise-categorized item's new companion."""
    if slug_a == slug_b:
        return
    conn = get_conn()
    try:
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO capture_event_relations (slug_a, slug_b, created_at) VALUES (?, ?, ?)",
            (slug_a, slug_b, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO capture_event_relations (slug_a, slug_b, created_at) VALUES (?, ?, ?)",
            (slug_b, slug_a, now),
        )
        conn.commit()
        _sync_relation_categorization(slug_a, slug_b)
    finally:
        conn.close()


def _sync_relation_categorization(slug_a, slug_b):
    """Propagates tags and project membership both ways between two newly
    related slugs — see add_relation's docstring (#16) for why. Runs after
    add_relation's own transaction commits, using the existing
    attach_tags/add_item_to_project primitives (both already idempotent via
    INSERT OR IGNORE) rather than a bespoke bulk query, so this stays
    consistent with how tags/project membership are written everywhere
    else."""
    tags_a = {t["id"] for t in list_tags_for_post(slug_a)}
    tags_b = {t["id"] for t in list_tags_for_post(slug_b)}
    missing_for_a = tags_b - tags_a
    missing_for_b = tags_a - tags_b
    if missing_for_a:
        _attach_tags(slug_a, list(missing_for_a))
    if missing_for_b:
        _attach_tags(slug_b, list(missing_for_b))

    projects_a = {p["id"] for p in list_projects_for_post(slug_a)}
    projects_b = {p["id"] for p in list_projects_for_post(slug_b)}
    for project_id in projects_b - projects_a:
        _add_item_to_project(project_id, slug_a)
    for project_id in projects_a - projects_b:
        _add_item_to_project(project_id, slug_b)


def _remove_relation(slug_a, slug_b):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? AND slug_b = ?", (slug_a, slug_b))
        conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? AND slug_b = ?", (slug_b, slug_a))
        conn.commit()
    finally:
        conn.close()


def list_related(slug):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ce.* FROM capture_event_relations r JOIN capture_events ce ON ce.slug = r.slug_b "
            "WHERE r.slug_a = ? ORDER BY ce.timestamp DESC",
            (slug,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# --- Revision chains (#477) ---
# item_revisions(old_slug PK, new_slug): "new supersedes old", at most one successor per item,
# so a chain A -> B -> C is two rows and the current revision is the item with no row of its
# own. Validation and the change log live in core/revisions.py; this is the raw access the
# listings share. Deliberately NOT capture_event_relations: that table is symmetric and its
# add_relation merges tags/projects, neither of which fits a directed "replaces" link.

def _unlink_revision(conn, slug):
    """Removes `slug` from its chain on the caller's connection: its predecessors are
    re-pointed at its successor (or become the chain's end when there is none)."""
    row = conn.execute("SELECT new_slug FROM item_revisions WHERE old_slug = ?", (slug,)).fetchone()
    if row is not None:
        conn.execute("DELETE FROM item_revisions WHERE old_slug = ?", (slug,))  # first: new_slug is UNIQUE
        conn.execute("UPDATE item_revisions SET new_slug = ? WHERE new_slug = ?", (row["new_slug"], slug))
    else:
        conn.execute("DELETE FROM item_revisions WHERE new_slug = ?", (slug,))


LOOKALIKE_IMAGE_CLAUSE = "(media_type = 'image' OR media_type IS NULL OR media_type = '')"


def list_lookalike_images():
    """(#759) Every un-redacted image item with an uploaded file, as row dicts without the embedding
    BLOB, oldest first: what the look-alike pass (core/lookalikes.py) considers. Rows with no usable
    perceptual hash are included (the pass counts them)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE COALESCE(redacted, 0) = 0 AND stored_filename IS NOT NULL "
            f"AND stored_filename != '' AND {LOOKALIKE_IMAGE_CLAUSE} ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            out.append(d)
        return out
    finally:
        conn.close()


def revision_pairs():
    """{old_slug: new_slug} for every direct supersedes link. One small query."""
    conn = get_conn()
    try:
        return {r["old_slug"]: r["new_slug"] for r in conn.execute("SELECT old_slug, new_slug FROM item_revisions")}
    finally:
        conn.close()


def superseded_slugs():
    """Slugs that have a successor (everything in a chain except its current revision)."""
    return set(revision_pairs())


def _not_superseded(prefix=""):
    """SQL fragment keeping superseded items out of a browse query (#477): only the
    current revision of each chain is listed. `prefix` is a table alias like "ce."."""
    return f" AND NOT EXISTS (SELECT 1 FROM item_revisions ir WHERE ir.old_slug = {prefix}slug)"


# --- Project-to-project relations (#408) ---
# Peer links between projects, mirroring capture_event_relations for objects
# (projects live in their own table, so they can't reuse that object-only
# relation store). Symmetric: stored both directions so either side's list is
# a single lookup. Unlike object relations there's no categorization sync —
# projects aren't tag/membership carriers the way objects are; a related link
# is just a link.
#
# V2 cards 3.8: links are typed. A row (a, b, type) reads "a <type> b"; directed
# types are one row, `related` is two (a,b)+(b,a). Writes go through
# write_project_links (called only from core/cards.py, which validates and logs);
# the old add_project_relation / remove_project_relation helpers are gone so
# nothing can write a link around the rules.
def _write_project_links(deletes, inserts, op, actor, batch_id=None, affected_slugs=None):
    """Applies link deletions then insertions in ONE transaction and logs their
    row images as one change-log row. `deletes`: [(slug_a, slug_b, type)];
    `inserts`: [{slug_a, slug_b, type, note?}]. Returns the change-log row id
    (None when there was nothing to do). Validation is the caller's."""
    if not deletes and not inserts:
        return None
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        muts = []
        for a, b, t in deletes:
            key = {"slug_a": a, "slug_b": b, "type": t}
            before = image_get(conn, "project_relations", key)  # #541 phase D: full image incl. rowid
            if before is None:
                continue
            conn.execute("DELETE FROM project_relations WHERE slug_a = ? AND slug_b = ? AND type = ?", (a, b, t))
            muts.append({"table": "project_relations", "key": key, "before": before, "after": None})
        now = time.time()
        for ins in inserts:
            a, b, t = ins["slug_a"], ins["slug_b"], ins["type"]
            note = ins.get("note") or ""
            conn.execute("INSERT INTO project_relations (slug_a, slug_b, type, note, created_at) VALUES (?, ?, ?, ?, ?)",
                         (a, b, t, note, now))
            key = {"slug_a": a, "slug_b": b, "type": t}
            muts.append({"table": "project_relations", "key": key, "before": None,
                         "after": image_get(conn, "project_relations", key)})
        row_id = insert_change_log(conn, op, actor, muts, batch_id=batch_id, affected_slugs=affected_slugs)
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_project_link_rows(slug=None, pair=None):
    """Raw project_relations rows ({slug_a, slug_b, type, note, created_at}).
    `slug`: every row touching that card (either end). `pair`: (x, y), every row
    between those two cards in either direction. No filter: all rows."""
    conn = get_conn()
    try:
        if pair:
            x, y = pair
            rows = conn.execute("SELECT * FROM project_relations WHERE (slug_a = ? AND slug_b = ?) OR (slug_a = ? AND slug_b = ?) "
                                "ORDER BY created_at, type", (x, y, y, x)).fetchall()
        elif slug:
            rows = conn.execute("SELECT * FROM project_relations WHERE slug_a = ? OR slug_b = ? ORDER BY created_at, type",
                                (slug, slug)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM project_relations ORDER BY slug_a, slug_b, type").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def connected_project_ids():
    """(#534) Ids of every card with at least one real connection: a hobby membership, a
    family/collection membership (as member or as the family), nesting (a parent or at least
    one child via parent_id) or any project_relations link of any type. One query for the
    whole set, memoised for the life of a read_session(), so scoring N cards costs one lookup
    rather than N."""
    memo = getattr(_ambient, "memo", None)
    if memo is not None and "connected_ids" in memo:
        return set(memo["connected_ids"])
    conn = get_conn()
    try:
        parts = [
            "SELECT project_id AS id FROM project_hobbies",
            "SELECT id FROM projects WHERE parent_id IS NOT NULL",
            "SELECT parent_id AS id FROM projects WHERE parent_id IS NOT NULL",
            "SELECT p.id FROM projects p JOIN project_relations r ON r.slug_a = p.slug OR r.slug_b = p.slug",
        ]
        if _table_exists(conn, "family_members"):
            parts += ["SELECT family_id AS id FROM family_members", "SELECT member_id AS id FROM family_members"]
        ids = {r[0] for r in conn.execute(" UNION ".join(parts))}
    finally:
        conn.close()
    if memo is not None:
        memo["connected_ids"] = set(ids)
    return ids


def list_linked_projects(slug):
    """Every project linked to `slug` by ANY link type, either direction (typed
    links included), newest-updated first. For 'is this card connected at all'."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.* FROM projects p WHERE p.slug IN ("
            "SELECT slug_b FROM project_relations WHERE slug_a = ? UNION "
            "SELECT slug_a FROM project_relations WHERE slug_b = ?) ORDER BY p.updated_at DESC",
            (slug, slug),
        ).fetchall()
        return [_card_row(r) for r in rows]
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


# --- Blog tags ---
# A loose, nestable tag tree (Section > Category > ... as deep as someone
# wants to go) separate from capture_events' own flat `tags` JSON column,
# which is a different feature (freeform screenshot labels). A post can
# carry any number of these tags at any depth — there's no fixed "one
# category per post" rule, which is what lets the Projects page act as a
# real table of contents instead of a forced single-parent taxonomy.

def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "tag"


def get_tag(tag_id):
    """Look up one blog_tags row by id. Added for #274: a project's
    linked tag_id needs to be resolved to its real name so
    web/app.py's _attach_to_project can also merge that name into the
    row's own free-text tags column (see that function's docstring)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM blog_tags WHERE id = ?", (tag_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _find_tag_by_name(name):
    """Search the entire tag tree for a tag with the given name, regardless
    of parent. Returns the first match, or None if not found. Used to prevent
    creating duplicate root-level tags when a child tag with the same name
    already exists (#213).

    Do NOT use this to resolve a free-text tag name back to "the" root tag
    it refers to (sync_real_tags_for_post's previous-tag detach check needs
    exactly that) -- with no parent filter, this can return a same-named
    CHILD tag instead of the root one, which then fails that function's
    `tag["parent_id"] is None` eligibility check and silently leaves a
    stale post_tags row behind (#280). Use _find_root_tag_by_name for that."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM blog_tags WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _find_root_tag_by_name(name):
    """Root-level-only counterpart to _find_tag_by_name (#280) -- the free-
    text tags column can only ever reference a root-level tag (see
    sync_real_tags_for_post), so resolving a previously-typed name back to
    "the" tag it refers to must be scoped to parent_id IS NULL, not the
    whole tree. Read-only (unlike get_or_create_tag, which creates one if
    missing -- wrong side effect for this "was this actually attached"
    check). Returns None if no root-level tag has this name."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM blog_tags WHERE name = ? AND parent_id IS NULL", (name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_or_create_tag(name, parent_id=None):
    """Looked up by (name, parent_id) so the same tag name can exist under
    different parents (e.g. a "Camera" tag under both "FPV" and "3D
    Printing") without colliding — only the slug has to be globally unique,
    and a name collision there just gets a numeric suffix.

    #213 fix: when parent_id is None (root-level lookup for free-text tags),
    first search the whole tree for an existing tag with that name before
    creating a new one. This prevents creating a duplicate root-level tag
    when the user types a child tag's name into the free-text box."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM blog_tags WHERE name = ? AND parent_id IS ?", (name, parent_id)
        ).fetchone()
        if row:
            return dict(row)

        # #213 fix: if we're creating a root-level tag, first check if this name
        # exists anywhere in the tree. If so, reuse it instead of creating a duplicate.
        if parent_id is None:
            existing_tag = _find_tag_by_name(name)
            if existing_tag:
                return existing_tag

        slug = _slugify(name)
        base_slug = slug
        n = 2
        while conn.execute("SELECT 1 FROM blog_tags WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{n}"
            n += 1
        cur = conn.execute(
            "INSERT INTO blog_tags (name, slug, parent_id) VALUES (?, ?, ?)", (name, slug, parent_id)
        )
        conn.commit()
        tag_id = cur.lastrowid
        return {"id": tag_id, "name": name, "slug": slug, "parent_id": parent_id}
    finally:
        conn.close()


def _attach_tags(post_slug, tag_ids):
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO post_tags (post_slug, tag_id) VALUES (?, ?)",
            [(post_slug, tag_id) for tag_id in tag_ids],
        )
        conn.commit()
    finally:
        conn.close()


def _detach_tag(post_slug, tag_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM post_tags WHERE post_slug = ? AND tag_id = ?", (post_slug, tag_id))
        conn.commit()
    finally:
        conn.close()


def list_tags_for_post(post_slug):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT t.* FROM blog_tags t JOIN post_tags pt ON pt.tag_id = t.id WHERE pt.post_slug = ? ORDER BY t.name",
            (post_slug,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def tag_ids_for_posts(post_slugs):
    """(#524) The set of tag ids attached to any of `post_slugs`, in as few queries as
    possible (list_tags_for_post once per post was the Curator's biggest N+1)."""
    slugs = list(post_slugs or [])
    out = set()
    if not slugs:
        return out
    conn = get_conn()
    try:
        for i in range(0, len(slugs), 500):  # stay well under SQLite's variable limit
            chunk = slugs[i:i + 500]
            marks = ",".join("?" * len(chunk))
            out.update(r[0] for r in conn.execute(
                f"SELECT DISTINCT t.id FROM blog_tags t JOIN post_tags pt ON pt.tag_id = t.id "
                f"WHERE pt.post_slug IN ({marks})", chunk))
        return out
    finally:
        conn.close()


def list_tag_tree():
    """Every tag, nested under its parent — the Projects page's table of
    contents renders straight from this. Built in Python rather than a
    recursive CTE since the tree is small and this is far easier to read."""
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM blog_tags ORDER BY name").fetchall()]
        by_id = {row["id"]: {**row, "children": []} for row in rows}
        roots = []
        for row in rows:
            node = by_id[row["id"]]
            if row["parent_id"] is not None and row["parent_id"] in by_id:
                by_id[row["parent_id"]]["children"].append(node)
            else:
                roots.append(node)
        return roots
    finally:
        conn.close()


def _descendant_tag_ids(tag_id):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, parent_id FROM blog_tags").fetchall()
        children_by_parent = {}
        for r in rows:
            children_by_parent.setdefault(r["parent_id"], []).append(r["id"])
        ids = [tag_id]
        frontier = [tag_id]
        while frontier:
            frontier = [child for parent in frontier for child in children_by_parent.get(parent, [])]
            ids.extend(frontier)
        return ids
    finally:
        conn.close()


def list_posts_for_tag(tag_id, include_descendants=True, limit=50, offset=0):
    """Posts tagged with `tag_id`, or anywhere under it in the tree when
    include_descendants — e.g. the "Projects" root category page shows
    every post filed under any of its child tags too, not just posts
    tagged with "Projects" directly (which would almost never happen).
    Newest first, ties by slug, so `offset` pages through it stably (#722: MCP list_items paging)."""
    tag_ids = _descendant_tag_ids(tag_id) if include_descendants else [tag_id]
    placeholders = ",".join("?" for _ in tag_ids)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT ce.* FROM capture_events ce JOIN post_tags pt ON pt.post_slug = ce.slug "
            f"WHERE pt.tag_id IN ({placeholders}) AND ce.redacted = 0{policy.sql_browse_clause('ce.')} ORDER BY ce.timestamp DESC, ce.slug LIMIT ? OFFSET ?",
            tag_ids + [limit, max(0, int(offset))],
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# --- Projects ---
# A curated collection of posts an owner deliberately assembles into one
# card — distinct from blog_tags/post_tags, which is automatic grouping by
# whatever tags a post happens to carry. A project has its own identity
# (title, description, optional cover image) and a hand-ordered set of
# member posts, rather than being derived from tag membership.

def _create_project(title, description="", cover_slug=None, tag_id=None, parent_id=None, with_writeup=True,
                   kind=None, stage=None, stop_reason=None, actor=None, batch_id=None):
    """Auto-generates a unique slug from title, same dedup-with-numeric-
    suffix pattern as get_or_create_tag.

    tag_id optionally links this project to a blog_tags row (see #1 —
    "tied to the site tags") — callers that want a project reachable via the
    home page's tag filter should pass _get_or_create_tag(title)["id"]
    themselves rather than this function inventing the tag on its own, since
    not every project needs (or predates) a tag link.

    parent_id optionally links this project to a parent project (#133),
    enabling a simple hierarchy of nested projects.

    kind/stage/stop_reason (V2 cards): default to a Project that is In progress
    (matching v1's default status='active'); validated by core/card_rules and a
    rule violation raises card_rules.CardError. There is no v1 `status` any more
    (retired in #545, the column dropped in #760)."""
    from . import card_rules, changes
    batch_id = batch_id or changes.new_batch_id()
    kind = card_rules.validate_kind(kind or card_rules.DEFAULT_KIND)
    status_triple = card_rules.validate_status(kind, stage or card_rules.DEFAULT_STAGE, stop_reason)
    if parent_id is not None:
        # Nest rules (3.7) guard creation too: the parent must exist and be nestable
        # under, and a new group-kind card can't start life nested.
        parent_row = get_project(parent_id)
        if parent_row is None:
            raise card_rules.CardError("not_found", f"No such parent card: {parent_id!r}")
        card_rules.validate_nest({"id": None, "kind": kind, "title": title, "parent_id": None}, parent_row, ())
        parent_id = parent_row["id"]
    conn = get_conn()
    try:
        slug = _slugify(title)
        # Never mint a purely-numeric slug: get_project treats an all-digits
        # string as an id (it must — project_id arrives as a numeric string in
        # many Form-based callers), so a slug like "2" would be shadowed by
        # id lookup and unreachable by slug (#320, sibling of #318).
        if slug.isdigit():
            slug = f"{slug}-project"
        base_slug = slug
        n = 2
        while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{n}"
            n += 1
        now = time.time()
        from . import actor as actor_ctx, users  # lazy: users imports this module
        created_by = users.user_id_for_actor(actor_ctx.resolve(actor))  # #604: NULL = admin-owned
        via_token = actor_ctx.token_id_for(actor) if created_by is not None else None  # #778
        cur = conn.execute(
            "INSERT INTO projects (slug, title, description, cover_slug, created_at, updated_at, tag_id, parent_id, "
            "kind, activity, stage, stop_reason, created_by_user_id, created_via_token_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, title, description, cover_slug, now, now, tag_id, parent_id,
             kind, status_triple["activity"], status_triple["stage"], status_triple["stop_reason"], created_by,
             via_token),
        )
        project_id = cur.lastrowid
        project = {
            "id": project_id,
            "slug": slug,
            "title": title,
            "description": description,
            "cover_slug": cover_slug,
            "created_at": now,
            "updated_at": now,
            "tag_id": tag_id,
            "parent_id": parent_id,
            "writeup_slug": None,
            "kind": kind,
            "created_by_user_id": created_by,
            "created_via_token_id": via_token,
            **status_triple,
        }
        insert_change_log(
            conn, "create_card", actor,
            [{"table": "projects", "key": {"id": project_id}, "before": None,
              "after": {"slug": slug, "title": title, "kind": kind, **status_triple, "parent_id": parent_id}}],
            batch_id=batch_id, affected_slugs=[slug],
        )
        conn.commit()
    finally:
        conn.close()
    # #156/#423: every project gets a blank write-up document, wired in here in
    # the db layer so NO create path can silently skip it — the HTTP routes, the
    # constructicon_create_project MCP tool, and seed scripts all funnel through
    # create_project. (#423 caught the MCP tool missing it because the auto-
    # writeup used to live only in web/app.py's _create_writeup_for_project.)
    # with_writeup=False is the opt-out for a caller that deliberately wants none.
    if with_writeup:
        project["writeup_slug"] = _make_project_writeup(project, actor=actor, batch_id=batch_id)
    return project


def _make_project_writeup(project, actor=None, batch_id=None):
    """Create the blank document-type write-up for a project and wire it up:
    a capture_event with an empty type_metadata.body, added to the project's
    items, tagged with the project's linked tag (so it's reachable via tag
    browsing, #219), and set as the project's writeup_slug. Returns the slug."""
    from . import storage
    writeup_slug = storage.make_slug()
    before_card = get_project(project["id"]) or {}
    insert_content(
        slug=writeup_slug,
        uploaded_by=SOURCE_AUTHORED,
        media_type="document",
        content_description=f"{project['title']} — Write-up",
        type_metadata={"body": ""},
    )
    _add_item_to_project(project["id"], writeup_slug)
    if project.get("tag_id"):
        _attach_tags(writeup_slug, [project["tag_id"]])
    conn = get_conn()
    try:
        conn.execute("UPDATE projects SET writeup_slug = ?, updated_at = ? WHERE id = ?",
                     (writeup_slug, time.time(), project["id"]))
        conn.commit()
    finally:
        conn.close()
    if batch_id:
        # Image the write-up's three rows (V2 cards 3.13) so undoing the card's creation
        # (or a split part) takes its auto-made blank write-up with it.
        conn = get_conn()
        try:
            after_card = conn.execute("SELECT writeup_slug, updated_at FROM projects WHERE id = ?",
                                      (project["id"],)).fetchone()
            muts = [
                {"table": "capture_events", "key": {"slug": writeup_slug}, "before": None,
                 "after": image_get(conn, "capture_events", {"slug": writeup_slug})},
                {"table": "project_items", "key": {"project_id": project["id"], "post_slug": writeup_slug},
                 "before": None,
                 "after": image_get(conn, "project_items", {"project_id": project["id"], "post_slug": writeup_slug})},
                {"table": "projects", "key": {"id": project["id"]},
                 "before": {"writeup_slug": before_card.get("writeup_slug"), "updated_at": before_card.get("updated_at")},
                 "after": {"writeup_slug": after_card["writeup_slug"], "updated_at": after_card["updated_at"]}},
            ]
            if project.get("tag_id"):
                # #541 phase D: the write-up's linked-tag row is imaged too, so a redo (undo of the
                # undo) brings it back with the document.
                key = {"post_slug": writeup_slug, "tag_id": project["tag_id"]}
                muts.insert(1, {"table": "post_tags", "key": key, "before": None,
                                "after": image_get(conn, "post_tags", key)})
            insert_change_log(conn, "create_writeup", actor, muts, batch_id=batch_id, affected_slugs=[project["slug"]])
            conn.commit()
        finally:
            conn.close()
    return writeup_slug


def resolve_project_cover_slug(project_or_id, _depth=0):
    """Resolves the effective cover slug for a project (#356).

    A project's cover can be either:
    1. An object cover_slug (direct reference to a capture_events row)
    2. A cover_project_id (borrowed from a child project)

    If cover_project_id is set, recursively resolves that child project's
    effective cover. Returns the final effective cover_slug (or None if no
    cover exists at the end of the chain).

    Guards against cycles and runaway recursion with a depth cap of 10.

    Accepts either a project dict or an id."""
    if _depth > 10:
        # Depth exceeded (likely a cycle or very deep chain)
        return None

    # Resolve to a project dict if given an id
    if isinstance(project_or_id, int) or (isinstance(project_or_id, str) and project_or_id.isdigit()):
        project = get_project(project_or_id)
        if project is None:
            return None
    else:
        project = project_or_id

    # Safety check: project must not be None at this point
    if project is None:
        return None

    # If this project has a direct cover_slug, return it
    if project.get("cover_slug"):
        return project["cover_slug"]

    # If this project has a cover_project_id, resolve the child's effective cover
    if project.get("cover_project_id"):
        child = get_project(project["cover_project_id"])
        if child is None:
            return None
        # Recurse to the child
        return resolve_project_cover_slug(child, _depth=_depth + 1)

    # No cover at this level
    return None


PRIVATE_CARD_COLUMNS = ("agent_notes",)  # #745: never in a card row a reader hands out


def _card_row(row):
    """A projects row as a dict WITHOUT its private columns (#745). Every reader below that hands out
    whole card rows goes through this, so a route or shaper that dumps a row (or a future one that
    forgets to whitelist) can't leak the agent notes. Undo and the change log read the table
    themselves (image_get), so they still see and restore the column."""
    d = dict(row)
    for col in PRIVATE_CARD_COLUMNS:
        d.pop(col, None)
    return d


def get_card_agent_notes(card_id):
    """A card's private agent notes (#745), or None. The ONE reader of projects.agent_notes: MCP
    get_project and set_agent_notes only."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT agent_notes FROM projects WHERE id = ?", (int(card_id),)).fetchone()
        return row["agent_notes"] if row else None
    finally:
        conn.close()


def get_project(id_or_slug):
    """Look up by either numeric id or slug — a project detail page will
    likely be reached by slug in a URL, but internal callers (e.g.
    add_item_to_project) often already have the id."""
    conn = get_conn()
    try:
        if isinstance(id_or_slug, int) or (isinstance(id_or_slug, str) and id_or_slug.isdigit()):
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(id_or_slug),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM projects WHERE slug = ?", (id_or_slug,)).fetchone()
        return _card_row(row) if row else None
    finally:
        conn.close()


def list_projects(kind=None, activity=None, stage=None):
    """All projects, most-recently-updated first, optionally filtered on the live V2
    columns. (The v1 `status` column is gone, #545 / #760.)"""
    conn = get_conn()
    try:
        clauses, args = [], []
        for col, val in (("kind", kind), ("activity", activity), ("stage", stage)):
            if val is not None:
                clauses.append(f"{col} = ?")
                args.append(val)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(f"SELECT * FROM projects{where} ORDER BY updated_at DESC", args).fetchall()
        return [_card_row(r) for r in rows]
    finally:
        conn.close()


def check_nest(child, parent_id, replace=False):
    """Runs the "part of" rules (card_rules.validate_nest, V2 cards 3.7) for
    nesting `child` (a project dict) under `parent_id`. Returns the parent row;
    raises card_rules.CardError (not_found / nest_*). No writes."""
    from . import card_rules
    parent_row = get_project(parent_id)
    if parent_row is None:
        raise card_rules.CardError("not_found", f"No such parent card: {parent_id!r}")
    card_rules.validate_nest(child, parent_row, _descendant_project_ids(child["id"]), replace=replace)
    return parent_row


def _set_display_date_override(slug, value):
    """value=None clears the override, reverting to the computed default
    (content_date, falling back to timestamp — see core/timeline.py)."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET display_date_override = ? WHERE slug = ?", (value, slug))
        conn.commit()
    finally:
        conn.close()


def _set_content_date(slug, value):
    """Sets the content's own real-world date directly (distinct from
    display_date_override, which is a manual override of the *displayed*
    date on top of this — see core/timeline.py's resolve_item_date chain).
    Added so a correction/backfill script with a real known date (e.g. a
    YouTube video's publishedAt) can persist it via the HTTP API rather
    than writing to the DB directly — see scripts/full_youtube_channel_sync.py's
    correct_content_row, which previously fetched this from the API on
    every correction pass but had no way to actually write it back for an
    already-imported row."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET content_date = ? WHERE slug = ?", (value, slug))
        conn.commit()
    finally:
        conn.close()


def _descendant_project_ids(project_id):
    """Recursively get all descendant project IDs (children, grandchildren, etc.)
    for a given project — similar to _descendant_tag_ids but for projects.
    Used for cycle detection and for breadcrumb navigation."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, parent_id FROM projects").fetchall()
        children_by_parent = {}
        for r in rows:
            children_by_parent.setdefault(r["parent_id"], []).append(r["id"])
        ids = [project_id]
        frontier = [project_id]
        while frontier:
            frontier = [child for parent in frontier for child in children_by_parent.get(parent, [])]
            ids.extend(frontier)
        return ids
    finally:
        conn.close()


def list_child_projects(project_id):
    """Get the direct children of a project (not including grandchildren).
    Returns a list of project dicts, ordered by title."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM projects WHERE parent_id = ? ORDER BY title",
            (project_id,),
        ).fetchall()
        return [_card_row(r) for r in rows]
    finally:
        conn.close()


def _add_item_to_project(project_id, post_slug, sort_order=None):
    """If sort_order isn't given, appends at the end (max existing
    sort_order + 1, or 0 if the project has no items yet)."""
    conn = get_conn()
    try:
        if sort_order is None:
            row = conn.execute(
                "SELECT MAX(sort_order) AS m FROM project_items WHERE project_id = ?", (project_id,)
            ).fetchone()
            sort_order = (row["m"] + 1) if row["m"] is not None else 0
        conn.execute(
            "INSERT OR IGNORE INTO project_items (project_id, post_slug, sort_order) VALUES (?, ?, ?)",
            (project_id, post_slug, sort_order),
        )
        conn.commit()
    finally:
        conn.close()


def _remove_item_from_project(project_id, post_slug):
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM project_items WHERE project_id = ? AND post_slug = ?", (project_id, post_slug)
        )
        conn.commit()
    finally:
        conn.close()


def list_project_items(project_id):
    """The posts in a project, joined with capture_events so full post data
    comes back — mirrors how list_posts_for_tag joins post_tags to
    capture_events.

    Ordered by each item's real-world chronology (#300): the same
    effective-date chain core/timeline.py's resolve_item_date establishes
    for the Timeline feature (display_date_override → date made → content_date →
    a pre-install source_modified_at; #725: undated items last), oldest first, so the project page
    reads as the project actually happened rather than in the order things
    were filed. Resolved in Python rather than inlined into SQL so there's
    exactly one implementation of that chain. The SQL still orders by
    project_items.sort_order and the Python sort is stable, so sort_order
    remains the tie-break for items resolving to the same instant — the
    column itself, and the manual-curation discipline behind project
    membership, are untouched (see CLAUDE.md's "Projects are curated, not
    auto-generated").

    (#524) Inside read_session() the result is kept for the rest of the session (the
    Curator derives from the same project's items 3 or 4 times). The returned list is a
    fresh one each time; the item dicts are shared, so treat them as read-only there."""
    memo = getattr(_ambient, "memo", None)
    if memo is not None and ("items", project_id) in memo:
        return list(memo[("items", project_id)])
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ce.* FROM project_items pi JOIN capture_events ce ON ce.slug = pi.post_slug "
            "WHERE pi.project_id = ? AND ce.redacted = 0 ORDER BY pi.sort_order ASC",
            (project_id,),
        ).fetchall()
        items = [_row_to_dict(r) for r in rows]
        cutoff = timeline.first_upload()
        # #725: undated items (no real date) go last, in sort_order (the sort is stable).
        items.sort(key=lambda i: timeline.date_sort_key(timeline.resolve_item_date(i, cutoff)))
        if memo is not None:
            memo[("items", project_id)] = items
            return list(items)
        return items
    finally:
        conn.close()


def privacy_snapshot():
    """(#604 step 3) Everything core/policy.py needs to decide card / hobby visibility, read in one
    go. None when nothing at all is private (the common case: one cheap query each). Otherwise
    {"cards": {id: owner_user_id}, "hobbies": {tag_id: owner_user_id}, "members": {(kind, id):
    {user_id: role}}, "children": {parent_id: [card ids]} (projects.parent_id), "family": {family_id:
    [member ids]}, "hobby_cards": {tag_id: [card ids]}}: the private nodes and the edges along which
    privacy is inherited downward. Inside a read_session() (a page render) it is read once and kept
    for the session (read-only work, so it can't go stale; a write clears the memo)."""
    memo = getattr(_ambient, "memo", None)
    if memo is not None and "privacy_snapshot" in memo:
        return memo["privacy_snapshot"]
    snap = _privacy_snapshot()
    if memo is not None:
        memo["privacy_snapshot"] = snap
    return snap


def _privacy_snapshot():
    conn = get_conn()
    try:
        cards_p = {r["id"]: r["created_by_user_id"] for r in conn.execute(
            "SELECT id, created_by_user_id FROM projects WHERE COALESCE(private, 0) = 1")}
        hobbies_p = {r["hobby_tag_id"]: r["created_by_user_id"] for r in conn.execute(
            "SELECT hs.hobby_tag_id, hs.created_by_user_id FROM hobby_settings hs "
            "JOIN blog_tags bt ON bt.id = hs.hobby_tag_id WHERE COALESCE(hs.private, 0) = 1 AND bt.is_hobby = 1")}
        if not cards_p and not hobbies_p:
            return None
        members = {}
        for r in conn.execute("SELECT target_kind, target_id, user_id, role FROM share_members"):
            members.setdefault((r["target_kind"], r["target_id"]), {})[r["user_id"]] = r["role"]
        children, family, hobby_cards = {}, {}, {}
        for r in conn.execute("SELECT id, parent_id FROM projects WHERE parent_id IS NOT NULL"):
            children.setdefault(r["parent_id"], []).append(r["id"])
        for r in conn.execute("SELECT family_id, member_id FROM family_members"):
            family.setdefault(r["family_id"], []).append(r["member_id"])
        for r in conn.execute("SELECT project_id, hobby_tag_id FROM project_hobbies"):
            hobby_cards.setdefault(r["hobby_tag_id"], []).append(r["project_id"])
        return {"cards": cards_p, "hobbies": hobbies_p, "members": members, "children": children,
                "family": family, "hobby_cards": hobby_cards}
    finally:
        conn.close()


def card_parents(card_id):
    """(#604 step 3) What a card inherits privacy FROM: {"cards": [parent card id, family ids it is a
    member of], "hobbies": [hobby tag ids it is in]}. Read-only."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT parent_id FROM projects WHERE id = ?", (card_id,)).fetchone()
        up = [row["parent_id"]] if row and row["parent_id"] is not None else []
        up += [r["family_id"] for r in conn.execute("SELECT family_id FROM family_members WHERE member_id = ?",
                                                    (card_id,))]
        hobbies_ = [r["hobby_tag_id"] for r in conn.execute(
            "SELECT hobby_tag_id FROM project_hobbies WHERE project_id = ?", (card_id,))]
        return {"cards": up, "hobbies": hobbies_}
    finally:
        conn.close()


def slugs_in_cards(card_ids):
    """(#604 step 3) The item slugs on any of these cards (project_items)."""
    ids = [int(i) for i in card_ids]
    if not ids:
        return set()
    conn = get_conn()
    try:
        out = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            out |= {r[0] for r in conn.execute(
                f"SELECT post_slug FROM project_items WHERE project_id IN ({','.join('?' for _ in chunk)})", chunk)}
        return out
    finally:
        conn.close()


def card_tag_links():
    """(#604 step 3) {tag_id: [card ids whose linked tag it is]} (projects.tag_id)."""
    conn = get_conn()
    try:
        out = {}
        for r in conn.execute("SELECT id, tag_id FROM projects WHERE tag_id IS NOT NULL"):
            out.setdefault(r["tag_id"], []).append(r["id"])
        return out
    finally:
        conn.close()


def slugs_tagged(tag_ids):
    """(#604 step 3) The item slugs tagged directly (post_tags) with any of these tags: a hobby's
    loose objects, for a private hobby."""
    ids = [int(i) for i in tag_ids]
    if not ids:
        return set()
    conn = get_conn()
    try:
        return {r[0] for r in conn.execute(
            f"SELECT post_slug FROM post_tags WHERE tag_id IN ({','.join('?' for _ in ids)})", ids)}
    finally:
        conn.close()


def item_holders(slug):
    """(#604 step 3) {"cards": [card ids holding this item], "tags": [tag ids on it]}: what an item's
    visibility depends on."""
    conn = get_conn()
    try:
        return {"cards": [r[0] for r in conn.execute("SELECT project_id FROM project_items WHERE post_slug = ?", (slug,))],
                "tags": [r[0] for r in conn.execute("SELECT tag_id FROM post_tags WHERE post_slug = ?", (slug,))]}
    finally:
        conn.close()


def share_members(kind, target_id):
    """(#604 step 3) A private card's / hobby's members: [{user_id, role, added_at, username,
    display_name, user_role, disabled}], by name. Deleted users show with username None."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT sm.user_id, sm.role, sm.added_at, u.username, u.display_name, u.role AS user_role, u.disabled "
            "FROM share_members sm LEFT JOIN users u ON u.id = sm.user_id "
            "WHERE sm.target_kind = ? AND sm.target_id = ? ORDER BY LOWER(COALESCE(u.display_name, u.username, ''))",
            (kind, target_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def hobby_privacy(tag_id):
    """(#604 step 3) {private, created_by_user_id, created_via_token_id (#778)} for a hobby (no
    hobby_settings row = open, admin-owned)."""
    conn = get_conn()
    try:
        r = conn.execute("SELECT COALESCE(private, 0) AS private, created_by_user_id, created_via_token_id "
                         "FROM hobby_settings WHERE hobby_tag_id = ?", (tag_id,)).fetchone()
        return {"private": int(r["private"]), "created_by_user_id": r["created_by_user_id"],
                "created_via_token_id": r["created_via_token_id"]} if r else \
            {"private": 0, "created_by_user_id": None, "created_via_token_id": None}
    finally:
        conn.close()


def list_projects_for_post(post_slug):
    """Reverse lookup — which projects contain a given post. Uses
    idx_project_items_slug."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.* FROM projects p JOIN project_items pi ON pi.project_id = p.id "
            "WHERE pi.post_slug = ? ORDER BY p.updated_at DESC",
            (post_slug,),
        ).fetchall()
        return [_card_row(r) for r in rows]
    finally:
        conn.close()


def list_unfiled_items(limit=10000, include_brand=False, include_superseded=False, offset=0):
    """capture_events rows with no project_items row — "unfiled" uploads
    (#41). #17 moved the raw upload gallery into the home page's
    hover/drag pop-out and left the main page showing only Project tiles;
    with zero projects created (or an upload simply never filed into one),
    that made the home page look completely empty even though uploads
    still existed, which was mistaken for real data loss and led directly
    to an accidental production /api/delete-all. home_page renders this
    list as an always-visible "Unfiled" section so that state can never
    look like "everything is gone" again.

    #123 had also excluded anything with a non-empty tags array, not just
    project membership, on the theory that tagging was already "organizing"
    an item. #152 reverted this: tags are for sorting/search assistance,
    not a substitute for project membership, and an item that only ever
    got tagged (never added to a project) had nowhere else in the UI to be
    found and filed into a project. Only project membership means "filed"
    now.

    LEFT JOIN + IS NULL rather than NOT IN/NOT EXISTS — reads cleanest
    given project_items' exact shape (a plain (project_id, post_slug)
    membership row per project.py's PRIMARY KEY, no post_slug uniqueness
    across projects), and is symmetric with list_project_items' own
    JOIN-based style just below.
    """
    conn = get_conn()
    try:
        # #417: brand assets are excluded by default -- an unfiled brand asset
        # belongs in the Brand Vault, not the home "Unfiled" section. include_brand
        # re-includes them for an explicit enumerate-everything caller.
        brand_clause = "" if include_brand else "AND ce.is_brand_asset = 0 "
        if not include_superseded:  # #477: a superseded file is not "unfiled work", its current revision is
            brand_clause += _not_superseded("ce.").lstrip() + " "
        rows = conn.execute(
            "SELECT ce.* FROM capture_events ce LEFT JOIN project_items pi ON pi.post_slug = ce.slug "
            "WHERE pi.post_slug IS NULL AND ce.redacted = 0" + policy.sql_browse_clause("ce.") + " " + brand_clause +
            "ORDER BY ce.timestamp DESC, ce.slug LIMIT ? OFFSET ?",  # #722: slug breaks ties, so offset pages are stable
            (limit, max(0, int(offset))),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_unfiled_slugs(include_brand=False, include_superseded=False):
    """(#624) The slugs list_unfiled_items would return, without the rows: the home Files panel only
    needs to know WHICH items are unfiled (the amber lamp, the Unfiled/Filed toggle and its counts),
    and no longer carries a full row for each. Same filter as list_unfiled_items, no LIMIT."""
    conn = get_conn()
    try:
        brand_clause = "" if include_brand else "AND ce.is_brand_asset = 0 "
        if not include_superseded:
            brand_clause += _not_superseded("ce.").lstrip() + " "
        return [r[0] for r in conn.execute(
            "SELECT ce.slug FROM capture_events ce LEFT JOIN project_items pi ON pi.post_slug = ce.slug "
            "WHERE pi.post_slug IS NULL AND ce.redacted = 0" + policy.sql_browse_clause("ce.") + " " + brand_clause)]
    finally:
        conn.close()


def count_unfiled_items(include_brand=False, include_superseded=False):
    """(#524) len(list_unfiled_items(limit=huge)) without building a dict per row: the same
    WHERE clause, counted in SQL. For callers that only want the number (the Curator's
    unfiled-objects nudge)."""
    conn = get_conn()
    try:
        brand_clause = "" if include_brand else "AND ce.is_brand_asset = 0 "
        if not include_superseded:
            brand_clause += _not_superseded("ce.").lstrip() + " "
        return conn.execute(
            "SELECT COUNT(*) FROM capture_events ce LEFT JOIN project_items pi ON pi.post_slug = ce.slug "
            "WHERE pi.post_slug IS NULL AND ce.redacted = 0" + policy.sql_browse_clause("ce.") + " " + brand_clause
        ).fetchone()[0]
    finally:
        conn.close()


def list_loose_reference_objects(limit=500):
    """capture_events rows with provenance='reference' that are not filed in
    any project (#370 follow-up). "Loose" means they have no project_items row —
    reference objects that are already in a project are shown as part of that
    project, not duplicated here.

    Used to populate the Reference pill's "Reference material" section, showing
    objects classified as reference material but not yet curated into a project.
    Returns _row_to_dict rows ordered newest first (by rowid/timestamp desc).
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ce.* FROM capture_events ce LEFT JOIN project_items pi ON pi.post_slug = ce.slug "
            "WHERE ce.provenance = 'reference' AND ce.redacted = 0" + policy.sql_browse_clause("ce.") + " AND pi.post_slug IS NULL "
            "ORDER BY ce.rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_recent_items_by_type(limit_per_type=10, include_brand=False, include_superseded=False):
    """Most recent capture_events rows, independently fetched per media_type.
    Each type gets its own N most recent items, rather than competing within
    a shared global pool. Returns a dict keyed by media_type, each value a
    list of dicts, only including types that have at least one real row.

    Used to populate the Files home widget (#107) — ensures every type that
    has any uploads appears in the tabs, not just types that happen to fall
    within the global top-N pool.

    Limit defaults to 10 — smaller than the old global 20, since with up to
    11 types each contributing 10, the embedded JSON payload is still compact
    while ensuring diverse per-type coverage even when one type has a burst.
    """
    conn = get_conn()
    try:
        # #417: brand assets are excluded by default -- the Files home widget
        # is general browse, not the Brand Vault. include_brand re-includes them.
        brand_clause = "" if include_brand else " AND is_brand_asset = 0"
        if not include_superseded:  # #477: current revisions only
            brand_clause += _not_superseded()
        # Fetch all distinct media_types that have at least one (non-redacted,
        # #282) row -- a type whose only rows are redacted gets no tab.
        media_type_rows = conn.execute(
            "SELECT DISTINCT media_type FROM capture_events WHERE redacted = 0" + policy.sql_browse_clause() + brand_clause
        ).fetchall()

        result = {}
        for (media_type,) in media_type_rows:
            rows = conn.execute(
                "SELECT * FROM capture_events WHERE media_type = ? AND redacted = 0" + brand_clause +
                " ORDER BY timestamp DESC LIMIT ?",
                (media_type, limit_per_type),
            ).fetchall()
            if rows:
                result[media_type] = [_row_to_dict(r) for r in rows]

        return result
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


# --- Change log (V2 cards 3.13) ---
# Core card operations write row images into the audit_log's extra columns so a
# later piece can undo them. All SQL stays here; core/changes.py is the API.

def insert_change_log(conn, op, actor, mutations, batch_id=None, affected_slugs=None, details=None):
    """Writes one change-log row on the caller's connection (the caller commits,
    so the log row lands in the same transaction as the write it describes).
    `mutations` is a list of {table, key, before, after} row images.
    `actor` None = the current actor context (core/actor.py, #560): every core write
    passes through here, so this is where a defaulted actor is resolved.
    `details` (#541 phase C): an optional JSON-able summary kept in form_body, for a record
    that has no row images (delete-all writes its per-table counts there)."""
    actor = actor_ctx.resolve(actor)
    # #604 step 3: the private-card write guard (a viewer member only looks). It raises before the
    # log row exists, so the caller's transaction rolls the write back.
    policy.check_write(mutations, actor)
    # #778: the personal MCP key this write was made with (only when `actor` is that key's user);
    # core/changes.via renders "<user>'s <token name>" from it at read time.
    via_token = actor_ctx.token_id_for(actor)
    cur = conn.execute(
        "INSERT INTO audit_log (method, path, form_body, affected_slugs, status_code, error_detail, timestamp, "
        "op, actor, batch_id, mutations, via_token_id) VALUES (?, ?, ?, ?, 200, NULL, ?, ?, ?, ?, ?, ?)",
        ("CORE", f"core:{op}", json.dumps(details or {}), json.dumps(affected_slugs or []), time.time(), op,
         actor, batch_id, json.dumps(mutations or []), via_token),
    )
    return cur.lastrowid


def list_change_log(card_slug=None, batch_id=None, limit=50):
    """Change-log rows (those with an `op`), newest first, mutations parsed."""
    conn = get_conn()
    try:
        sql = "SELECT * FROM audit_log WHERE op IS NOT NULL"
        args = []
        if batch_id:
            sql += " AND batch_id = ?"
            args.append(batch_id)
        if card_slug:
            sql += " AND affected_slugs LIKE ?"
            args.append(f'%"{card_slug}"%')
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["mutations"] = json.loads(d["mutations"]) if d.get("mutations") else []
            d["affected_slugs"] = json.loads(d["affected_slugs"]) if d.get("affected_slugs") else []
            out.append(d)
        return out
    finally:
        conn.close()


# --- Row images, undo, and card-operation write helpers (V2 cards piece 6, 3.13) ---
# Every composite card operation records `row images` ({table, key, before, after})
# so `undo` can invert them generically. The tables below are the ones an image may
# name; `rowid` rides along for tables without an INTEGER PRIMARY KEY so undoing a
# delete restores the row in its original position (membership order is rowid order).

IMAGE_TABLE_KEYS = {
    "projects": ("id",),
    "project_items": ("project_id", "post_slug"),
    "project_hobbies": ("project_id", "hobby_tag_id"),
    "family_members": ("family_id", "member_id"),
    "project_relations": ("slug_a", "slug_b", "type"),
    "blog_entry_projects": ("entry_id", "project_id"),
    "pending_decisions": ("id",),
    "curator_dismissals": ("nudge_key",),
    "blog_tags": ("id",),
    "capture_events": ("slug",),
    "post_tags": ("post_slug", "tag_id"),
    "provenance_options": ("scope", "key"),
    "vocab_terms": ("id",),  # #556: editable brand roles + wallpaper tags (core/vocab.py)
    "item_revisions": ("old_slug",),  # #477: one successor per item
    # #541 phase B (core/items.py): what an item delete touches, plus its trash entry.
    "capture_event_relations": ("slug_a", "slug_b"),
    "blog_entry_items": ("entry_id", "post_slug"),
    "trash": ("batch_id", "slug"),
    # #541 phase D (core/blog.py): the entry row itself.
    "blog_entries": ("id",),
    # #562: install identity (core/install_config.py) and per-hobby settings (core/hobbies.py).
    "install_config": ("key",),
    "hobby_settings": ("hobby_tag_id",),
    # #604 step 3 (core/sharing.py): a private card's / hobby's members.
    "share_members": ("target_kind", "target_id", "user_id"),
}
_IMAGE_ROWID_TABLES = ("project_items", "project_hobbies", "family_members", "project_relations",
                       "blog_entry_projects", "post_tags")


class UndoConflict(Exception):
    """A row no longer matches the state the change log recorded (someone changed it)."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def _image_columns(conn, table):
    """Non-BLOB columns of `table` (embeddings are never imaged)."""
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})") if (r["type"] or "").upper() != "BLOB"]


def _key_where(table, key):
    cols = IMAGE_TABLE_KEYS[table]
    return " AND ".join(f"{c} = ?" for c in cols), [key[c] for c in cols]


def image_get(conn, table, key):
    """The current full row image for `key` (None when absent)."""
    where, args = _key_where(table, key)
    cols = _image_columns(conn, table)
    select = ", ".join(cols)
    if table in _IMAGE_ROWID_TABLES:
        select = "rowid AS rowid, " + select
    row = conn.execute(f"SELECT {select} FROM {table} WHERE {where}", args).fetchone()
    return dict(row) if row else None


def _image_insert_row(conn, table, key, values):
    row = {**key, **values}
    cols = list(row)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                 [row[c] for c in cols])


def _image_update_row(conn, table, key, fields):
    fields = {k: v for k, v in fields.items() if k != "rowid" and k not in IMAGE_TABLE_KEYS[table]}
    if not fields:
        return
    where, args = _key_where(table, key)
    conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in fields)} WHERE {where}",
                 list(fields.values()) + args)


def _image_delete_row(conn, table, key):
    where, args = _key_where(table, key)
    conn.execute(f"DELETE FROM {table} WHERE {where}", args)


class ImageLog:
    """Writes rows through a connection and collects their row images; flush() records
    them as ONE change-log row. Use inside `transaction()` (or any connection) so the
    writes and their log row commit together."""

    def __init__(self, op, actor, batch_id, affected_slugs=None, conn=None):
        self.conn = conn or get_conn()
        self.op, self.actor, self.batch_id = op, actor, batch_id
        self.slugs = list(affected_slugs or [])
        self.muts = []
        self.details = None  # #555: an optional JSON-able summary for the log row's form_body

    def get(self, table, key):
        return image_get(self.conn, table, key)

    def insert(self, table, key, values):
        _image_insert_row(self.conn, table, key, values)
        self.muts.append({"table": table, "key": dict(key), "before": None, "after": image_get(self.conn, table, key)})

    def insert_auto(self, table, values):
        """INSERT into a table whose key is an `id` INTEGER PRIMARY KEY the DB assigns (#541
        phase C: a new blog_tags row). Imaged like insert(); returns the new id."""
        cols = list(values)
        cur = self.conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                [values[c] for c in cols])
        key = {"id": cur.lastrowid}
        self.muts.append({"table": table, "key": key, "before": None, "after": image_get(self.conn, table, key)})
        return cur.lastrowid

    def update(self, table, key, fields):
        before = image_get(self.conn, table, key)
        if before is None:
            return False
        if all(before.get(c) == v for c, v in fields.items()):
            return False
        _image_update_row(self.conn, table, key, fields)
        after = image_get(self.conn, table, key)
        cols = [c for c in fields if before.get(c) != after.get(c)]
        self.muts.append({"table": table, "key": dict(key),
                          "before": {c: before.get(c) for c in cols}, "after": {c: after.get(c) for c in cols}})
        return True

    def delete(self, table, key):
        before = image_get(self.conn, table, key)
        if before is None:
            return False
        _image_delete_row(self.conn, table, key)
        self.muts.append({"table": table, "key": dict(key), "before": before, "after": None})
        return True

    def flush(self):
        if not self.muts:
            return None
        row_id = insert_change_log(self.conn, self.op, self.actor, self.muts, batch_id=self.batch_id,
                                   affected_slugs=sorted(set(self.slugs)), details=self.details)
        self.muts = []
        return row_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Inside transaction() these three calls are no-ops on the shared connection; on a
        # private connection they commit (or roll back) and release it.
        try:
            if exc_type is None:
                self.flush()
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False


def invert_image(conn, mutation, force=False):
    """Applies the inverse of one row image (after -> before). Unless `force`, raises
    UndoConflict when the row no longer equals the recorded `after`. Returns the
    mutation that was actually applied ({table, key, before: <what was there>,
    after: <what is there now>}) so the undo can itself be logged and undone."""
    table, key = mutation["table"], mutation["key"]
    if table not in IMAGE_TABLE_KEYS:
        raise UndoConflict(f"Can't undo a change to {table!r}.", {"table": table})
    expected, target = mutation.get("after"), mutation.get("before")
    # #704: a row image logged before a column was dropped still names it (capture_events.source,
    # client, ...). Compare and restore only the columns the table has now; the dropped ones held
    # nothing anything reads, and writing them back would fail.
    live = set(_image_columns(conn, table)) | {"rowid"}
    if table == "capture_events":
        # #555: `tags` is a cache derived from the tag tree (the chip triggers keep it), so an image
        # logged before #555 that names it is neither compared nor written back: the post_tags rows
        # that op imaged are what undo restores, and the cache follows them.
        live.discard("tags")
    if expected is not None:
        expected = {k: v for k, v in expected.items() if k in live}
    if target is not None:
        target = {k: v for k, v in target.items() if k in live}
    current = image_get(conn, table, key)
    if not force:
        if expected is None and current is not None:
            raise UndoConflict(f"A {table} row {key} exists now but didn't when the change was made.",
                               {"table": table, "key": key, "found": "row exists", "expected": "no row"})
        if expected is not None:
            if current is None:
                raise UndoConflict(f"The {table} row {key} was deleted since.",
                                   {"table": table, "key": key, "found": "no row", "expected": "row"})
            # updated_at last: it moves on every edit, so a real field difference is the better explanation.
            for col, want in sorted(expected.items(), key=lambda kv: kv[0] == "updated_at"):
                if col == "rowid":
                    continue
                if current.get(col) != want:
                    raise UndoConflict(
                        f"{table} {key} changed since: {col} is {current.get(col)!r}, was {want!r} when logged.",
                        {"table": table, "key": key, "field": col, "found": current.get(col), "expected": want})
    if target is None and table == "blog_tags" and current is not None and not force:
        # #541 phase C: undoing a tag's creation removes the tag. Refuse while something made
        # since still uses it (another item's tag link, a card's linked tag, a hobby, a child tag).
        n = conn.execute(
            "SELECT (SELECT COUNT(*) FROM post_tags WHERE tag_id = ?) + (SELECT COUNT(*) FROM projects WHERE tag_id = ?)"
            " + (SELECT COUNT(*) FROM project_hobbies WHERE hobby_tag_id = ?)"
            " + (SELECT COUNT(*) FROM blog_tags WHERE parent_id = ?) AS n",
            (key["id"],) * 4).fetchone()["n"]
        if n:
            raise UndoConflict(f"The tag {current.get('name')!r} is in use by {n} other row(s) now, so its creation "
                               "can't be undone.", {"table": table, "key": key, "in_use": n})
    if target is None:
        if current is not None:
            if table == "capture_events":
                conn.execute("DELETE FROM post_tags WHERE post_slug = ?", (key["slug"],))
            _image_delete_row(conn, table, key)
        return {"table": table, "key": key, "before": current, "after": None}
    if current is None:
        _image_insert_row(conn, table, key, dict(target))
    else:
        _image_update_row(conn, table, key, target)
    return {"table": table, "key": key, "before": current, "after": image_get(conn, table, key)}


def get_change_rows(audit_id=None, batch_id=None):
    """Change-log rows (op set) by audit id or batch id, oldest first, mutations parsed."""
    conn = get_conn()
    try:
        if audit_id is not None:
            rows = conn.execute("SELECT * FROM audit_log WHERE op IS NOT NULL AND id = ?", (audit_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log WHERE op IS NOT NULL AND batch_id = ? ORDER BY id",
                                (batch_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["mutations"] = json.loads(d["mutations"]) if d.get("mutations") else []
            d["affected_slugs"] = json.loads(d["affected_slugs"]) if d.get("affected_slugs") else []
            out.append(d)
        return out
    finally:
        conn.close()


def mark_change_rows_undone(row_ids, undone_by):
    """Stamps audit rows as reversed by `undone_by` (None clears the stamp)."""
    conn = get_conn()
    try:
        conn.executemany("UPDATE audit_log SET undone_by = ? WHERE id = ?", [(undone_by, i) for i in row_ids])
        conn.commit()
    finally:
        conn.close()


def list_tables():
    """Every user table in the DB (sqlite_* internals excluded). The search index (#543: item_search and
    its FTS5 shadow tables) is derived data, emptied by the triggers when its items go, so it is not
    listed -- reset.CLEARED_TABLES / KEPT_TABLES classify real data only."""
    conn = get_conn()
    try:
        return sorted(r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'item_search%'"))
    finally:
        conn.close()


def list_all_item_files():
    """[{slug, stored_filename}] for EVERY item row (redacted and brand assets included)."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT slug, stored_filename FROM capture_events ORDER BY id")]
    finally:
        conn.close()


def _clear_tables(tables):
    """DELETE every row of `tables` (in the order given) on the current connection; returns
    {table: rows_deleted}. Only core/reset.py calls this, inside its transaction."""
    conn = get_conn()
    try:
        counts = {}
        for t in tables:
            counts[t] = conn.execute(f"DELETE FROM {t}").rowcount
        conn.commit()
        return counts
    finally:
        conn.close()


# Readers the reshaping operations (split / merge / move) plan from. Plain SELECTs.

def list_project_item_rows(project_id):
    """Raw project_items rows incl. rowid, in sort_order then rowid order."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT rowid AS rowid, project_id, post_slug, sort_order FROM project_items WHERE project_id = ? "
            "ORDER BY sort_order, rowid", (project_id,))]
    finally:
        conn.close()


def list_hobby_rows(project_id):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT rowid AS rowid, project_id, hobby_tag_id FROM project_hobbies WHERE project_id = ? ORDER BY rowid",
            (project_id,))]
    finally:
        conn.close()


def list_family_rows(card_id, as_member=True):
    """family_members rows where `card_id` is the member (as_member) or the family."""
    conn = get_conn()
    try:
        col = "member_id" if as_member else "family_id"
        return [dict(r) for r in conn.execute(
            f"SELECT rowid AS rowid, family_id, member_id, sort_order, created_at FROM family_members "
            f"WHERE {col} = ? ORDER BY created_at, rowid", (card_id,))]
    finally:
        conn.close()


def list_entry_project_rows(project_id):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT rowid AS rowid, entry_id, project_id, sort_order, note FROM blog_entry_projects "
            "WHERE project_id = ?", (project_id,))]
    finally:
        conn.close()


def list_projects_referencing(card_id):
    """Cards that point at `card_id` through cover_project_id or a card home override."""
    conn = get_conn()
    try:
        return [_card_row(r) for r in conn.execute(
            "SELECT * FROM projects WHERE cover_project_id = ? OR (home_kind = 'card' AND home_ref = ?)",
            (card_id, card_id))]
    finally:
        conn.close()


def list_post_tag_rows(post_slug):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT rowid AS rowid, post_slug, tag_id FROM post_tags WHERE post_slug = ?", (post_slug,))]
    finally:
        conn.close()


def _write_card_row(card_id, fields, op, actor, batch_id=None):
    """Non-card-column fields on a projects row (description, cover_slug, ...), imaged
    with updated_at. Returns True when something changed."""
    with ImageLog(op, actor, batch_id) as log:
        row = log.get("projects", {"id": card_id})
        if row is None:
            return False
        log.slugs.append(row["slug"])
        return log.update("projects", {"id": card_id}, {**fields, "updated_at": time.time()})


def _insert_card_hobby(project_id, tag_id, op, actor, batch_id=None, affected_slugs=None):
    """Adds a hobby membership (idempotent) and images it. True if a row was added."""
    with ImageLog(op, actor, batch_id, affected_slugs) as log:
        if log.get("project_hobbies", {"project_id": project_id, "hobby_tag_id": tag_id}) is not None:
            return False
        log.insert("project_hobbies", {"project_id": project_id, "hobby_tag_id": tag_id}, {})
        return True


def _delete_card_hobby(project_id, tag_id, op, actor, batch_id=None, affected_slugs=None):
    with ImageLog(op, actor, batch_id, affected_slugs) as log:
        return log.delete("project_hobbies", {"project_id": project_id, "hobby_tag_id": tag_id})


# (#541 phase C) write_card_items moved to core/membership.py (`membership.write`), the one
# writer of project_items rows.

def next_item_sort_order(conn, project_id):
    """The sort_order a file appended to the card gets (0 for an empty card)."""
    return conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM project_items WHERE project_id = ?",
                        (project_id,)).fetchone()["n"]


def write_images(op, actor, batch_id, affected_slugs, fn):
    """Runs `fn(log)` with an ImageLog (log.insert / update / delete / get); the images are
    recorded as one change-log row. For merge's many small writes."""
    with ImageLog(op, actor, batch_id, affected_slugs) as log:
        return fn(log)


def blank_document_body(slug):
    """True when `slug` is a document whose body is empty/whitespace (a blank write-up)."""
    row = get_by_slug(slug)
    if row is None or row.get("media_type") != "document":
        return False
    return not ((row.get("type_metadata") or {}).get("body") or "").strip()


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone() is not None


def count_family_members(family_id):
    """Members of a family/collection card. 0 before the family_members table
    exists (it arrives with piece 3)."""
    conn = get_conn()
    try:
        if not _table_exists(conn, "family_members"):
            return 0
        return conn.execute("SELECT COUNT(*) AS n FROM family_members WHERE family_id = ?", (family_id,)).fetchone()["n"]
    finally:
        conn.close()


def _clear_family_members(family_id, op="set_kind", actor=None, batch_id=None, affected_slugs=None):
    """Drops a group card's membership rows (set_kind force=True), logging one
    row image per dropped membership so the drop is undoable. No-op before the
    family_members table exists."""
    conn = get_conn()
    try:
        if _table_exists(conn, "family_members"):
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT member_id, sort_order, created_at FROM family_members WHERE family_id = ?",
                                (family_id,)).fetchall()
            if rows:
                conn.execute("DELETE FROM family_members WHERE family_id = ?", (family_id,))
                insert_change_log(
                    conn, op, actor,
                    [{"table": "family_members", "key": {"family_id": family_id, "member_id": r["member_id"]},
                      "before": {"sort_order": r["sort_order"], "created_at": r["created_at"]}, "after": None}
                     for r in rows],
                    batch_id=batch_id, affected_slugs=affected_slugs)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_family_members(family_id):
    """Members of a family/collection card, in sort_order then insertion order.
    Returns project dicts, each with the membership's sort_order."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.*, fm.sort_order AS member_sort_order FROM family_members fm "
            "JOIN projects p ON p.id = fm.member_id WHERE fm.family_id = ? "
            "ORDER BY fm.sort_order, fm.created_at, fm.rowid",
            (family_id,),
        ).fetchall()
        return [_card_row(r) for r in rows]
    finally:
        conn.close()


def list_families_for_member(member_id):
    """Families/collections a card belongs to (earliest membership first)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.* FROM family_members fm JOIN projects p ON p.id = fm.family_id "
            "WHERE fm.member_id = ? ORDER BY fm.created_at, fm.rowid",
            (member_id,),
        ).fetchall()
        return [_card_row(r) for r in rows]
    finally:
        conn.close()


def is_family_member(family_id, member_id):
    conn = get_conn()
    try:
        return conn.execute("SELECT 1 FROM family_members WHERE family_id = ? AND member_id = ?",
                            (family_id, member_id)).fetchone() is not None
    finally:
        conn.close()


def _add_family_member(family_id, member_id, op, actor, batch_id=None, affected_slugs=None):
    """Idempotently adds a membership row and logs its row image in the same
    transaction. Returns True when a row was inserted, False when it already
    existed (nothing logged). Validation is the caller's (core/cards.py)."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM family_members WHERE family_id = ? AND member_id = ?",
                        (family_id, member_id)).fetchone():
            conn.rollback()
            return False
        order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM family_members WHERE family_id = ?",
                             (family_id,)).fetchone()["n"]
        now = time.time()
        conn.execute("INSERT INTO family_members (family_id, member_id, sort_order, created_at) VALUES (?, ?, ?, ?)",
                     (family_id, member_id, order, now))
        insert_change_log(
            conn, op, actor,
            [{"table": "family_members", "key": {"family_id": family_id, "member_id": member_id},
              "before": None, "after": {"sort_order": order, "created_at": now}}],
            batch_id=batch_id, affected_slugs=affected_slugs)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _remove_family_member(family_id, member_id, op, actor, batch_id=None, affected_slugs=None):
    """Removes a membership row (logging its image). True if a row was removed."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT sort_order, created_at FROM family_members WHERE family_id = ? AND member_id = ?",
                           (family_id, member_id)).fetchone()
        if row is None:
            conn.rollback()
            return False
        conn.execute("DELETE FROM family_members WHERE family_id = ? AND member_id = ?", (family_id, member_id))
        insert_change_log(
            conn, op, actor,
            [{"table": "family_members", "key": {"family_id": family_id, "member_id": member_id},
              "before": {"sort_order": row["sort_order"], "created_at": row["created_at"]}, "after": None}],
            batch_id=batch_id, affected_slugs=affected_slugs)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns a card operation may write on `projects` (anything else is refused, so
# a typo in core/cards.py can't write an arbitrary column).
CARD_WRITABLE_COLUMNS = ("kind", "activity", "stage", "stop_reason", "parent_id",
                         "whereabouts", "whereabouts_note", "provenance", "provenance_credit", "highlight",
                         "home_kind", "home_ref")


# Free-text card columns that offer autocomplete suggestions (#523). Allow-list:
# the column name is interpolated into SQL, so it must come from here, never from input.
SUGGESTABLE_CARD_FIELDS = ("provenance_credit", "whereabouts_note")


def distinct_card_values(field, limit=50):
    """Distinct non-empty values of a free-text card column, most-used first
    (ties alphabetical), capped at `limit`. Raises ValueError for any field not
    in SUGGESTABLE_CARD_FIELDS."""
    if field not in SUGGESTABLE_CARD_FIELDS:
        raise ValueError(f"field not suggestable: {field!r}")
    limit = max(1, min(int(limit), 200))
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT TRIM({field}) AS v, COUNT(*) AS n FROM projects "
            f"WHERE {field} IS NOT NULL AND TRIM({field}) != '' "
            f"GROUP BY TRIM({field}) ORDER BY n DESC, v COLLATE NOCASE ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _update_card_columns(card_id, fields, op, actor, batch_id=None, bump_updated=True):
    """Atomically writes `fields` (a dict limited to CARD_WRITABLE_COLUMNS) onto
    one projects row and records a change-log row image in the same transaction.
    Returns (before, after): dicts of just the columns passed. No-ops (nothing
    changed) write nothing and log nothing."""
    bad = set(fields) - set(CARD_WRITABLE_COLUMNS)
    if bad:
        raise ValueError(f"Not a writable card column: {sorted(bad)}")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None, None
        before = {c: row[c] for c in fields}
        if before == dict(fields):
            conn.rollback()
            return before, dict(fields)
        sets = ", ".join(f"{c} = ?" for c in fields)
        args = list(fields.values())
        img_before, img_after = dict(before), dict(fields)
        if bump_updated:
            now = time.time()
            sets += ", updated_at = ?"
            args.append(now)
            # updated_at rides in the row image so undo can restore it exactly.
            img_before["updated_at"], img_after["updated_at"] = row["updated_at"], now
        conn.execute(f"UPDATE projects SET {sets} WHERE id = ?", args + [card_id])
        insert_change_log(
            conn, op, actor,
            [{"table": "projects", "key": {"id": card_id}, "before": img_before, "after": img_after}],
            batch_id=batch_id, affected_slugs=[row["slug"]],
        )
        conn.commit()
        return before, dict(fields)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def media_type_counts():
    """Return a list of dicts {media_type, count} for all non-redacted items,
    grouped by media_type, ordered by count descending. Used by the storage
    stats panel (#352) to show file type distribution."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT media_type, COUNT(*) as count FROM capture_events WHERE redacted = 0 GROUP BY media_type ORDER BY count DESC"
        ).fetchall()
        return [{"media_type": row["media_type"], "count": row["count"]} for row in rows]
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


# --- Blog entries ---
# A blog entry is a collection of curated projects and objects that tell a story,
# distinct from a project (which is a collection of objects) or a tag (which is an
# automatic grouping).

def get_blog_entry(id_or_slug):
    """Look up by id (int) or slug (str); returns dict or None.

    A STRING is always looked up by slug, even an all-digits one: blog slugs
    come from _slugify(title), so a title like "2" yields the slug "2", and
    the public identifier everywhere (HTTP routes, MCP tools) is the slug.
    Only an actual int is treated as an id (the internal existing["id"]
    callers). Coercing numeric strings to ids made numeric-slug entries
    unreachable by their own slug -> 404 (#318)."""
    conn = get_conn()
    try:
        if isinstance(id_or_slug, int):
            row = conn.execute("SELECT * FROM blog_entries WHERE id = ?", (id_or_slug,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM blog_entries WHERE slug = ?", (id_or_slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_blog_entries(status=None):
    """All entries (or filtered by status), ORDER BY updated_at DESC."""
    conn = get_conn()
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM blog_entries WHERE status = ? ORDER BY updated_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM blog_entries ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_entry_projects(entry_id):
    """The projects attached to an entry, joined with projects so full project
    data comes back, including sort_order and note. Ordered by sort_order ASC."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.*, bep.sort_order, bep.note FROM blog_entry_projects bep "
            "JOIN projects p ON p.id = bep.project_id WHERE bep.entry_id = ? ORDER BY bep.sort_order ASC",
            (entry_id,),
        ).fetchall()
        return [_card_row(r) for r in rows]
    finally:
        conn.close()


def list_entries_for_project(project_id):
    """Blog entries that feature this project (#414) — the reverse of
    list_entry_projects, for project_detail's "Featured in" row. Most-recently-
    updated first."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT be.* FROM blog_entry_projects bep "
            "JOIN blog_entries be ON be.id = bep.entry_id WHERE bep.project_id = ? "
            "ORDER BY be.updated_at DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_entry_items(entry_id):
    """The objects attached to an entry, joined with capture_events so full
    object data comes back, including sort_order and note. Ordered by sort_order ASC."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ce.*, bei.sort_order, bei.note FROM blog_entry_items bei "
            "JOIN capture_events ce ON ce.slug = bei.post_slug WHERE bei.entry_id = ? "
            "ORDER BY bei.sort_order ASC",
            (entry_id,),
        ).fetchall()
        # _row_to_dict parses the capture_events JSON columns (tags,
        # type_metadata); the joined sort_order/note ride along in dict(row).
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# --- Hobbies (#360) ---
# A hobby is a first-class organizing tier ABOVE projects. Mechanism: a hobby
# is just a tag (blog_tags row) marked is_hobby=1 with an optional hobby_status.
# Projects attach to hobbies many-to-many via project_hobbies.


def hobby_codes_by_tag_name():
    """{tag name: group_code} for every hobby that has a code (V2 cards 3.9). Item cards use
    it to show the codes of the hobby tags an item carries."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT name, group_code FROM blog_tags WHERE is_hobby = 1 AND group_code IS NOT NULL").fetchall()
        return {r["name"]: r["group_code"] for r in rows}
    finally:
        conn.close()


def all_group_codes(conn=None, exclude_tag_id=None):
    """Group codes already taken by any hobby."""
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            "SELECT id, group_code FROM blog_tags WHERE is_hobby = 1 AND group_code IS NOT NULL").fetchall()
        return [r["group_code"] for r in rows if r["id"] != exclude_tag_id]
    finally:
        if own:
            conn.close()


# Columns a card operation may write on a hobby's blog_tags row.
HOBBY_WRITABLE_COLUMNS = ("hobby_status", "group_code")


def _update_hobby_columns(tag_id, fields, op, actor, batch_id=None):
    """Atomically writes `fields` (limited to HOBBY_WRITABLE_COLUMNS) onto one hobby's
    blog_tags row and records a change-log row image in the same transaction.
    Returns (before, after) of just the columns passed, or (None, None) when the hobby
    doesn't exist. A no-op writes and logs nothing."""
    bad = set(fields) - set(HOBBY_WRITABLE_COLUMNS)
    if bad:
        raise ValueError(f"Not a writable hobby column: {sorted(bad)}")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM blog_tags WHERE id = ? AND is_hobby = 1", (tag_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None, None
        before = {c: row[c] for c in fields}
        if before == dict(fields):
            conn.rollback()
            return before, dict(fields)
        sets = ", ".join(f"{c} = ?" for c in fields)
        conn.execute(f"UPDATE blog_tags SET {sets} WHERE id = ?", list(fields.values()) + [tag_id])
        insert_change_log(
            conn, op, actor,
            [{"table": "blog_tags", "key": {"id": tag_id}, "before": before, "after": dict(fields)}],
            batch_id=batch_id, affected_slugs=[row["slug"]],
        )
        conn.commit()
        return before, dict(fields)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hobby_flags(tag_id, now=None):
    """Computed, never stored (V2 cards 3.3): mismatches between a hobby's manual
    Active/Inactive switch and the work inside it. Returns a list of
    {code, label, detail, ...}:

    - inactive_with_active_work: the hobby is inactive and a member project has
      activity='active' (`projects` lists up to 5; `detail` names them).
    - active_untouched: the hobby is active and its last-touched date is older than
      card_rules.HOBBY_STALE_DAYS (730). Last touched = the latest real-world end of any
      member project (timeline.resolve_project_span, so edits that bump updated_at can't
      mask staleness; a project with no dated files and no end override is skipped) or
      the effective date of any file tagged with the hobby. A hobby with nothing dated
      is not flagged."""
    hobby = get_tag(tag_id)
    if hobby is None or not hobby.get("is_hobby"):
        return []
    now = now if now is not None else time.time()
    status = hobby.get("hobby_status") or "active"
    projects = list_projects_for_hobby(tag_id)
    flags = []
    if status == "inactive":
        active = [p for p in projects if p.get("activity") == "active"]
        if active:
            titles = [p["title"] for p in active]
            more = len(titles) - 5
            flags.append({
                "code": card_rules.HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK,
                "label": card_rules.HOBBY_FLAG_LABELS[card_rules.HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK],
                "detail": "Active: " + ", ".join(titles[:5]) + (f" (+{more} more)" if more > 0 else ""),
                "projects": [{"slug": p["slug"], "title": p["title"]} for p in active[:5]],
                "count": len(active),
            })
    elif status == "active":
        last = None
        for p in projects:
            items = list_project_items(p["id"])
            end = timeline.resolve_project_span(p, items)[1]  # #725: None when nothing real
            if end is None:
                continue
            last = end if last is None else max(last, end)
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT ce.* FROM capture_events ce JOIN post_tags pt ON pt.post_slug = ce.slug "
                "WHERE pt.tag_id = ? AND ce.redacted = 0", (tag_id,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            d = timeline.resolve_item_date(_row_to_dict(r))
            if d is None:
                continue  # #725: an undated item says nothing about when the hobby was touched
            last = d if last is None else max(last, d)
        if last is not None:
            days = int((now - last) // 86400)
            if days > card_rules.HOBBY_STALE_DAYS:
                when = timeline.epoch_to_local(last).strftime("%b %Y")
                flags.append({
                    "code": card_rules.HOBBY_FLAG_ACTIVE_UNTOUCHED,
                    "label": card_rules.HOBBY_FLAG_LABELS[card_rules.HOBBY_FLAG_ACTIVE_UNTOUCHED],
                    "detail": f"Last touched {when} ({days} days ago).",
                    "last_touched": last,
                    "days_since": days,
                })
    return flags


def list_hobbies():
    """List all hobby tags (is_hobby=1), with a project count each.

    Returns a list of tag dicts with an added 'project_count' key."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT bt.*, COUNT(ph.project_id) AS project_count FROM blog_tags bt "
            "LEFT JOIN project_hobbies ph ON ph.hobby_tag_id = bt.id "
            "WHERE bt.is_hobby = 1 GROUP BY bt.id ORDER BY bt.name",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_hobby(id_or_slug):
    """Look up a hobby by id or slug.

    If id_or_slug is numeric (int or all-digits string), look up by id;
    otherwise look up by slug. Returns the hobby tag dict (is_hobby=1) or None."""
    conn = get_conn()
    try:
        if isinstance(id_or_slug, int) or (isinstance(id_or_slug, str) and id_or_slug.isdigit()):
            row = conn.execute(
                "SELECT * FROM blog_tags WHERE id = ? AND is_hobby = 1", (int(id_or_slug),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM blog_tags WHERE slug = ? AND is_hobby = 1", (id_or_slug,)
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_projects_for_hobby(tag_id):
    """Get all projects attached to a hobby via project_hobbies.

    Returns a list of project dicts."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.* FROM projects p JOIN project_hobbies ph ON ph.project_id = p.id "
            "WHERE ph.hobby_tag_id = ? ORDER BY p.updated_at DESC",
            (tag_id,),
        ).fetchall()
        return [_card_row(r) for r in rows]
    finally:
        conn.close()


def list_loose_hobby_objects(tag_id, include_superseded=False):
    """Objects tagged with a hobby that sit in none of its member projects (#525): the
    hobby page's "loose objects". Every tagged object, not capped like list_posts_for_tag's
    default. Newest first, as list_posts_for_tag returns them."""
    tagged = list_posts_for_tag(tag_id, include_descendants=False, limit=1_000_000)
    conn = get_conn()
    try:
        in_members = {r[0] for r in conn.execute(
            "SELECT pi.post_slug FROM project_items pi JOIN project_hobbies ph ON ph.project_id = pi.project_id "
            "WHERE ph.hobby_tag_id = ?", (tag_id,)).fetchall()}
    finally:
        conn.close()
    hidden = set() if include_superseded else superseded_slugs()  # #477
    return [r for r in tagged if r["slug"] not in in_members and r["slug"] not in hidden]


def list_hobbies_for_project(project_id):
    """Get all hobbies attached to a project via project_hobbies.

    Returns a list of hobby tag dicts."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT bt.* FROM blog_tags bt JOIN project_hobbies ph ON ph.hobby_tag_id = bt.id "
            "WHERE ph.project_id = ? ORDER BY bt.name",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def physical_piece_hobbies():
    """#562: the hobbies whose "shows physical-piece fields" setting is on: [{id, name, slug}]."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT bt.id, bt.name, bt.slug FROM blog_tags bt JOIN hobby_settings hs ON hs.hobby_tag_id = bt.id "
            "WHERE bt.is_hobby = 1 AND hs.shows_physical_piece = 1 ORDER BY bt.id").fetchall()]
    finally:
        conn.close()


def get_hobby_settings(hobby_id):
    """#562/#596/#686/#554: one hobby's settings ({shows_physical_piece, synopsis, flavor, cover_slug,
    cover_project_id}); defaults when the hobby has no settings row yet."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT shows_physical_piece, synopsis, flavor, cover_slug, cover_project_id "
                           "FROM hobby_settings WHERE hobby_tag_id = ?", (hobby_id,)).fetchone()
        return dict(row) if row else {"shows_physical_piece": 0, "synopsis": None, "flavor": None, "cover_slug": None,
                                      "cover_project_id": None}
    finally:
        conn.close()


def hobby_for_old_slug(slug):
    """#554: the hobby that once had URL slug `slug` (hobby_slug_history), or None. Callers try the
    current slug first (db.get_hobby); this is only the redirect fallback."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT bt.* FROM hobby_slug_history h JOIN blog_tags bt ON bt.id = h.hobby_tag_id "
                           "WHERE h.slug = ? AND bt.is_hobby = 1", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def card_ids_with_writeup(slug):
    """#596: ids of the cards whose write-up document is `slug` (normally none or one)."""
    conn = get_conn()
    try:
        return [r[0] for r in conn.execute("SELECT id FROM projects WHERE writeup_slug = ? ORDER BY id", (slug,))]
    finally:
        conn.close()


def first_card_titles():
    """#596: {item slug: title of the card it was first filed into}, for the file face's
    "Stacked · <card>" box. One query for the whole grid; unfiled items are absent."""
    conn = get_conn()
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT pi.post_slug, p.title FROM project_items pi JOIN projects p ON p.id = pi.project_id "
            "WHERE pi.rowid IN (SELECT MIN(rowid) FROM project_items GROUP BY post_slug)")}
    finally:
        conn.close()


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


def get_by_slugs(slugs):
    """Full item rows (the get_by_slug shape) for `slugs`, in the order given; unknown slugs are
    left out. Read in chunks (SQLite's parameter limit)."""
    slugs = list(dict.fromkeys(slugs))
    found = {}
    conn = get_conn()
    try:
        for i in range(0, len(slugs), 500):
            chunk = slugs[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT * FROM capture_events WHERE slug IN ({marks})", chunk):
                found[r["slug"]] = _row_to_dict(r)
    finally:
        conn.close()
    return [found[s] for s in slugs if s in found]


def hobby_shows_physical_piece(hobby_id):
    """#562: True when this hobby's items get the PHYSICAL PIECE fields."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT shows_physical_piece FROM hobby_settings WHERE hobby_tag_id = ?",
                           (hobby_id,)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


# --- Users and sessions (#467 step 1) -------------------------------------------------------
# core/users.py owns every write (the `_` writers below are called only from there). Reads that
# hand rows to a page or a listing never include password_hash: only `get_user_credentials`
# returns it, for the password check itself.

USER_PUBLIC_COLUMNS = "id, username, display_name, role, created_at, last_login_at, disabled"


def count_users():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def count_active_admins(exclude_user_id=None):
    """Admins that are not disabled (optionally leaving one user out: "would this change leave
    an admin?")."""
    conn = get_conn()
    try:
        sql = "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
        args = []
        if exclude_user_id is not None:
            sql += " AND id != ?"
            args.append(exclude_user_id)
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def list_users():
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {USER_PUBLIC_COLUMNS} FROM users ORDER BY username COLLATE NOCASE")]
    finally:
        conn.close()


def get_user(user_id=None, username=None):
    """One user (no password hash) by id or by username (case-insensitive); None when absent."""
    conn = get_conn()
    try:
        if user_id is not None:
            row = conn.execute(f"SELECT {USER_PUBLIC_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
        else:
            row = conn.execute(f"SELECT {USER_PUBLIC_COLUMNS} FROM users WHERE username = ? COLLATE NOCASE",
                               (username or "",)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_credentials(username):
    """{id, username, password_hash, disabled} for the sign-in check only; None when absent."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, username, password_hash, disabled FROM users WHERE username = ? "
                           "COLLATE NOCASE", (username or "",)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_password_hash(user_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_session(token_hash, now, created_after=None):
    """The live session for a token hash joined with its (enabled) user, else None. With
    `created_after`, a session created at or before it is dead too (#651's absolute cap)."""
    sql = ("SELECT s.token_hash, s.user_id, s.csrf_token, s.created_at, s.last_seen, s.expires_at, "
           "u.username, u.display_name, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
           "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0")
    params = [token_hash, now]
    if created_after is not None:
        sql += " AND s.created_at > ?"
        params.append(created_after)
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_sessions(user_id=None):
    conn = get_conn()
    try:
        if user_id is None:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0]
    finally:
        conn.close()


def _insert_user(username, display_name, role, password_hash, now):
    conn = get_conn()
    try:
        cur = conn.execute("INSERT INTO users (username, display_name, role, password_hash, created_at, disabled) "
                           "VALUES (?, ?, ?, ?, ?, 0)", (username, display_name, role, password_hash, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


_USER_WRITABLE = ("display_name", "role", "password_hash", "disabled", "last_login_at")


def _update_user(user_id, **fields):
    bad = set(fields) - set(_USER_WRITABLE)
    if bad:
        raise ValueError(f"not a writable user column: {sorted(bad)}")
    if not fields:
        return
    conn = get_conn()
    try:
        conn.execute(f"UPDATE users SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                     list(fields.values()) + [user_id])
        conn.commit()
    finally:
        conn.close()


def _delete_user(user_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM mcp_tokens WHERE user_id = ?", (user_id,))  # #604 step 5
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# --- #604 step 5: per-user MCP tokens (core/mcp_tokens.py owns every write) -----------------------

MCP_TOKEN_PUBLIC_COLUMNS = "id, user_id, name, created_at, rotated_at, last_used_at"


def list_mcp_tokens(user_id):
    """A user's MCP tokens, newest first: {id, user_id, name, created_at, rotated_at, last_used_at}
    (never the hash)."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {MCP_TOKEN_PUBLIC_COLUMNS} FROM mcp_tokens WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,))]
    finally:
        conn.close()


def list_all_mcp_tokens():
    """(#766) Every user's MCP tokens, for Admin > Users: {id, user_id, name, created_at, rotated_at,
    last_used_at}, newest first per user (never the hash)."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {MCP_TOKEN_PUBLIC_COLUMNS} FROM mcp_tokens ORDER BY user_id, created_at DESC, id DESC")]
    finally:
        conn.close()


def _delete_user_mcp_tokens(user_id):
    """(#766) Deletes every MCP token of one user (core/mcp_tokens.revoke_all); returns how many."""
    conn = get_conn()
    try:
        n = conn.execute("DELETE FROM mcp_tokens WHERE user_id = ?", (user_id,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def get_mcp_token(token_id):
    conn = get_conn()
    try:
        r = conn.execute(f"SELECT {MCP_TOKEN_PUBLIC_COLUMNS} FROM mcp_tokens WHERE id = ?", (token_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def find_mcp_token(token_hash):
    """The token row (no hash) whose sha256 is `token_hash`, plus its user's username / role /
    disabled (None for a deleted user); None when no such token."""
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT t.id, t.user_id, t.name, t.created_at, t.rotated_at, t.last_used_at, u.username, u.role, "
            "u.disabled, u.display_name FROM mcp_tokens t LEFT JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
            (token_hash,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _insert_mcp_token(user_id, name, token_hash, now):
    conn = get_conn()
    try:
        cur = conn.execute("INSERT INTO mcp_tokens (user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                           (user_id, name, token_hash, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _rehash_mcp_token(token_id, token_hash, now):
    conn = get_conn()
    try:
        conn.execute("UPDATE mcp_tokens SET token_hash = ?, rotated_at = ?, last_used_at = NULL WHERE id = ?",
                     (token_hash, now, token_id))
        conn.commit()
    finally:
        conn.close()


def _rename_mcp_token(token_id, name):
    """#778: a token's new name (core/mcp_tokens.rename). Credits read the name at read time."""
    conn = get_conn()
    try:
        conn.execute("UPDATE mcp_tokens SET name = ? WHERE id = ?", (name, token_id))
        conn.commit()
    finally:
        conn.close()


def _delete_mcp_token(token_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM mcp_tokens WHERE id = ?", (token_id,))
        conn.commit()
    finally:
        conn.close()


def _touch_mcp_token(token_id, now):
    conn = get_conn()
    try:
        conn.execute("UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?", (now, token_id))
        conn.commit()
    finally:
        conn.close()


def _insert_session(token_hash, user_id, csrf_token, now, expires_at):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen, expires_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (token_hash, user_id, csrf_token, now, now, expires_at))
        conn.commit()
    finally:
        conn.close()


def _touch_session(token_hash, now, expires_at):
    conn = get_conn()
    try:
        conn.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                     (now, expires_at, token_hash))
        conn.commit()
    finally:
        conn.close()


def _delete_session(token_hash):
    conn = get_conn()
    try:
        n = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _delete_user_sessions(user_id, keep_token_hash=None):
    conn = get_conn()
    try:
        if keep_token_hash:
            n = conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                             (user_id, keep_token_hash)).rowcount
        else:
            n = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _purge_expired_sessions(now, created_before=None):
    """Deletes sessions past expires_at, and (with `created_before`) those created at or before
    it (#651's absolute cap)."""
    conn = get_conn()
    try:
        if created_before is None:
            n = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount
        else:
            n = conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR created_at <= ?",
                             (now, created_before)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()



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
