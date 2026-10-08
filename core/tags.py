"""Tag service (#541 phase C): the ONE place an item's tags are written.

Same contract as core/cards.py and core/items.py: validate first, one `db.transaction()`, every
row written through `db.ImageLog` (row images in the change log), a `Result`, `dry_run`, and
`actor` (None = the request's actor context). Undo is the generic `cards.undo`.

An item's tags live in TWO stores (unifying them is #555, a later phase; nothing here changes
how they relate):
  - `post_tags` (item <-> blog_tags): the tag tree, browsing and pills read this.
  - the free-text `capture_events.tags` JSON column: the item page's TAGS box.
Each op below writes exactly the store(s) its old raw path wrote, so behaviour is unchanged:

  create(name, parent_name=None)   a blog_tags row (and a root parent by name). Returns the
                                   existing tag when there is one (no write). MCP create_tag.
  attach(slug, names)              post_tags only, creating missing ROOT tags. MCP attach_tags.
  detach(slug, tag_id)             post_tags only. MCP detach_tag.
  set_item_tags(slug, names)       the free-text column (full replace) plus its post_tags sync
                                   (the old db._update_tags + sync_real_tags_for_post): every
                                   typed name gets a root tag (created if missing) attached;
                                   names that were typed before and are gone now are detached;
                                   tags that came another way (a card's linked tag, MCP) stay.
                                   The item Save calls it through items.update(tags=...), so a
                                   Save is still ONE change-log row.
  merge_item_tags(slugs, names)    bulk attach-tags: each item's free-text list becomes
                                   sorted(existing | names), then the same sync. One batch.

Creating a tag is always imaged (blog_tags insert), so undoing the op that created it removes it
again. Undo refuses (undo_conflict) while something made later uses that tag. Lookups never
create: `find` / `find_root` are reads.
"""

import json
import re

from . import changes, db
from .cards import Result
from .errors import InvalidInput, NotFound

OP_CREATE = "tag_create"
OP_ATTACH = "tag_attach"
OP_DETACH = "tag_detach"
OP_SET = "item_tags_set"
OP_MERGE = "item_tags_merge"


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
    """Root-level only (#280): what a free-text name was attached as."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM blog_tags WHERE name = ? AND parent_id IS NULL", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "tag"


def _get_item(slug):
    row = db.get_by_slug(slug) if slug else None
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    return row


# --- writers on an open ImageLog (shared with items / membership) -----------------------

def ensure(log, name, parent_id=None):
    """The tag `name` under `parent_id`, created (and imaged) when missing. Returns
    (tag dict, created?)."""
    found = find(name, parent_id)
    if found is not None:
        return found, False
    base = slug = _slugify(name)
    n = 2
    while log.conn.execute("SELECT 1 FROM blog_tags WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    tag_id = log.insert_auto("blog_tags", {"name": name, "slug": slug, "parent_id": parent_id})
    return {"id": tag_id, "name": name, "slug": slug, "parent_id": parent_id}, True


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


def _free_text(log, slug):
    row = log.conn.execute("SELECT tags FROM capture_events WHERE slug = ?", (slug,)).fetchone()
    return json.loads(row["tags"] or "[]") if row else []


def write_free_text(log, slug, names):
    """The full-replace free-text save plus its post_tags sync (old update_tags semantics)."""
    previous = _free_text(log, slug)
    log.update("capture_events", {"slug": slug}, {"tags": json.dumps(names)})
    desired = set()
    created = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        tag, made = ensure(log, name, None)
        desired.add(tag["id"])
        if made:
            created.append(tag)
    prev_ids = set()
    for name in previous:
        name = name.strip()
        if not name:
            continue
        tag = find_root(name)
        if tag:
            prev_ids.add(tag["id"])
    current = _current_tag_ids(log, slug)
    for tid in sorted(desired - current):
        link(log, slug, tid)
    for tid in sorted(prev_ids - desired):
        unlink(log, slug, tid)
    return created


def merge_free_text_name(log, slug, name):
    """Appends `name` to the item's free-text list if it isn't there (the old db._add_tags for one
    name: a card's linked tag shows as a chip on the item page, #274). True if it changed."""
    existing = _free_text(log, slug)
    if name in existing:
        return False
    return log.update("capture_events", {"slug": slug}, {"tags": json.dumps(existing + [name])})


def remove_free_text_name(log, slug, name):
    """Drops `name` from the item's free-text list if it is there (the reverse of
    merge_free_text_name; #590: a file leaving a card sheds that card's chip). True if it changed."""
    existing = _free_text(log, slug)
    if name not in existing:
        return False
    return log.update("capture_events", {"slug": slug}, {"tags": json.dumps([n for n in existing if n != name])})


def _flat(op, muts):
    from .items import _flat as items_flat  # same change-summary shape as the item service
    return items_flat(op, muts)


def _names(names):
    if isinstance(names, str):
        names = [names]
    return [str(n).strip() for n in (names or []) if str(n).strip()]


# --- ops ---------------------------------------------------------------------------------

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
    """Attaches tags (by name; missing root tags are created) to one item's post_tags.
    data: {slug, attached: [tag ids], created: [tags]}."""
    names = _names(names)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        _get_item(slug)
        with db.ImageLog(OP_ATTACH, actor, batch_id, [slug]) as log:
            attached, created = [], []
            for name in names:
                tag, made = ensure(log, name, None)
                if made:
                    created.append(tag)
                if link(log, slug, tag["id"]):
                    attached.append(tag["id"])
            muts = list(log.muts)
    return Result(True, _flat(OP_ATTACH, muts), [], batch_id, dry_run,
                  {"slug": slug, "attached": attached, "created": created})


def detach(slug, tag_id, *, dry_run=False, actor=None, batch_id=None):
    """Removes one tag from an item's post_tags (a no-op if it isn't attached)."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        _get_item(slug)
        if db.get_tag(tag_id) is None:
            raise NotFound(f"No tag {tag_id!r}.")
        with db.ImageLog(OP_DETACH, actor, batch_id, [slug]) as log:
            removed = unlink(log, slug, tag_id)
            muts = list(log.muts)
    return Result(True, _flat(OP_DETACH, muts), [], batch_id, dry_run, {"slug": slug, "detached": removed})


def set_item_tags(slug, names, *, dry_run=False, actor=None, batch_id=None):
    """Full-replace save of one item's free-text tags (with the post_tags sync). The item Save
    goes through items.update(tags=...) instead, so the whole Save is one change-log row."""
    names = list(names or [])
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        _get_item(slug)
        with db.ImageLog(OP_SET, actor, batch_id, [slug]) as log:
            created = write_free_text(log, slug, names)
            muts = list(log.muts)
    return Result(True, _flat(OP_SET, muts), [], batch_id, dry_run, {"slug": slug, "created": created})


def merge_item_tags(slugs, names, *, dry_run=False, actor=None, batch_id=None):
    """Bulk attach-tags (#98): unions `names` onto each item's free-text tags (sorted, as before)
    and syncs post_tags. Unknown slugs are skipped. ONE batch and one change-log row for the
    call. data: {count, slugs, created}."""
    names = _names(names)
    batch_id = batch_id or changes.new_batch_id()
    done, created = [], []
    if not names:
        return Result(True, [], [], batch_id, dry_run, {"count": 0, "slugs": [], "created": []})
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_MERGE, actor, batch_id) as log:
            for slug in dict.fromkeys(slugs or []):
                row = db.get_by_slug(slug)
                if row is None:
                    continue
                merged = sorted(set(row["tags"]) | set(names))
                created += write_free_text(log, slug, merged)
                done.append(slug)
                log.slugs.append(slug)
            muts = list(log.muts)
    return Result(True, _flat(OP_MERGE, muts), [], batch_id, dry_run,
                  {"count": len(done), "slugs": done, "created": created})
