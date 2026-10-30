"""The schema: SCHEMA, init_db and its column adds, the every-boot steps (#667, #704, #760),
and the table-level helpers.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import re
import sqlite3
import time
import uuid

from .conn import get_conn, transaction
from .changelog import insert_change_log
from .tags import _chip_triggers, _ensure_chip_triggers
from .search_index import _ensure_search_schema, _search_sync_triggers
from .migrations import pending_migrations, run_pending_migrations


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
        from .. import provenance_options
        conn.execute(provenance_options.DDL)
        # Editable brand roles + wallpaper tags (#556): schema here, seed = `vocab_seed_556` below.
        from .. import vocab
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
    from .. import changes
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
    from .. import changes
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
