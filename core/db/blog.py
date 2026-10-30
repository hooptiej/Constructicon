"""Blog entries.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""


from .conn import get_conn, _row_to_dict
from .projects import _card_row


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
