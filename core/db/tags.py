"""The tag tree (blog_tags / post_tags) and the tag-chip cache derived from it (#555).

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import re

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import policy
from .conn import get_conn, _row_to_dict


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
