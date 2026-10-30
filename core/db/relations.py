"""Item-to-item relations and card-to-card relations (#408).

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import time

from .conn import _ambient, get_conn, _row_to_dict, _table_exists
from .changelog import image_get, insert_change_log
from .tags import _attach_tags, list_tags_for_post
from .projects import _add_item_to_project, _card_row, list_projects_for_post


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
