"""Hobbies (#360): hobby rows, flags and settings.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import time

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import card_rules, timeline
from .conn import get_conn, _row_to_dict
from .changelog import insert_change_log
from .revisions import superseded_slugs
from .tags import get_tag, list_posts_for_tag
from .projects import _card_row, list_project_items


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


def hobby_shows_physical_piece(hobby_id):
    """#562: True when this hobby's items get the PHYSICAL PIECE fields."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT shows_physical_piece FROM hobby_settings WHERE hobby_tag_id = ?",
                           (hobby_id,)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()
