"""Tag service (#541 phase C, #555): the ONE place tags are written.

Same contract as core/cards.py and core/items.py: validate first, one `db.transaction()`, every
row written through `db.ImageLog` (row images in the change log), a `Result`, `dry_run`, and
`actor` (None = the request's actor context). Undo is the generic `cards.undo`.

**One store (#555, owner decisions 2026-10-18): the tag tree** (`blog_tags` + `post_tags`). What an
item's TAGS box shows, its CHIPS, is derived from the tree: the names of its tags in the order they
were attached, minus the linked tag (`projects.tag_id`) of any card the item is on. A file's card
membership shows as the card (a link), never as a chip, so a card rename can't leave a stale chip.
`capture_events.tags` is only a cache of that list, kept by triggers in core/db.py ("Tag chips");
nothing here reads it as truth or writes it.

Item ops:
  create(name, parent_name=None)   a blog_tags row (and a root parent by name). Returns the
                                   existing tag when there is one (no write). MCP create_tag.
  attach(slug, names)              adds tags by name (missing ROOT tags are created; an existing tag
                                   of that name in any case is reused). MCP item_tags attach.
  detach(slug, tag_id)             takes one tag off an item. MCP item_tags detach, the item chip's x.
  set_item_tags(slug, names)       full replace of the item's CHIPS: names that aren't there are
                                   attached, chips not named are detached. Tags that aren't chips (a
                                   card's linked tag, a tag hidden from the actor) are left alone.
                                   The item Save does this through items.update(tags=...), so a Save
                                   is still ONE change-log row.
  merge_item_tags(slugs, names)    bulk attach-tags: the names onto every item. One batch.
  attach_on_upload(slug, names)    an upload's tags (pipeline; the uploader just made the row).
  attach_matched(slug, tag_ids)    the upload-time automatch tags (core/automatch.py).

Tag management (#555 item 3; Admin > Tags, MCP constructicon_tags, the item chip's rename):
  rename(tag, name)                a hobby's tag goes through hobbies.rename (its address history);
                                   a card's linked tag is refused (it follows the card's title;
                                   renaming it to that title is allowed, to mend old drift).
  merge(source, into)              every item, and every sub-tag, of `source` moves to `into`, then
                                   `source` is deleted. A card's or hobby's tag can't be the source.
  move(tag, parent)                re-parents (None = the top level). No cycles; a hobby stays at the
                                   top level.
  delete(tag)                      removes the tag and takes it off its items. Refused for a card's
                                   or hobby's tag and for a tag with sub-tags.
  sweep() / sweep_apply(ids)       the cleanup list (#555 item 4): orphan, empty and near-duplicate
                                   tags. sweep only LISTS; sweep_apply deletes exactly the tags the
                                   owner picked, as one batch, and only ones that are still on the
                                   list with no item under them.
  follow_card_title(log, card, title)  a card rename renames its linked tag in the same batch.

Every refusal says what was tried, what happened (nothing was changed) and what to do instead.
A tag hidden from the actor (policy.hidden_tag_ids: a private card's or hobby's) is not_found here.
Lookups never create: `find` / `find_root` / `find_any` / `get_by_name` are reads.
"""

import json
import re

from . import changes, db
from .cards import Result
from .errors import AppError, Conflict, InvalidInput, NotFound

OP_CREATE = "tag_create"
OP_ATTACH = "tag_attach"
OP_DETACH = "tag_detach"
OP_SET = "item_tags_set"
OP_MERGE = "item_tags_merge"
OP_UPLOAD = "upload_tags"
OP_AUTOMATCH = "automatch_tags"
OP_RENAME = "tag_rename"
OP_MERGE_TAGS = "tag_merge"
OP_MOVE = "tag_move"
OP_DELETE = "tag_delete"
OP_SWEEP = "tag_sweep_delete"
OP_MIGRATE = "migration_tags_one_store_555"

NAME_MAX = 200
SWEEP_KINDS = ("orphan", "empty", "near_duplicate")


class TagError(AppError):
    """A refused tag operation: 409 for a conflict with what is there, else 400."""

    def __init__(self, code, message, details=None, status=409):
        super().__init__(code, message, status=status, details=details)


# --- reads (never create) ---------------------------------------------------------------

def find(name, parent_id=None):
    """The tag a name refers to under `parent_id`, the same lookup db._get_or_create_tag did
    before creating: exact (name, parent) first; for a root lookup, a same-named tag anywhere
    in the tree (#213: never mint a duplicate root for a child's name). None when absent."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM blog_tags WHERE name = ? AND parent_id IS ?", (name, parent_id)).fetchone()
        if row is None and parent_id is None:
            row = conn.execute("SELECT * FROM blog_tags WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_named(name):
    """#555: the tag a typed name means: `find(name)` (exact), else one with that name in another
    case (so typing "kerbal" reuses "Kerbal" instead of minting a near-duplicate). Never creates."""
    found = find(name, None)
    if found is not None:
        return found
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM blog_tags WHERE lower(name) = lower(?) ORDER BY parent_id IS NOT NULL, id",
                           (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_any(name):
    """The first tag with this name anywhere in the tree (any parent), or None: the MCP's
    by-name lookups (get_posts_for_tag, detach_tag). Never creates."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM blog_tags WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_root(name):
    """Root-level only (#280)."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM blog_tags WHERE name = ? AND parent_id IS NULL", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "tag"


def _hw():
    """#700: the install's word for a hobby, for user-visible text (one, many)."""
    from . import install_config  # lazy: reads install config at call time
    label = install_config.hobby_label()
    return label.one_lc, label.many_lc


def _clean_name(name):
    return " ".join(str(name or "").split())


def _get_item(slug, actor=None):
    """The item row; NotFound when it is missing, #709's 403 refusal when `actor` may not see it."""
    from . import policy  # lazy, as in core/items.py
    if not slug:
        raise NotFound(f"No item {slug!r}.")
    return policy.viewable_rows([slug], actor=actor)[0][0]


def _hidden(actor=None):
    from . import policy  # lazy: policy imports db
    return policy.hidden_tag_ids(actor)


def _all_tags(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM blog_tags")}


def _path(by_id, tag_id):
    parts, seen = [], set()
    while tag_id is not None and tag_id in by_id and tag_id not in seen:
        seen.add(tag_id)
        parts.append(by_id[tag_id]["name"])
        tag_id = by_id[tag_id]["parent_id"]
    return " > ".join(reversed(parts))


def _descendants(by_id, tag_id):
    kids = {}
    for t in by_id.values():
        kids.setdefault(t["parent_id"], []).append(t["id"])
    out, frontier = [], [tag_id]
    while frontier:
        frontier = [c for p in frontier for c in kids.get(p, [])]
        out.extend(frontier)
    return out


def chip_rows(slug, conn=None):
    """[{id, name, slug}] the item's chips (see the module docstring), in attach order."""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT bt.id, bt.name, bt.slug FROM post_tags pt JOIN blog_tags bt ON bt.id = pt.tag_id "
            "WHERE pt.post_slug = ? AND NOT EXISTS (SELECT 1 FROM project_items pi JOIN projects p ON "
            "p.id = pi.project_id WHERE pi.post_slug = ? AND p.tag_id = pt.tag_id) ORDER BY pt.rowid",
            (slug, slug))]
    finally:
        if own:
            conn.close()


def chips(slug, actor=None):
    """The item's chips the actor may see (a private card's or hobby's tag is left out)."""
    hidden = _hidden(actor)
    return [c for c in chip_rows(slug) if c["id"] not in hidden]


def visible_chip_names(names, actor=None):
    """A row's cached chip names without the ones that would name something hidden from `actor`."""
    hidden = _hidden(actor)
    if not hidden or not names:
        return list(names or [])
    conn = db.get_conn()
    try:
        bad = {r["name"] for r in conn.execute(
            f"SELECT name FROM blog_tags WHERE id IN ({','.join('?' for _ in hidden)})", list(hidden))}
    finally:
        conn.close()
    return [n for n in names if n not in bad]


def usage(tag_id, conn=None):
    """What uses a tag: {items (direct), children, cards [{id, slug, title}], hobby (bool)}."""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        row = conn.execute("SELECT is_hobby FROM blog_tags WHERE id = ?", (tag_id,)).fetchone()
        return {
            "items": conn.execute("SELECT COUNT(*) FROM post_tags WHERE tag_id = ?", (tag_id,)).fetchone()[0],
            "children": conn.execute("SELECT COUNT(*) FROM blog_tags WHERE parent_id = ?", (tag_id,)).fetchone()[0],
            "cards": [dict(r) for r in conn.execute(
                "SELECT id, slug, title FROM projects WHERE tag_id = ? ORDER BY id", (tag_id,))],
            "hobby": bool(row and row["is_hobby"]) or bool(conn.execute(
                "SELECT 1 FROM project_hobbies WHERE hobby_tag_id = ? LIMIT 1", (tag_id,)).fetchone()),
        }
    finally:
        if own:
            conn.close()


def get_tag(tag, actor=None):
    """One tag by id (an int or digit string) or by exact name (any case when no exact match).
    NotFound when missing or hidden from the actor; an ambiguous name (several tags with it) is a
    400 that lists them, so the caller can name one by id."""
    hidden = _hidden(actor)
    conn = db.get_conn()
    try:
        by_id = _all_tags(conn)
    finally:
        conn.close()
    if isinstance(tag, dict):
        tag = tag.get("id")
    if isinstance(tag, int) or (isinstance(tag, str) and tag.strip().isdigit()):
        row = by_id.get(int(tag))
        if row is None or row["id"] in hidden:
            raise NotFound(f"No tag {tag!r}.")
        return {**row, "path": _path(by_id, row["id"])}
    name = _clean_name(tag)
    if not name:
        raise InvalidInput("Name a tag (its id or its name).", code="bad_tag")
    exact = [t for t in by_id.values() if t["name"] == name and t["id"] not in hidden]
    matches = exact or [t for t in by_id.values() if t["name"].lower() == name.lower() and t["id"] not in hidden]
    if not matches:
        raise NotFound(f"No tag called {name!r}.")
    if len(matches) > 1:
        raise TagError("ambiguous_tag", f"{len(matches)} tags are called {name!r}; name one by its id.",
                       {"tags": [{"id": t["id"], "path": _path(by_id, t["id"])} for t in matches]}, status=400)
    return {**matches[0], "path": _path(by_id, matches[0]["id"])}


def get_by_name(name, actor=None):
    """Every visible tag whose name is `name` in any case, each with its path and usage. A read."""
    name = _clean_name(name)
    if not name:
        raise InvalidInput("Name a tag.", code="bad_tag")
    hidden = _hidden(actor)
    conn = db.get_conn()
    try:
        by_id = _all_tags(conn)
        out = [{**t, "path": _path(by_id, t["id"]), **usage(t["id"], conn)}
               for t in by_id.values() if t["name"].lower() == name.lower() and t["id"] not in hidden]
    finally:
        conn.close()
    if not out:
        raise NotFound(f"No tag called {name!r}.")
    return sorted(out, key=lambda t: (t["name"] != name, t["path"].lower()))


def tree(actor=None):
    """The tag tree the actor may see (policy.filter_tag_tree), every node with `path`, `items`
    (items tagged directly), `cards` (titles of the visible cards it is the linked tag of) and
    `hobby`. For Admin > Tags and MCP constructicon_tags(action="tree")."""
    from . import policy  # lazy
    conn = db.get_conn()
    try:
        by_id = _all_tags(conn)
        counts = {r[0]: r[1] for r in conn.execute("SELECT tag_id, COUNT(*) FROM post_tags GROUP BY tag_id")}
        card_rows = [dict(r) for r in conn.execute("SELECT id, slug, title, tag_id, private FROM projects WHERE tag_id IS NOT NULL")]
        hobby_ids = {r[0] for r in conn.execute("SELECT hobby_tag_id FROM project_hobbies")}
    finally:
        conn.close()
    visible_cards = {c["id"] for c in policy.filter_visible_cards(card_rows, actor=actor)}
    by_card = {}
    for c in card_rows:
        if c["id"] in visible_cards:
            by_card.setdefault(c["tag_id"], []).append({"id": c["id"], "slug": c["slug"], "title": c["title"]})

    def walk(nodes):
        return [{"id": n["id"], "name": n["name"], "slug": n["slug"], "parent_id": n["parent_id"],
                 "path": _path(by_id, n["id"]), "items": counts.get(n["id"], 0), "cards": by_card.get(n["id"], []),
                 "hobby": bool(by_id[n["id"]].get("is_hobby")) or n["id"] in hobby_ids,
                 "children": walk(n.get("children") or [])} for n in nodes]
    return walk(policy.filter_tag_tree(db.list_tag_tree(), actor=actor))


# --- writers on an open ImageLog (shared with items / membership / cards) ------------------

def ensure(log, name, parent_id=None):
    """The tag `name` under `parent_id`, created (and imaged) when missing. Returns
    (tag dict, created?)."""
    found = find(name, parent_id)
    if found is not None:
        return found, False
    return _insert_tag(log, name, parent_id), True


def ensure_named(log, name):
    """#555: a typed name's tag: an existing one (find_named: exact, else any case), else a new ROOT
    tag, imaged. Returns (tag, created?)."""
    found = find_named(name)
    if found is not None:
        return found, False
    return _insert_tag(log, name, None), True


def _unique_slug(conn, name, exclude_id=None):
    base = slug = _slugify(name)
    n = 2
    while conn.execute("SELECT 1 FROM blog_tags WHERE slug = ? AND id IS NOT ?", (slug, exclude_id)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _insert_tag(log, name, parent_id):
    slug = _unique_slug(log.conn, name)
    tag_id = log.insert_auto("blog_tags", {"name": name, "slug": slug, "parent_id": parent_id})
    return {"id": tag_id, "name": name, "slug": slug, "parent_id": parent_id}


def link(log, slug, tag_id):
    """Attaches one tag to an item (post_tags). True if a row was added."""
    if log.get("post_tags", {"post_slug": slug, "tag_id": tag_id}) is not None:
        return False
    log.insert("post_tags", {"post_slug": slug, "tag_id": tag_id}, {})
    return True


def unlink(log, slug, tag_id):
    return log.delete("post_tags", {"post_slug": slug, "tag_id": tag_id})


def _current_tag_ids(log, slug):
    return {r["tag_id"] for r in log.conn.execute("SELECT tag_id FROM post_tags WHERE post_slug = ?", (slug,))}


def attach_names(log, slug, names):
    """Attaches tags by name to one item on an open log (ensure_named). Returns (attached ids, created)."""
    attached, created = [], []
    for name in names:
        tag, made = ensure_named(log, name)
        if made:
            created.append(tag)
        if link(log, slug, tag["id"]):
            attached.append(tag["id"])
    return attached, created


def write_chips(log, slug, names, actor=None):
    """Full replace of one item's chips on an open log (see set_item_tags). Returns the tags it
    created."""
    wanted = []
    for n in names or []:
        n = _clean_name(n)
        if n and n not in wanted:
            wanted.append(n)
    hidden = _hidden(actor)
    current = [c for c in chip_rows(slug, log.conn) if c["id"] not in hidden]
    have_ids = _current_tag_ids(log, slug)
    wanted_lc = {n.lower() for n in wanted}
    for c in current:
        if c["name"].lower() not in wanted_lc:
            unlink(log, slug, c["id"])
    current_lc = {c["name"].lower() for c in current}
    created = []
    for name in wanted:
        if name.lower() in current_lc:
            continue
        tag, made = ensure_named(log, name)
        if made:
            created.append(tag)
        if tag["id"] not in have_ids:  # already on it (e.g. a card's linked tag): nothing to add
            link(log, slug, tag["id"])
            have_ids.add(tag["id"])
    return created


def follow_card_title(log, card, title):
    """#555: a card rename renames its linked tag on the caller's log (same change-log batch), so
    the tag is never wrong anywhere it shows. Returns a warning (str) when the tag keeps its old name,
    and why; None when it was renamed or there was nothing to do."""
    new = _clean_name(title)
    tag_id = card.get("tag_id")
    if not tag_id or not new:
        return None
    tag = log.get("blog_tags", {"id": tag_id})
    if tag is None or tag["name"] == new:
        return None
    use = usage(tag_id, log.conn)
    keep = f"The card's linked tag kept its old name '{tag['name']}'"
    if use["hobby"]:
        one = _hw()[0]
        return f"{keep}: it is also a {one}'s tag. Rename the {one} on its own page if you want them to match."
    others = [c["title"] for c in use["cards"] if c["id"] != card["id"]]
    if others:
        return f"{keep}: the card {others[0]!r} links the same tag. Rename that card too, or give one of them its own tag."
    clash = log.conn.execute("SELECT id, name FROM blog_tags WHERE lower(name) = lower(?) AND id != ?",
                             (new, tag_id)).fetchone()
    if clash is not None:
        return (f"{keep}: another tag is already called '{clash['name']}'. Merge the two in Admin > Tags "
                "if they mean the same thing.")
    log.update("blog_tags", {"id": tag_id}, {"name": new, "slug": _unique_slug(log.conn, new, tag_id)})
    return None


def _flat(op, muts):
    from .items import _flat as items_flat  # same change-summary shape as the item service
    return items_flat(op, muts)


def _names(names):
    if isinstance(names, str):
        names = [names]
    out = []
    for n in names or []:
        n = _clean_name(n)
        if n and n not in out:
            out.append(n)
    return out


def _check_name(name):
    new = _clean_name(name)
    if not new:
        raise InvalidInput("A tag name can't be empty.", code="bad_tag")
    if len(new) > NAME_MAX:
        raise InvalidInput(f"That tag name is too long ({NAME_MAX} characters at most).", code="bad_tag")
    return new


# --- item ops ----------------------------------------------------------------------------

def create(name, parent_name=None, *, dry_run=False, actor=None, batch_id=None):
    """Creates a tag (and its root parent by name, when given and missing). Returns the existing
    tag unchanged when it already exists. data: {tag, created}."""
    name = (name or "").strip()
    if not name:
        raise InvalidInput("Tag name can't be empty", code="bad_tag")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_CREATE, actor, batch_id) as log:
            parent_id = None
            if parent_name and parent_name.strip():
                parent, _ = ensure(log, parent_name.strip(), None)
                parent_id = parent["id"]
            tag, created = ensure(log, name, parent_id)
            muts = list(log.muts)
    return Result(True, _flat(OP_CREATE, muts), [], batch_id, dry_run, {"tag": tag, "created": created})


def attach(slug, names, *, dry_run=False, actor=None, batch_id=None):
    """Attaches tags (by name; missing root tags are created) to one item.
    data: {slug, attached: [tag ids], created: [tags]}."""
    names = _names(names)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        _get_item(slug, actor)
        with db.ImageLog(OP_ATTACH, actor, batch_id, [slug]) as log:
            attached, created = attach_names(log, slug, names)
            muts = list(log.muts)
    return Result(True, _flat(OP_ATTACH, muts), [], batch_id, dry_run,
                  {"slug": slug, "attached": attached, "created": created})


def detach(slug, tag_id, *, dry_run=False, actor=None, batch_id=None):
    """Removes one tag from an item (a no-op if it isn't attached). A card's linked tag can't be
    taken off a file that is on that card (it is the card's own plumbing): take the file off the
    card instead. data: {slug, detached}."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        _get_item(slug, actor)
        tag = db.get_tag(tag_id)
        if tag is None or tag["id"] in _hidden(actor):
            raise NotFound(f"No tag {tag_id!r}.")
        conn = db.get_conn()
        holding = conn.execute(
            "SELECT p.title FROM project_items pi JOIN projects p ON p.id = pi.project_id "
            "WHERE pi.post_slug = ? AND p.tag_id = ?", (slug, tag["id"])).fetchone()
        if holding is not None:
            raise TagError("tag_is_card_tag",
                           f"Nothing was changed: '{tag['name']}' is the linked tag of the card "
                           f"'{holding['title']}', which this file is on. Take the file off that card instead.",
                           {"tag_id": tag["id"], "card": holding["title"]})
        with db.ImageLog(OP_DETACH, actor, batch_id, [slug]) as log:
            removed = unlink(log, slug, tag["id"])
            muts = list(log.muts)
    return Result(True, _flat(OP_DETACH, muts), [], batch_id, dry_run, {"slug": slug, "detached": removed})


def set_item_tags(slug, names, *, dry_run=False, actor=None, batch_id=None):
    """Full-replace save of one item's chips (in the tree). The item Save goes through
    items.update(tags=...) instead, so the whole Save is one change-log row."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        _get_item(slug, actor)
        with db.ImageLog(OP_SET, actor, batch_id, [slug]) as log:
            created = write_chips(log, slug, list(names or []), actor)
            muts = list(log.muts)
    return Result(True, _flat(OP_SET, muts), [], batch_id, dry_run, {"slug": slug, "created": created})


def merge_item_tags(slugs, names, *, dry_run=False, actor=None, batch_id=None):
    """Bulk attach-tags (#98): the names onto each item's tags. Unknown slugs are skipped. #709: an
    item the actor may not see (policy.viewable_rows) refuses the WHOLE call (403 forbidden, saying
    how many and why) before anything is written. ONE batch and one change-log row for the call.
    data: {count, slugs, created}."""
    from . import policy  # lazy, as in core/items.py
    names = _names(names)
    batch_id = batch_id or changes.new_batch_id()
    done, created = [], []
    if not names:
        return Result(True, [], [], batch_id, dry_run, {"count": 0, "slugs": [], "created": []})
    with db.transaction(dry_run=dry_run):
        rows, _ = policy.viewable_rows(slugs, missing_ok=True, actor=actor)
        with db.ImageLog(OP_MERGE, actor, batch_id) as log:
            for row in rows:
                slug = row["slug"]
                created += attach_names(log, slug, names)[1]
                done.append(slug)
                log.slugs.append(slug)
            muts = list(log.muts)
    return Result(True, _flat(OP_MERGE, muts), [], batch_id, dry_run,
                  {"count": len(done), "slugs": done, "created": created})


def attach_on_upload(slug, names, *, actor=None):
    """An upload's own tags (web upload / content, MCP upload / import / add_content), right after
    db.insert_upload made the row: one imaged row (op upload_tags). No policy check: the uploader
    just created the item. data: {slug, attached, created}."""
    names = _names(names)
    batch_id = changes.new_batch_id()
    if not names:
        return Result(True, [], [], None, False, {"slug": slug, "attached": [], "created": []})
    with db.transaction():
        with db.ImageLog(OP_UPLOAD, actor, batch_id, [slug]) as log:
            attached, created = attach_names(log, slug, names)
            muts = list(log.muts)
    return Result(True, _flat(OP_UPLOAD, muts), [], batch_id, False, {"slug": slug, "attached": attached, "created": created})


def attach_matched(slug, tag_ids, *, actor=None):
    """The tags upload-time automatch found in a file's name (core/automatch.py): existing tags by
    id, one imaged row (op automatch_tags), so the owner can undo a bad match. data: {attached}."""
    batch_id = changes.new_batch_id()
    with db.transaction():
        with db.ImageLog(OP_AUTOMATCH, actor, batch_id, [slug]) as log:
            attached = [tid for tid in dict.fromkeys(tag_ids) if db.get_tag(tid) and link(log, slug, tid)]
            muts = list(log.muts)
    return Result(True, _flat(OP_AUTOMATCH, muts), [], batch_id if muts else None, False, {"attached": attached})


# --- tag management (#555 item 3) ------------------------------------------------------

def _card_titles(use):
    return ", ".join(f"'{c['title']}'" for c in use["cards"][:3]) + (" and others" if len(use["cards"]) > 3 else "")


def _resolve(tag, actor):
    return get_tag(tag, actor)


def rename(tag, name, *, dry_run=False, actor=None, batch_id=None):
    """Renames a tag everywhere it is used (every chip, search, the tree). A hobby's tag is renamed
    by hobbies.rename (keeps its old addresses working). Refused: a card's linked tag (409
    tag_is_card_tag: rename the card, its tag follows; renaming it to the card's own title is
    allowed), a name another tag has in any case (409 tag_name_conflict: merge them instead).
    data: {tag, old_name, items}."""
    row = _resolve(tag, actor)
    new = _check_name(name)
    if new == row["name"]:
        return Result(True, [], ["Nothing changed: the tag already has that name."], None, dry_run,
                      {"tag": row, "old_name": row["name"], "items": usage(row["id"])["items"]})
    use = usage(row["id"])
    if use["hobby"] and row.get("is_hobby"):
        from . import hobbies  # lazy: hobbies imports this module
        res = hobbies.rename(row["id"], new, dry_run=dry_run, actor=actor, batch_id=batch_id)
        res.data = {**res.data, "tag": get_tag(row["id"], actor) if not dry_run else row, "old_name": row["name"],
                    "items": use["items"]}
        return res
    if use["cards"] and not any(new.lower() == (c["title"] or "").strip().lower() for c in use["cards"]):
        raise TagError("tag_is_card_tag",
                       f"Nothing was changed: '{row['name']}' is the linked tag of the card {_card_titles(use)}, and "
                       "it follows the card's title. Rename the card instead (its tag follows).",
                       {"tag_id": row["id"], "cards": use["cards"]})
    conn = db.get_conn()
    try:
        clash = conn.execute("SELECT id, name FROM blog_tags WHERE lower(name) = lower(?) AND id != ?",
                             (new, row["id"])).fetchone()
    finally:
        conn.close()
    if clash is not None:
        raise TagError("tag_name_conflict",
                       f"Nothing was changed: there is already a tag called '{clash['name']}'. If they mean the same "
                       "thing, merge this tag into it instead.", {"tag_id": row["id"], "existing_tag_id": clash["id"]})
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        conn = db.get_conn()
        slugs = [r[0] for r in conn.execute("SELECT post_slug FROM post_tags WHERE tag_id = ?", (row["id"],))]
        with db.ImageLog(OP_RENAME, actor, batch_id, slugs) as log:
            log.update("blog_tags", {"id": row["id"]}, {"name": new, "slug": _unique_slug(log.conn, new, row["id"])})
            muts = list(log.muts)
        after = db.get_tag(row["id"])
    return Result(True, _flat(OP_RENAME, muts), [], batch_id, dry_run,
                  {"tag": after, "old_name": row["name"], "items": len(slugs)})


def _refuse_protected(row, use, verb, extra=""):
    if use["hobby"]:
        raise TagError("tag_is_hobby",
                       f"Nothing was changed: '{row['name']}' is a {_hw()[0]}'s tag, so it can't be {verb}. "
                       f"Unmark the {_hw()[0]} first if you really mean to.{extra}", {"tag_id": row["id"]})
    if use["cards"]:
        raise TagError("tag_is_card_tag",
                       f"Nothing was changed: '{row['name']}' is the linked tag of the card {_card_titles(use)}, so it "
                       f"can't be {verb}. Its cards would lose their tag.{extra}",
                       {"tag_id": row["id"], "cards": use["cards"]})


def merge(source, into, *, dry_run=False, actor=None, batch_id=None):
    """Merges `source` into `into`: every item tagged `source` gets `into` (once), every sub-tag of
    `source` moves under `into`, then `source` is deleted. ONE batch, undoable. Refused: the same tag
    twice (400 bad_request), `into` inside `source` (409 tag_cycle), a card's or hobby's tag as the
    source (409 tag_is_card_tag / tag_is_hobby; merging the other way works).
    data: {source, into, items_moved, already_had, children_moved}."""
    src = _resolve(source, actor)
    dst = _resolve(into, actor)
    if src["id"] == dst["id"]:
        raise InvalidInput("Pick two different tags to merge.", code="bad_request")
    _refuse_protected(src, usage(src["id"]), "merged away", " Or merge the other tag into this one.")
    conn = db.get_conn()
    try:
        by_id = _all_tags(conn)
    finally:
        conn.close()
    if dst["id"] in _descendants(by_id, src["id"]):
        raise TagError("tag_cycle", f"Nothing was changed: '{dst['path']}' is inside '{src['path']}'. Move it out "
                       "first, or merge the other way.", {"source": src["id"], "into": dst["id"]})
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        conn = db.get_conn()
        slugs = [r[0] for r in conn.execute("SELECT post_slug FROM post_tags WHERE tag_id = ? ORDER BY rowid", (src["id"],))]
        kids = [r[0] for r in conn.execute("SELECT id FROM blog_tags WHERE parent_id = ?", (src["id"],))]
        moved = had = 0
        with db.ImageLog(OP_MERGE_TAGS, actor, batch_id, slugs) as log:
            for s in slugs:
                unlink(log, s, src["id"])
                if link(log, s, dst["id"]):
                    moved += 1
                else:
                    had += 1
            for k in kids:
                log.update("blog_tags", {"id": k}, {"parent_id": dst["id"]})
            if log.get("hobby_settings", {"hobby_tag_id": src["id"]}) is not None:
                log.delete("hobby_settings", {"hobby_tag_id": src["id"]})
            log.delete("blog_tags", {"id": src["id"]})
            log.details = {"source": {"id": src["id"], "name": src["name"]}, "into": {"id": dst["id"], "name": dst["name"]}}
            muts = list(log.muts)
    return Result(True, _flat(OP_MERGE_TAGS, muts), [], batch_id, dry_run,
                  {"source": src, "into": dst, "items_moved": moved, "already_had": had, "children_moved": len(kids)})


def move(tag, parent, *, dry_run=False, actor=None, batch_id=None):
    """Re-parents a tag (`parent` None or "" = the top level). Refused: under itself or one of its own
    sub-tags (409 tag_cycle), a hobby's tag anywhere but the top level (409 tag_is_hobby).
    data: {tag, old_parent_id, parent_id}."""
    row = _resolve(tag, actor)
    new_parent = None if parent in (None, "") else _resolve(parent, actor)
    pid = new_parent["id"] if new_parent else None
    if pid == row["parent_id"]:
        return Result(True, [], ["Nothing changed: the tag is already there."], None, dry_run,
                      {"tag": row, "old_parent_id": row["parent_id"], "parent_id": pid})
    if pid is not None:
        conn = db.get_conn()
        try:
            by_id = _all_tags(conn)
        finally:
            conn.close()
        if pid == row["id"] or pid in _descendants(by_id, row["id"]):
            raise TagError("tag_cycle", f"Nothing was changed: '{new_parent['path']}' is '{row['name']}' itself or "
                           "inside it, so the tag can't go there.", {"tag_id": row["id"], "parent_id": pid})
        if usage(row["id"])["hobby"]:
            raise TagError("tag_is_hobby", f"Nothing was changed: '{row['name']}' is a {_hw()[0]}'s tag, and {_hw()[1]} stay "
                           "at the top level.", {"tag_id": row["id"]})
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_MOVE, actor, batch_id) as log:
            log.update("blog_tags", {"id": row["id"]}, {"parent_id": pid})
            muts = list(log.muts)
        after = db.get_tag(row["id"])
    return Result(True, _flat(OP_MOVE, muts), [], batch_id, dry_run,
                  {"tag": after, "old_parent_id": row["parent_id"], "parent_id": pid})


def _delete_rows(log, tag_id):
    slugs = [r[0] for r in log.conn.execute("SELECT post_slug FROM post_tags WHERE tag_id = ? ORDER BY rowid", (tag_id,))]
    for s in slugs:
        unlink(log, s, tag_id)
    if log.get("hobby_settings", {"hobby_tag_id": tag_id}) is not None:
        log.delete("hobby_settings", {"hobby_tag_id": tag_id})
    log.delete("blog_tags", {"id": tag_id})
    log.slugs += slugs
    return slugs


def delete(tag, *, dry_run=False, actor=None, batch_id=None):
    """Deletes a tag and takes it off every item it is on. ONE batch, undoable. Refused: a card's
    linked tag or a hobby's tag (409 tag_is_card_tag / tag_is_hobby), a tag with sub-tags (409
    tag_has_children: move or delete them first). data: {tag, items}."""
    row = _resolve(tag, actor)
    use = usage(row["id"])
    _refuse_protected(row, use, "deleted")
    if use["children"]:
        raise TagError("tag_has_children",
                       f"Nothing was changed: '{row['name']}' has {use['children']} sub-tag(s). Move them somewhere else "
                       "or delete them first.", {"tag_id": row["id"], "children": use["children"]})
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_DELETE, actor, batch_id) as log:
            slugs = _delete_rows(log, row["id"])
            log.details = {"tag": {"id": row["id"], "name": row["name"], "path": row["path"]}, "items": len(slugs)}
            muts = list(log.muts)
    return Result(True, _flat(OP_DELETE, muts), [], batch_id, dry_run, {"tag": row, "items": len(slugs)})


# --- the cleanup sweep (#555 item 4) ---------------------------------------------------

def _dup_key(name):
    """Two names are near-duplicates when they match ignoring case, surrounding punctuation,
    `-` / `_` and runs of spaces ("Ancient projects." and "Ancient Projects")."""
    s = re.sub(r"[-_]+", " ", str(name or "").casefold())
    s = re.sub(r"[^\w\s]+", " ", s)
    return " ".join(s.split())


def sweep(actor=None):
    """The cleanup list, read-only: every tag the actor may see that is
      orphan          on no item, no card, no hobby, and has no sub-tags;
      empty           has sub-tags but no item, card or hobby anywhere under it;
      near_duplicate  its name matches another tag's ignoring case and punctuation.
    Each entry: {id, name, path, parent_id, kinds, items, subtree_items, children, cards, hobby,
    duplicates [{id, name, path, items}], deletable, why_not, suggestion}. `deletable` = what
    sweep_apply would delete (no item under it, not a card's or hobby's tag). Nothing is written."""
    hidden = _hidden(actor)
    conn = db.get_conn()
    try:
        by_id = _all_tags(conn)
        counts = {r[0]: r[1] for r in conn.execute("SELECT tag_id, COUNT(*) FROM post_tags GROUP BY tag_id")}
        card_tags = {}
        for r in conn.execute("SELECT id, slug, title, tag_id FROM projects WHERE tag_id IS NOT NULL"):
            card_tags.setdefault(r["tag_id"], []).append({"id": r["id"], "slug": r["slug"], "title": r["title"]})
        hobby_ids = {r[0] for r in conn.execute("SELECT hobby_tag_id FROM project_hobbies")}
    finally:
        conn.close()
    hobby_ids |= {i for i, t in by_id.items() if t.get("is_hobby")}
    kids = {}
    for t in by_id.values():
        kids.setdefault(t["parent_id"], []).append(t["id"])
    sub_cache = {}

    def subtree(tid):
        if tid not in sub_cache:
            ids = [tid] + _descendants(by_id, tid)
            sub_cache[tid] = {
                "items": sum(counts.get(i, 0) for i in ids),
                "held": any(i in card_tags or i in hobby_ids for i in ids),
            }
        return sub_cache[tid]

    groups = {}
    for t in by_id.values():
        if t["id"] not in hidden:
            groups.setdefault(_dup_key(t["name"]), []).append(t["id"])
    out = []
    for tid, t in sorted(by_id.items(), key=lambda kv: _path(by_id, kv[0]).lower()):
        if tid in hidden:
            continue
        held = tid in card_tags or tid in hobby_ids
        children = len(kids.get(tid, []))
        kinds = []
        st = subtree(tid)
        if not held and not counts.get(tid) and not children:
            kinds.append("orphan")
        elif children and not st["items"] and not st["held"]:
            kinds.append("empty")
        dups = [i for i in groups.get(_dup_key(t["name"]), []) if i != tid]
        if dups:
            kinds.append("near_duplicate")
        if not kinds:
            continue
        why_not = None
        if tid in hobby_ids:
            why_not = f"a {_hw()[0]}'s tag"
        elif tid in card_tags:
            why_not = "the linked tag of a card"
        elif st["items"]:
            why_not = f"on {st['items']} item(s): merge it instead"
        deletable = why_not is None
        dup_rows = [{"id": i, "name": by_id[i]["name"], "path": _path(by_id, i), "items": counts.get(i, 0)} for i in dups]
        if "near_duplicate" in kinds:
            best = max(dup_rows, key=lambda d: (d["items"], -d["id"]))
            suggestion = (f"Looks like '{best['path']}'. Merge it into that one" +
                          (", or delete it (nothing uses it)." if deletable else "."))
        elif "orphan" in kinds:
            suggestion = f"Not on any item, card or {_hw()[0]}, and has no sub-tags. Safe to delete."
        else:
            suggestion = "Only empty sub-tags under it. Pick it and its sub-tags to delete the whole branch."
        out.append({"id": tid, "name": t["name"], "path": _path(by_id, tid), "parent_id": t["parent_id"],
                    "kinds": kinds, "items": counts.get(tid, 0), "subtree_items": st["items"], "children": children,
                    "cards": card_tags.get(tid, []), "hobby": tid in hobby_ids, "duplicates": dup_rows,
                    "deletable": deletable, "why_not": why_not, "suggestion": suggestion})
    return out


def sweep_apply(tag_ids, *, dry_run=False, actor=None, batch_id=None):
    """Deletes exactly the tags the owner picked from the sweep, as ONE batch (undoable). The sweep is
    worked out again first; every picked tag must still be on it and `deletable`, and a picked tag's
    sub-tags must be picked too, else the WHOLE call is refused (409 sweep_refused, details.refused:
    {id: reason}) and nothing is deleted. data: {deleted: [{id, name, path}]}."""
    try:
        ids = list(dict.fromkeys(int(i) for i in (tag_ids or [])))
    except (TypeError, ValueError) as e:
        raise InvalidInput("tag_ids must be a list of tag ids.", code="bad_request") from e
    if not ids:
        raise InvalidInput("Pick at least one tag from the cleanup list.", code="bad_request")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        listed = {e["id"]: e for e in sweep(actor)}
        conn = db.get_conn()
        by_id = _all_tags(conn)
        refused = {}
        for i in ids:
            e = listed.get(i)
            if e is None:
                refused[i] = "not on the cleanup list (it is in use, or doesn't exist)"
            elif not e["deletable"]:
                refused[i] = f"not deletable: {e['why_not']}"
            else:
                missing = [c for c in _descendants(by_id, i) if c not in ids]
                if missing:
                    refused[i] = f"has {len(missing)} sub-tag(s) that weren't picked"
        if refused:
            names = ", ".join(f"'{by_id[i]['name']}'" if i in by_id else str(i) for i in refused)
            raise TagError("sweep_refused", f"Nothing was deleted: {names} can't be deleted from the cleanup list. "
                           "Refresh the list and pick again.", {"refused": {str(k): v for k, v in refused.items()}})
        depth = {i: len(_path(by_id, i).split(" > ")) for i in ids}
        deleted = []
        with db.ImageLog(OP_SWEEP, actor, batch_id) as log:
            for i in sorted(ids, key=lambda x: -depth[x]):
                deleted.append({"id": i, "name": by_id[i]["name"], "path": _path(by_id, i)})
                _delete_rows(log, i)
            log.details = {"deleted": deleted}
            muts = list(log.muts)
    return Result(True, _flat(OP_SWEEP, muts), [], batch_id, dry_run, {"deleted": deleted})


# --- the #555 migration ----------------------------------------------------------------

def merge_free_text_into_tree():
    """The tags_one_store_555 migration's data step, run once by db.MIGRATIONS (web process, inside
    its transaction, as actor `migration`). For every item, each free-text chip (the old
    `capture_events.tags` column) that none of its tree tags carries (in any case) is merged into the
    tree: an existing tag with that name (exact, else any case) is attached, else a new root tag is
    made. **The rule for card names:** a chip that is the title, or the linked tag's name, of a card
    the item is on is NOT turned into a tree tag: it came from card membership (#274 stamped card
    names into the column), and the card itself now shows as the item's card link. Such chips are
    listed per item in the change-log details (`card_name_chips`), so nothing is lost silently.
    ONE imaged change-log row (op migration_tags_one_store_555) with the counts in `details`.
    data: {items, disagreeing, items_changed, links_added, tags_created, skipped_card_names}."""
    conn = db.get_conn()
    items = [(r["slug"], r["tags"]) for r in conn.execute("SELECT slug, tags FROM capture_events ORDER BY id")]
    tree_of = {}
    for r in conn.execute("SELECT pt.post_slug, pt.tag_id, bt.name FROM post_tags pt JOIN blog_tags bt ON bt.id = pt.tag_id"):
        tree_of.setdefault(r["post_slug"], []).append((r["tag_id"], r["name"]))
    cards_of = {}
    for r in conn.execute("SELECT pi.post_slug, p.title, p.tag_id, bt.name AS tag_name FROM project_items pi "
                          "JOIN projects p ON p.id = pi.project_id LEFT JOIN blog_tags bt ON bt.id = p.tag_id"):
        cards_of.setdefault(r["post_slug"], []).append(r)
    stats = {"items": len(items), "disagreeing": 0, "items_changed": 0, "links_added": 0, "tags_created": 0,
             "skipped_card_names": 0, "unreadable": 0}
    card_name_chips, unreadable, added = {}, [], {}
    batch_id = changes.new_batch_id()
    with db.ImageLog(OP_MIGRATE, None, batch_id) as log:
        for slug, raw in items:
            try:
                free = json.loads(raw or "[]")
                if not isinstance(free, list):
                    raise ValueError("not a list")
            except ValueError:
                unreadable.append(slug)  # reported in details; the cache rewrite replaces it
                continue
            names = []
            for n in free:
                n = _clean_name(n)
                if n and n not in names:
                    names.append(n)
            tree = tree_of.get(slug, [])
            card_lc = set()
            linked = set()
            for c in cards_of.get(slug, []):
                card_lc.add((c["title"] or "").strip().lower())
                if c["tag_name"]:
                    card_lc.add(c["tag_name"].strip().lower())
                if c["tag_id"]:
                    linked.add(c["tag_id"])
            derived = {n.lower() for tid, n in tree if tid not in linked}
            if {n.lower() for n in names} != derived:
                stats["disagreeing"] += 1
            tree_lc = {n.lower() for _, n in tree}
            changed = False
            for name in names:
                if name.lower() in tree_lc:
                    if name.lower() in card_lc and name.lower() not in derived:
                        card_name_chips.setdefault(slug, []).append(name)
                    continue
                if name.lower() in card_lc:
                    stats["skipped_card_names"] += 1
                    card_name_chips.setdefault(slug, []).append(name)
                    continue
                tag, made = ensure_named(log, name)
                stats["tags_created"] += int(made)
                if link(log, slug, tag["id"]):
                    stats["links_added"] += 1
                    added.setdefault(slug, []).append(name)
                    changed = True
                tree_lc.add(name.lower())
            if changed:
                stats["items_changed"] += 1
                log.slugs.append(slug)
        stats["unreadable"] = len(unreadable)
        log.details = {**stats, "added": added, "card_name_chips": card_name_chips, "unreadable_slugs": unreadable,
                       "rule": "free-text chips not in the tree were attached (existing tag reused, any case; else a "
                               "new root tag); a chip naming a card the item is on was left to the card"}
        if not log.muts:
            # nothing to image: still leave one record of what the migration found
            db.insert_change_log(log.conn, OP_MIGRATE, None, [], batch_id=batch_id, affected_slugs=[],
                                 details=log.details)
    return Result(True, [], [], batch_id, False, stats)
