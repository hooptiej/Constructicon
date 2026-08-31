"""SQLite index for the image repo. One row per upload."""

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
    content_date REAL
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
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_items (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    post_slug TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, post_slug)
);
CREATE INDEX IF NOT EXISTS idx_project_items_slug ON project_items(post_slug);
"""

SPECIAL_CLIENTS = ["Unknown", "Not Business", "Internal Infrastructure"]

# --- Source (capture_events.tech) ---
# `tech` used to record which technician uploaded a screenshot, back when
# imagerepo was a real multi-tech tool gated behind auth. Auth is gone
# (Phase 1) and this is a single-owner site now, so the column has been
# repurposed as a "Source" label: who or what actually added the row, and
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
    # media_type is a loose classifier ('image' | 'video' | 'youtube' | 'document' | 'any', or
    # anything else a caller wants) — deliberately no CHECK constraint. Existing rows predate
    # this column and are all screenshots, so they default to 'image' below.
    if "media_type" not in existing_columns:
        conn.execute("ALTER TABLE capture_events ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image'")
    for column, ddl_type in (("external_url", "TEXT"), ("content_description", "TEXT"), ("content_date", "REAL")):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {column} {ddl_type}")
    existing_client_columns = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    if "nickname" not in existing_client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN nickname TEXT")
    conn.commit()
    conn.close()


def _row_to_dict(row):
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    return d


def insert_upload(slug, filename, stored_filename, uploaded_by, description="", tags=None, ticket_id=None, client=None,
                   source="screenshot", file_size=None, source_modified_at=None, ocr_status=None,
                   media_type="image", external_url=None, content_description=None, content_date=None):
    """Creates a capture_events row. filename/stored_filename are for uploaded files and can be
    None for content that lives elsewhere (media_type='youtube' + external_url, for example) —
    there's no requirement that a row correspond to an actual file on disk.

    media_type/external_url/content_description/content_date describe the content itself,
    separate from source/description which are about how/why the row was captured. See the
    capture_events column comments in SCHEMA for the distinction.
    """
    conn = get_conn()
    now = time.time()
    conn.execute(
        "INSERT INTO capture_events (slug, source, client, ticket_id, timestamp, tech, description, "
        "extracted_text, artifact_link, tags, filename, stored_filename, file_size, source_modified_at, ocr_status, ocr_started_at, "
        "media_type, external_url, content_description, content_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, source, client, ticket_id, now, uploaded_by, description,
         f"/f/{slug}", json.dumps(tags or []), filename, stored_filename, file_size, source_modified_at, ocr_status,
         now if ocr_status == "pending" else None,
         media_type, external_url, content_description, content_date),
    )
    conn.commit()
    conn.close()


def insert_content(slug, uploaded_by, media_type, external_url=None, content_description=None, content_date=None,
                    description="", tags=None, ticket_id=None, client=None, source="external"):
    """Thin wrapper around insert_upload for rows with no uploaded file — e.g. a YouTube video,
    where the content lives at external_url rather than in local storage. filename/stored_filename
    are left None and OCR-related fields don't apply."""
    insert_upload(
        slug, None, None, uploaded_by, description=description, tags=tags, ticket_id=ticket_id, client=client,
        source=source, media_type=media_type, external_url=external_url,
        content_description=content_description, content_date=content_date,
    )


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


def update_tags(slug, description=None, tags=None, ticket_id=None, client=None):
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    conn.execute(
        "UPDATE capture_events SET description = ?, tags = ?, ticket_id = ?, client = ? WHERE slug = ?",
        (
            description if description is not None else existing["description"],
            json.dumps(tags) if tags is not None else json.dumps(existing["tags"]),
            ticket_id if ticket_id is not None else existing["ticket_id"],
            client if client is not None else existing["client"],
            slug,
        ),
    )
    conn.commit()
    conn.close()
    return get_by_slug(slug)


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
    for deleting the actual file(s) from storage first."""
    conn = get_conn()
    conn.execute("DELETE FROM capture_events WHERE slug = ?", (slug,))
    conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? OR slug_b = ?", (slug, slug))
    conn.commit()
    conn.close()


def add_relation(slug_a, slug_b):
    """Symmetric — stored both directions so listing either side's related
    items is a single indexed lookup, not an OR query."""
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

def create_project(title, description="", cover_slug=None, status="active"):
    """Auto-generates a unique slug from title, same dedup-with-numeric-
    suffix pattern as get_or_create_tag."""
    conn = get_conn()
    slug = _slugify(title)
    base_slug = slug
    n = 2
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{n}"
        n += 1
    now = time.time()
    cur = conn.execute(
        "INSERT INTO projects (slug, title, description, cover_slug, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (slug, title, description, cover_slug, status, now, now),
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
