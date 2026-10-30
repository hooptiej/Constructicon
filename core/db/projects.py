"""Cards (the projects table): create, read, the card tree, files on cards, privacy reads,
unfiled items, family members and the card-column writers.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import time

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import policy, timeline
from .conn import _ambient, get_conn, _row_to_dict, _table_exists
from .labels import SOURCE_AUTHORED
from .changelog import image_get, ImageLog, insert_change_log
from .revisions import _not_superseded
from .tags import _attach_tags, _slugify
from .items import insert_content


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
    from .. import card_rules, changes
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
        from .. import actor as actor_ctx, users  # lazy: users imports this module
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
    from .. import storage
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
    from .. import card_rules
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
