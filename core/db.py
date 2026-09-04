"""SQLite index for Constructicon. One row per upload."""

import json
import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "imagerepo.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL DEFAULT 'screenshot',
    client TEXT,
    ticket_id TEXT,
    timestamp REAL NOT NULL,
    tech TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    extracted_text TEXT NOT NULL DEFAULT '',
    artifact_link TEXT,
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
    icon TEXT
);
CREATE INDEX IF NOT EXISTS idx_capture_events_source ON capture_events(source);
CREATE INDEX IF NOT EXISTS idx_capture_events_client ON capture_events(client);
CREATE INDEX IF NOT EXISTS idx_capture_events_ticket ON capture_events(ticket_id);
CREATE TABLE IF NOT EXISTS clients (
    name TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    nickname TEXT
);
CREATE TABLE IF NOT EXISTS client_domains (
    client_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    PRIMARY KEY (client_name, domain)
);
CREATE TABLE IF NOT EXISTS capture_event_relations (
    slug_a TEXT NOT NULL,
    slug_b TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (slug_a, slug_b)
);
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
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    tag_id INTEGER REFERENCES blog_tags(id)
);
CREATE TABLE IF NOT EXISTS project_items (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    post_slug TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, post_slug)
);
CREATE INDEX IF NOT EXISTS idx_project_items_slug ON project_items(post_slug);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

SPECIAL_CLIENTS = ["Unknown", "Not Business", "Internal Infrastructure"]

# --- Source (capture_events.tech) ---
# `tech` used to record which technician uploaded a screenshot in imagerepo
# (the original project), when it was a real multi-tech tool gated behind auth.
# Auth is gone (Phase 1) and this is a single-owner site now, so the column has
# been repurposed as a "Source" label: who or what actually added the row, and
# how. Every value written into `tech` should be one of these four exact
# strings (SOURCE_MIGRATED is a template — fill in `<source>`):
SOURCE_MANUAL_UPLOAD = "Hooptie J (me) — manual upload"
SOURCE_AUTOMATED_UPLOAD = "Hooptie J (me) — automated upload"
SOURCE_AUTHORED = "Claude — authored"  # Not written by anything yet — reserved for a
# future script that generates original content (e.g. a write-up) rather than
# migrating existing content from somewhere else. See SOURCE_MIGRATED for that case.


def source_migrated_from(source):
    """"Claude — migrated from <source>" — for automated migration scripts
    that bring in existing content from elsewhere (e.g. source="hooptiej.github.io"
    for scripts/backfill_from_hooptiej_site.py)."""
    return f"Claude — migrated from {source}"


# Known "who" prefixes a Source string can start with — used to bucket the
# compact gallery views (home page panes, /gallery/user/<x>) under a short
# grouping key instead of showing/URL-encoding the full sentence-length
# Source string in a narrow layout. The full string is still stored as-is in
# `tech` and always shown in full on the object detail page.
SOURCE_GROUPS = ["Hooptie J (me)", "Claude"]


def source_group(tech):
    """Short grouping key for a Source string, e.g. "Hooptie J (me) — manual
    upload" groups under "Hooptie J (me)". Falls back to the value unchanged
    if it doesn't start with a known prefix (covers legacy rows from before
    this repurposing, e.g. the literal old default "hooptiej")."""
    if not tech:
        return tech
    for group in SOURCE_GROUPS:
        if tech == group or tech.startswith(group + " —") or tech.startswith(group + " -"):
            return group
    return tech


def ensure_special_clients():
    conn = get_conn()
    for name in SPECIAL_CLIENTS:
        conn.execute("INSERT OR IGNORE INTO clients (name, category) VALUES (?, 'special')", (name,))
    conn.commit()
    conn.close()


def sync_hudu_clients(companies):
    """Replace the Hudu-sourced client list wholesale — called periodically
    so renamed/archived companies don't linger. Special categories (Unknown,
    Not Business, Internal Infrastructure) are untouched.

    Each item is either a plain name string (no nickname/domains — older
    sync data) or a {"name": ..., "nickname": ..., "domains": [...]} dict,
    so this stays compatible with whatever the upstream Hudu export
    currently produces. "domains" is any domain worth matching in OCR text —
    typically the company's website plus any Cloudflare-managed zones.
    """
    conn = get_conn()
    conn.execute("DELETE FROM client_domains WHERE client_name IN (SELECT name FROM clients WHERE category = 'hudu')")
    conn.execute("DELETE FROM clients WHERE category = 'hudu'")
    rows, domain_rows = [], []
    for c in companies:
        if isinstance(c, str):
            rows.append((c, None))
            continue
        name = c["name"]
        rows.append((name, c.get("nickname") or None))
        for domain in c.get("domains") or []:
            if domain:
                domain_rows.append((name, domain.strip().lower()))
    conn.executemany(
        "INSERT OR IGNORE INTO clients (name, category, nickname) VALUES (?, 'hudu', ?)",
        rows,
    )
    if domain_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO client_domains (client_name, domain) VALUES (?, ?)",
            domain_rows,
        )
    conn.commit()
    conn.close()


def add_test_client(name, nickname=None, domains=None):
    """A fake client for exercising the pipeline (OCR auto-tag matching,
    similarity, dropdowns) without mixing invented data into the real
    Hudu-synced list. Deliberately its own category, not 'hudu' — that
    category gets wiped and rebuilt wholesale on every sync_hudu_clients()
    call, which would silently delete a fake client the next time a real
    sync runs."""
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO clients (name, category, nickname) VALUES (?, 'test', ?)", (name, nickname))
    if domains:
        conn.executemany(
            "INSERT OR IGNORE INTO client_domains (client_name, domain) VALUES (?, ?)",
            [(name, d.strip().lower()) for d in domains if d],
        )
    conn.commit()
    conn.close()


def list_client_domains():
    """(client_name, domain) pairs — a client can have more than one, so this
    is flat rows rather than one-per-client like list_client_aliases."""
    conn = get_conn()
    rows = conn.execute("SELECT client_name, domain FROM client_domains").fetchall()
    conn.close()
    return [(r["client_name"], r["domain"]) for r in rows]


def list_client_aliases():
    """(name, nickname) pairs for real Hudu-synced clients plus any fake
    test clients — used to auto-tag OCR'd text by client, not the UI
    dropdown (list_clients handles that, and keeps test clients visually
    separate)."""
    conn = get_conn()
    rows = conn.execute("SELECT name, nickname FROM clients WHERE category IN ('hudu', 'test')").fetchall()
    conn.close()
    return [(r["name"], r["nickname"]) for r in rows]


def list_clients():
    conn = get_conn()
    specials = conn.execute("SELECT name FROM clients WHERE category = 'special' ORDER BY name").fetchall()
    hudu = conn.execute("SELECT name FROM clients WHERE category = 'hudu' ORDER BY name").fetchall()
    test = conn.execute("SELECT name FROM clients WHERE category = 'test' ORDER BY name").fetchall()
    conn.close()
    return {
        "special": [r["name"] for r in specials],
        "clients": [r["name"] for r in hudu],
        "test": [r["name"] for r in test],
    }


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    # Pre-generic-schema table from before the capture_events rework — sample
    # data only, safe to drop rather than migrate. Confirmed with Jason 2026-08-27.
    conn.execute("DROP TABLE IF EXISTS uploads")
    conn.executescript(SCHEMA)
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(capture_events)")}
    for column, ddl_type in (("file_size", "INTEGER"), ("source_modified_at", "REAL"), ("ocr_status", "TEXT"), ("ocr_started_at", "REAL"), ("perceptual_hash", "TEXT")):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {column} {ddl_type}")
    # media_type is a loose classifier ('image' | 'youtube' | 'document', or anything else a
    # caller wants) — deliberately no CHECK constraint. See core/object_types.py for the
    # registry that gives each value a real spec (thumbnail strategy, OCR eligibility,
    # per-type metadata fields); a media_type with no registered spec just falls back to
    # object_types.DEFAULT_SPEC rather than erroring. Existing rows predate this column and
    # are all screenshots, so they default to 'image' below.
    if "media_type" not in existing_columns:
        conn.execute("ALTER TABLE capture_events ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image'")
    for column, ddl_type in (("external_url", "TEXT"), ("content_description", "TEXT"), ("content_date", "REAL")):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {column} {ddl_type}")
    # type_metadata: a freeform JSON bag for per-object-type properties that
    # don't fit the generic columns above (see core/object_types.py's
    # MetadataField) — e.g. a future PDF's page count, an STL's dimensions.
    # Deliberately one shared column rather than a new ALTER TABLE per type,
    # so registering a new object type never requires a schema migration.
    if "type_metadata" not in existing_columns:
        conn.execute("ALTER TABLE capture_events ADD COLUMN type_metadata TEXT NOT NULL DEFAULT '{}'")
    existing_client_columns = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    if "nickname" not in existing_client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN nickname TEXT")
    # tag_id: links a project to a root-level blog_tags row of the same name
    # (see create_project's tag_id param) so tagging an object with a project
    # also surfaces it through the site's ordinary tag-based browsing (the
    # home page's ?tag=<slug> filter over the Projects column, and any future
    # consumer of list_posts_for_tag). Existing pre-#1 projects (e.g. the ones
    # from scripts/seed_example_projects.py) predate this and simply have
    # tag_id = NULL — they still work everywhere, they just aren't reachable
    # via a tag filter until someone links one up by hand.
    existing_project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "tag_id" not in existing_project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN tag_id INTEGER REFERENCES blog_tags(id)")
    # display_name/icon (#11): an optional per-object override so an object
    # can be given a human-friendly label and a custom emoji independent of
    # its filename and its media_type's generic badge_icon (see
    # core/object_types.py). NULL for every existing row — _to_public/
    # _to_object_detail in web/app.py fall back to the pre-existing
    # filename/content_description/slug and spec.badge_icon behavior when
    # unset, so this is purely additive.
    for column, ddl_type in (("display_name", "TEXT"), ("icon", "TEXT")):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {column} {ddl_type}")
    conn.commit()
    conn.close()


def _row_to_dict(row):
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    d["type_metadata"] = json.loads(d["type_metadata"]) if d.get("type_metadata") else {}
    return d


def insert_upload(slug, filename, stored_filename, uploaded_by, description="", tags=None, client=None,
                   source="screenshot", file_size=None, source_modified_at=None, ocr_status=None,
                   media_type="image", external_url=None, content_description=None, content_date=None,
                   type_metadata=None):
    """Creates a capture_events row. filename/stored_filename are for uploaded files and can be
    None for content that lives elsewhere (media_type='youtube' + external_url, for example) —
    there's no requirement that a row correspond to an actual file on disk.

    media_type/external_url/content_description/content_date describe the content itself,
    separate from source/description which are about how/why the row was captured. See the
    capture_events column comments in SCHEMA for the distinction. type_metadata is a freeform
    dict for whatever per-type properties don't fit those generic columns — see
    core/object_types.py's MetadataField and set_type_metadata below.
    """
    conn = get_conn()
    now = time.time()
    conn.execute(
        "INSERT INTO capture_events (slug, source, client, timestamp, tech, description, "
        "extracted_text, artifact_link, tags, filename, stored_filename, file_size, source_modified_at, ocr_status, ocr_started_at, "
        "media_type, external_url, content_description, content_date, type_metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, source, client, now, uploaded_by, description,
         f"/f/{slug}", json.dumps(tags or []), filename, stored_filename, file_size, source_modified_at, ocr_status,
         now if ocr_status == "pending" else None,
         media_type, external_url, content_description, content_date, json.dumps(type_metadata or {})),
    )
    conn.commit()
    conn.close()


def insert_content(slug, uploaded_by, media_type, external_url=None, content_description=None, content_date=None,
                    description="", tags=None, client=None, source="external", type_metadata=None):
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
        slug, None, None, uploaded_by, description=description, tags=tags, client=client,
        source=source, media_type=media_type, external_url=external_url,
        content_description=content_description, content_date=content_date,
        ocr_status="pending" if spec.ocr_capable else None,
        type_metadata=type_metadata,
    )


def set_type_metadata(slug, metadata):
    """Replaces a row's type_metadata dict wholesale — callers that want to
    merge should read get_by_slug(slug)["type_metadata"] first."""
    conn = get_conn()
    conn.execute("UPDATE capture_events SET type_metadata = ? WHERE slug = ?", (json.dumps(metadata or {}), slug))
    conn.commit()
    conn.close()


def update_content_metadata(slug, content_description=None, type_metadata=None):
    """Partial update for the two content-description-shaped fields that
    neither update_tags (description/tags/client — see
    api_update_image in web/app.py) nor rename_object (display_name/icon,
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
    if type_metadata:
        merged = dict(existing.get("type_metadata") or {})
        merged.update(type_metadata)
    else:
        merged = existing.get("type_metadata") or {}
    conn = get_conn()
    conn.execute(
        "UPDATE capture_events SET content_description = ?, type_metadata = ? WHERE slug = ?",
        (new_content_description, json.dumps(merged), slug),
    )
    conn.commit()
    conn.close()
    return get_by_slug(slug)


def list_pending_ocr():
    """Rows whose OCR never finished — normally just a brief in-flight window,
    but a process restart while a background OCR task was queued or running
    leaves a row stuck here forever unless something re-triggers it."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM capture_events WHERE ocr_status = 'pending'").fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def set_ocr_status(slug, status):
    """Setting status to "pending" also stamps ocr_started_at — lets the
    watchdog measure how long the *current* attempt has been running,
    independent of how old the upload itself is."""
    conn = get_conn()
    if status == "pending":
        conn.execute("UPDATE capture_events SET ocr_status = ?, ocr_started_at = ? WHERE slug = ?", (status, time.time(), slug))
    else:
        conn.execute("UPDATE capture_events SET ocr_status = ? WHERE slug = ?", (status, slug))
    conn.commit()
    conn.close()


def list_stale_pending_ocr(older_than_seconds):
    """Rows stuck at ocr_status='pending' for suspiciously long — the
    watchdog re-fires these rather than assuming they're just queued behind
    a big batch forever."""
    conn = get_conn()
    cutoff = time.time() - older_than_seconds
    rows = conn.execute(
        "SELECT * FROM capture_events WHERE ocr_status = 'pending' AND ocr_started_at < ?", (cutoff,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def set_perceptual_hash(slug, phash):
    conn = get_conn()
    conn.execute("UPDATE capture_events SET perceptual_hash = ? WHERE slug = ?", (phash, slug))
    conn.commit()
    conn.close()


def set_embedding(slug, embedding_bytes):
    conn = get_conn()
    conn.execute("UPDATE capture_events SET embedding = ? WHERE slug = ?", (embedding_bytes, slug))
    conn.commit()
    conn.close()


def list_hash_and_embedding_candidates(exclude_slug):
    """(slug, perceptual_hash, embedding) for every other row that has at
    least one of the two signals — used for live similarity comparison,
    not cached, so there's nothing to invalidate as new rows come in."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, perceptual_hash, embedding FROM capture_events "
        "WHERE slug != ? AND (perceptual_hash IS NOT NULL OR embedding IS NOT NULL)",
        (exclude_slug,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_by_slug(slug):
    conn = get_conn()
    row = conn.execute("SELECT * FROM capture_events WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def find_duplicate(filename, file_size, source_modified_at):
    """A prior capture_event is only treated as a duplicate when filename,
    file size, and the source file's own last-modified time all agree —
    matching just the name or just the size is too easy to collide on by
    coincidence (e.g. two unrelated 'screenshot.png' drops).
    """
    if not filename or file_size is None or source_modified_at is None:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM capture_events WHERE filename = ? AND file_size = ? AND source_modified_at = ? "
        "ORDER BY timestamp ASC LIMIT 1",
        (filename, file_size, source_modified_at),
    ).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_tags(slug, description=None, tags=None, client=None):
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    conn.execute(
        "UPDATE capture_events SET description = ?, tags = ?, client = ? WHERE slug = ?",
        (
            description if description is not None else existing["description"],
            json.dumps(tags) if tags is not None else json.dumps(existing["tags"]),
            client if client is not None else existing["client"],
            slug,
        ),
    )
    conn.commit()
    conn.close()
    return get_by_slug(slug)


def rename_object(slug, display_name=None, icon=None):
    """Sets an object's display_name and/or icon override (#11). Partial
    update, same pattern as update_tags/update_project — pass only the
    field(s) you want to change. Passing an empty string clears the field
    back to the default fallback (filename/content_description/slug for
    display_name, the media_type's spec.badge_icon for icon) rather than
    leaving the previous override in place, since "" is never a meaningful
    display name or icon glyph on its own."""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    conn.execute(
        "UPDATE capture_events SET display_name = ?, icon = ? WHERE slug = ?",
        (
            (display_name or None) if display_name is not None else existing.get("display_name"),
            (icon or None) if icon is not None else existing.get("icon"),
            slug,
        ),
    )
    conn.commit()
    conn.close()
    return get_by_slug(slug)


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
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


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
    if value:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    else:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def add_tags(slug, new_tags):
    """Merge new_tags into the row's existing tags (deduped) instead of
    replacing them — for auto-tagging, so it never clobbers tags a person
    already set by hand."""
    existing = get_by_slug(slug)
    if existing is None:
        return
    merged = existing["tags"] + [t for t in new_tags if t not in existing["tags"]]
    if merged == existing["tags"]:
        return
    conn = get_conn()
    conn.execute("UPDATE capture_events SET tags = ? WHERE slug = ?", (json.dumps(merged), slug))
    conn.commit()
    conn.close()


def set_client_if_empty(slug, client):
    """Only sets client if it's currently unset — auto-detection should
    never override a client a person already picked by hand."""
    conn = get_conn()
    conn.execute(
        "UPDATE capture_events SET client = ? WHERE slug = ? AND (client IS NULL OR client = '')",
        (client, slug),
    )
    conn.commit()
    conn.close()


def list_uploaders(query=None, client=None):
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
    clauses, params = [], []
    if query:
        clauses.append("(description LIKE ? OR filename LIKE ?)")
        params += [f"%{query}%", f"%{query}%"]
    if client:
        clauses.append("client = ?")
        params.append(client)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT tech, timestamp FROM capture_events {where}", params).fetchall()
    conn.close()
    groups = {}
    for r in rows:
        key = source_group(r["tech"])
        g = groups.setdefault(key, {"uploaded_by": key, "total": 0, "most_recent": 0})
        g["total"] += 1
        g["most_recent"] = max(g["most_recent"], r["timestamp"])
    return sorted(groups.values(), key=lambda g: g["most_recent"], reverse=True)


def search(query=None, tags=None, client=None, uploaded_by=None, limit=50):
    conn = get_conn()
    clauses, params = [], []
    if query:
        clauses.append("(description LIKE ? OR filename LIKE ? OR extracted_text LIKE ?)")
        params += [f"%{query}%", f"%{query}%", f"%{query}%"]
    if client:
        clauses.append("client = ?")
        params.append(client)
    if uploaded_by:
        # Matches either the exact Source string or rows whose Source starts
        # with `uploaded_by` as its group prefix (see source_group()) — lets
        # callers filter by either a full Source string or the short grouping
        # key the gallery views link with (e.g. "Hooptie J (me)").
        clauses.append("(tech = ? OR tech LIKE ? OR tech LIKE ?)")
        params += [uploaded_by, f"{uploaded_by} —%", f"{uploaded_by} -%"]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM capture_events {where} ORDER BY timestamp DESC LIMIT ?", params + [limit]
    ).fetchall()
    conn.close()
    results = [_row_to_dict(r) for r in rows]
    if tags:
        wanted = set(tags)
        results = [r for r in results if wanted & set(r["tags"])]
    return results


def set_extracted_text(slug, text):
    conn = get_conn()
    conn.execute("UPDATE capture_events SET extracted_text = ? WHERE slug = ?", (text, slug))
    conn.commit()
    conn.close()


def mark_redacted(slug):
    """File removed (sensitive content), metadata kept for future correlation."""
    conn = get_conn()
    conn.execute("UPDATE capture_events SET redacted = 1 WHERE slug = ?", (slug,))
    conn.commit()
    conn.close()
    return get_by_slug(slug)


def delete_upload(slug):
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
    conn.execute("DELETE FROM capture_events WHERE slug = ?", (slug,))
    conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? OR slug_b = ?", (slug, slug))
    conn.execute("DELETE FROM project_items WHERE post_slug = ?", (slug,))
    conn.execute("DELETE FROM post_tags WHERE post_slug = ?", (slug,))
    conn.commit()
    conn.close()


def add_relation(slug_a, slug_b):
    """Symmetric — stored both directions so listing either side's related
    items is a single indexed lookup, not an OR query.

    #16: a "related to" link used to be purely descriptive — it connected
    two slugs but did nothing about either side's categorization. That let
    a related item carry zero tags and zero project membership, which made
    it invisible everywhere tag/project browsing is the only way in (the
    home page's Projects column, any /?tag= filter, a project's own detail
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
    conn.close()
    _sync_relation_categorization(slug_a, slug_b)


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
        attach_tags(slug_a, list(missing_for_a))
    if missing_for_b:
        attach_tags(slug_b, list(missing_for_b))

    projects_a = {p["id"] for p in list_projects_for_post(slug_a)}
    projects_b = {p["id"] for p in list_projects_for_post(slug_b)}
    for project_id in projects_b - projects_a:
        add_item_to_project(project_id, slug_a)
    for project_id in projects_a - projects_b:
        add_item_to_project(project_id, slug_b)


def remove_relation(slug_a, slug_b):
    conn = get_conn()
    conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? AND slug_b = ?", (slug_a, slug_b))
    conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? AND slug_b = ?", (slug_b, slug_a))
    conn.commit()
    conn.close()


def list_related(slug):
    conn = get_conn()
    rows = conn.execute(
        "SELECT ce.* FROM capture_event_relations r JOIN capture_events ce ON ce.slug = r.slug_b "
        "WHERE r.slug_a = ? ORDER BY ce.timestamp DESC",
        (slug,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


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


def get_or_create_tag(name, parent_id=None):
    """Looked up by (name, parent_id) so the same tag name can exist under
    different parents (e.g. a "Camera" tag under both "FPV" and "3D
    Printing") without colliding — only the slug has to be globally unique,
    and a name collision there just gets a numeric suffix."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM blog_tags WHERE name = ? AND parent_id IS ?", (name, parent_id)
    ).fetchone()
    if row:
        conn.close()
        return dict(row)
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
    conn.close()
    return {"id": tag_id, "name": name, "slug": slug, "parent_id": parent_id}


def attach_tags(post_slug, tag_ids):
    conn = get_conn()
    conn.executemany(
        "INSERT OR IGNORE INTO post_tags (post_slug, tag_id) VALUES (?, ?)",
        [(post_slug, tag_id) for tag_id in tag_ids],
    )
    conn.commit()
    conn.close()


def detach_tag(post_slug, tag_id):
    conn = get_conn()
    conn.execute("DELETE FROM post_tags WHERE post_slug = ? AND tag_id = ?", (post_slug, tag_id))
    conn.commit()
    conn.close()


def list_tags_for_post(post_slug):
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.* FROM blog_tags t JOIN post_tags pt ON pt.tag_id = t.id WHERE pt.post_slug = ? ORDER BY t.name",
        (post_slug,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_tag_tree():
    """Every tag, nested under its parent — the Projects page's table of
    contents renders straight from this. Built in Python rather than a
    recursive CTE since the tree is small and this is far easier to read."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM blog_tags ORDER BY name").fetchall()]
    conn.close()
    by_id = {row["id"]: {**row, "children": []} for row in rows}
    roots = []
    for row in rows:
        node = by_id[row["id"]]
        if row["parent_id"] is not None and row["parent_id"] in by_id:
            by_id[row["parent_id"]]["children"].append(node)
        else:
            roots.append(node)
    return roots


def _descendant_tag_ids(tag_id):
    conn = get_conn()
    rows = conn.execute("SELECT id, parent_id FROM blog_tags").fetchall()
    conn.close()
    children_by_parent = {}
    for r in rows:
        children_by_parent.setdefault(r["parent_id"], []).append(r["id"])
    ids = [tag_id]
    frontier = [tag_id]
    while frontier:
        frontier = [child for parent in frontier for child in children_by_parent.get(parent, [])]
        ids.extend(frontier)
    return ids


def list_posts_for_tag(tag_id, include_descendants=True, limit=50):
    """Posts tagged with `tag_id`, or anywhere under it in the tree when
    include_descendants — e.g. the "Projects" root category page shows
    every post filed under any of its child tags too, not just posts
    tagged with "Projects" directly (which would almost never happen)."""
    tag_ids = _descendant_tag_ids(tag_id) if include_descendants else [tag_id]
    placeholders = ",".join("?" for _ in tag_ids)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT DISTINCT ce.* FROM capture_events ce JOIN post_tags pt ON pt.post_slug = ce.slug "
        f"WHERE pt.tag_id IN ({placeholders}) ORDER BY ce.timestamp DESC LIMIT ?",
        tag_ids + [limit],
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def list_recent_posts(limit=10):
    """Chronological feed — the Blog page uses this with a high limit, Home's
    highlights strip uses it with a small one. Same query either way."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM capture_events WHERE redacted = 0 ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# --- Projects ---
# A curated collection of posts an owner deliberately assembles into one
# card — distinct from blog_tags/post_tags, which is automatic grouping by
# whatever tags a post happens to carry. A project has its own identity
# (title, description, optional cover image) and a hand-ordered set of
# member posts, rather than being derived from tag membership.

def create_project(title, description="", cover_slug=None, status="active", tag_id=None):
    """Auto-generates a unique slug from title, same dedup-with-numeric-
    suffix pattern as get_or_create_tag.

    tag_id optionally links this project to a blog_tags row (see #1 —
    "tied to the site tags") — callers that want a project reachable via the
    home page's tag filter should pass get_or_create_tag(title)["id"]
    themselves rather than this function inventing the tag on its own, since
    not every project needs (or predates) a tag link."""
    conn = get_conn()
    slug = _slugify(title)
    base_slug = slug
    n = 2
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{n}"
        n += 1
    now = time.time()
    cur = conn.execute(
        "INSERT INTO projects (slug, title, description, cover_slug, status, created_at, updated_at, tag_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, title, description, cover_slug, status, now, now, tag_id),
    )
    conn.commit()
    project_id = cur.lastrowid
    conn.close()
    return {
        "id": project_id,
        "slug": slug,
        "title": title,
        "description": description,
        "cover_slug": cover_slug,
        "status": status,
        "created_at": now,
        "updated_at": now,
        "tag_id": tag_id,
    }


def get_project(id_or_slug):
    """Look up by either numeric id or slug — a project detail page will
    likely be reached by slug in a URL, but internal callers (e.g.
    add_item_to_project) often already have the id."""
    conn = get_conn()
    if isinstance(id_or_slug, int) or (isinstance(id_or_slug, str) and id_or_slug.isdigit()):
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(id_or_slug),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM projects WHERE slug = ?", (id_or_slug,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_projects(status=None):
    conn = get_conn()
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_project(id_or_slug, title=None, description=None, cover_slug=None, status=None):
    """Partial update — only overwrites fields that were passed, same
    pattern as update_tags() for capture_events. Bumps updated_at."""
    existing = get_project(id_or_slug)
    if existing is None:
        return None
    conn = get_conn()
    now = time.time()
    conn.execute(
        "UPDATE projects SET title = ?, description = ?, cover_slug = ?, status = ?, updated_at = ? WHERE id = ?",
        (
            title if title is not None else existing["title"],
            description if description is not None else existing["description"],
            cover_slug if cover_slug is not None else existing["cover_slug"],
            status if status is not None else existing["status"],
            now,
            existing["id"],
        ),
    )
    conn.commit()
    conn.close()
    return get_project(existing["id"])


def add_item_to_project(project_id, post_slug, sort_order=None):
    """If sort_order isn't given, appends at the end (max existing
    sort_order + 1, or 0 if the project has no items yet)."""
    conn = get_conn()
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
    conn.close()


def remove_item_from_project(project_id, post_slug):
    conn = get_conn()
    conn.execute(
        "DELETE FROM project_items WHERE project_id = ? AND post_slug = ?", (project_id, post_slug)
    )
    conn.commit()
    conn.close()


def list_project_items(project_id):
    """The posts in a project, ordered by sort_order, joined with
    capture_events so full post data comes back — mirrors how
    list_posts_for_tag joins post_tags to capture_events."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT ce.* FROM project_items pi JOIN capture_events ce ON ce.slug = pi.post_slug "
        "WHERE pi.project_id = ? ORDER BY pi.sort_order ASC",
        (project_id,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def list_projects_for_post(post_slug):
    """Reverse lookup — which projects contain a given post. Uses
    idx_project_items_slug."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT p.* FROM projects p JOIN project_items pi ON pi.project_id = p.id "
        "WHERE pi.post_slug = ? ORDER BY p.updated_at DESC",
        (post_slug,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_unfiled_items(limit=10000):
    """capture_events rows with no project_items row AND no tags — "unfiled"
    uploads (#41). #17 moved the raw upload gallery into the home page's
    hover/drag pop-out and left the main page showing only Project tiles;
    with zero projects created (or an upload simply never tagged to one),
    that made the home page look completely empty even though uploads
    still existed, which was mistaken for real data loss and led directly
    to an accidental production /api/delete-all. home_page renders this
    list as an always-visible "Unfiled" section so that state can never
    look like "everything is gone" again.

    #123: also excludes anything with a non-empty tags array, not just
    project membership. Once #98 added bulk tagging on this very page, an
    item could get real tags applied while never being added to a project
    -- it kept showing as "unfiled" even though a human had clearly already
    organized it. Tagging is still an explicit, deliberate action (nothing
    tags an item automatically on upload), so this doesn't reintroduce the
    original "looks like everything's gone" risk this function exists to
    prevent -- it only clears items a human actually did something to.

    LEFT JOIN + IS NULL rather than NOT IN/NOT EXISTS — reads cleanest
    given project_items' exact shape (a plain (project_id, post_slug)
    membership row per project.py's PRIMARY KEY, no post_slug uniqueness
    across projects), and is symmetric with list_project_items' own
    JOIN-based style just below.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT ce.* FROM capture_events ce LEFT JOIN project_items pi ON pi.post_slug = ce.slug "
        "WHERE pi.post_slug IS NULL AND (ce.tags IS NULL OR ce.tags = '[]') "
        "ORDER BY ce.timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def list_recent_items_by_type(limit_per_type=10):
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
    # Fetch all distinct media_types that have at least one row
    media_type_rows = conn.execute(
        "SELECT DISTINCT media_type FROM capture_events"
    ).fetchall()

    result = {}
    for (media_type,) in media_type_rows:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE media_type = ? ORDER BY timestamp DESC LIMIT ?",
            (media_type, limit_per_type),
        ).fetchall()
        if rows:
            result[media_type] = [_row_to_dict(r) for r in rows]

    conn.close()
    return result
