"""Membership service (#541 phase C): the ONE way files go onto a card and come off it.

Same contract as core/cards.py: validate first, one `db.transaction()`, rows through
`db.ImageLog` (row images), a `Result`, `dry_run`, `actor` (None = the request's actor context).
Undo is the generic `cards.undo` (POST /api/changes/{batch}/undo, MCP constructicon_undo).

  add_files(card, slugs, *, link_tag, merge_free_tags, auto_cover)
  remove_files(card, slugs)
  write(card_id, add, remove, op, actor, batch_id, affected)   the plain row writer the card
                                   reshaping ops (copy/move/split/merge/delete) use: membership
                                   rows only, never a side effect, logged under THEIR op.

The side effects are explicit flags with no defaults, so every caller says what it wants. They
are what core/ingest.py's attach_to_project always did (#1, #274, #103):
  link_tag         the card's linked tag (projects.tag_id, minted with the card) is attached to
                   the file in post_tags, so the file shows up in tag browsing.
  merge_free_tags  that tag's NAME is appended to the file's free-text `tags` column (if it isn't
                   there), so it shows as a chip in the item page's TAGS box (#274).
  auto_cover       a card with no cover_slug takes the first file added as its cover (#103). As
                   before this also clears a borrowed cover_project_id and bumps updated_at.
Each side effect applies to every named file even when it was already on the card (the old
path did the same), and each is imaged, so one undo reverses membership, tags and cover.

Removing never touches tags or the cover, on every path (the item page's own docstring calls
this out: "removing membership never untags"). Undo of a removal restores the row in place
(sort_order and rowid).

UI_EFFECTS is the item page / upload / bulk behaviour; MCP add-to-project uses it too since
phase C (it used to do the linked tag only).
"""

import time

from . import changes, db, tags
from .cards import Result
from .errors import NotFound

OP_ADD = "add_files"
OP_REMOVE = "remove_files"

UI_EFFECTS = {"link_tag": True, "merge_free_tags": True, "auto_cover": True}
NO_EFFECTS = {"link_tag": False, "merge_free_tags": False, "auto_cover": False}


def get_card(card):
    row = db.get_project(card) if card not in (None, "") else None
    if row is None:
        raise NotFound(f"No card {card!r}.")
    return row


def _slugs(slugs):
    if isinstance(slugs, str):
        slugs = [slugs]
    return list(dict.fromkeys((s or "").strip() for s in (slugs or []) if (s or "").strip()))


# --- the row writer ----------------------------------------------------------------------

def write_items(log, card_id, add=(), remove=()):
    """project_items rows on an open ImageLog: removals first, then additions appended at the end
    (already-present files skipped). Returns (added, removed)."""
    added, removed = [], []
    for slug in remove:
        if log.delete("project_items", {"project_id": card_id, "post_slug": slug}):
            removed.append(slug)
    if add:
        nxt = db.next_item_sort_order(log.conn, card_id)
        for slug in add:
            if log.get("project_items", {"project_id": card_id, "post_slug": slug}) is not None:
                continue
            log.insert("project_items", {"project_id": card_id, "post_slug": slug}, {"sort_order": nxt})
            nxt += 1
            added.append(slug)
    return added, removed


def write(card_id, add, remove, op, actor, batch_id=None, affected_slugs=None):
    """Membership rows only, as one change-log row under the caller's `op` (the card reshaping
    ops). Returns (added, removed). Was db.write_card_items."""
    with db.ImageLog(op, actor, batch_id, affected_slugs) as log:
        return write_items(log, card_id, add, remove)


# --- ops ---------------------------------------------------------------------------------

def add_files(card, slugs, *, link_tag, merge_free_tags, auto_cover, missing_ok=False, dry_run=False,
              actor=None, batch_id=None):
    """Puts files on a card with the named side effects (see the module docstring). Unknown file
    slugs raise not_found before anything is written, unless `missing_ok` (the bulk paths skip
    them, as before). One batch, one change-log row. data: {card, slugs, added, already,
    skipped, tagged, cover}."""
    wanted = _slugs(slugs)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        project = get_card(card)
        present, skipped = [], []
        for s in wanted:
            if db.get_by_slug(s) is None:
                if missing_ok:
                    skipped.append(s)
                    continue
                raise NotFound(f"No item {s!r}.")
            present.append(s)
        tag = db.get_tag(project["tag_id"]) if project.get("tag_id") else None
        tagged, cover = [], None
        with db.ImageLog(OP_ADD, actor, batch_id, [project["slug"]] + present) as log:
            added, _ = write_items(log, project["id"], present, ())
            for s in present:
                if link_tag and tag is not None and tags.link(log, s, tag["id"]):
                    tagged.append(s)
                if merge_free_tags and tag is not None:
                    tags.merge_free_text_name(log, s, tag["name"])
            if auto_cover and present and not project.get("cover_slug"):
                cover = present[0]
                log.update("projects", {"id": project["id"]},
                           {"cover_slug": cover, "cover_project_id": None, "updated_at": time.time()})
            muts = list(log.muts)
    already = [s for s in present if s not in added]
    return Result(True, _flat(OP_ADD, muts), [], batch_id, dry_run,
                  {"card": project["slug"], "slugs": present, "added": added, "already": already,
                   "skipped": skipped, "tagged": tagged, "cover": cover})


def remove_files(card, slugs, *, dry_run=False, actor=None, batch_id=None):
    """Takes files off a card (membership only: tags and the cover are left alone). Files that
    aren't on the card are a no-op, as before. data: {card, removed}."""
    wanted = _slugs(slugs)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        project = get_card(card)
        with db.ImageLog(OP_REMOVE, actor, batch_id, [project["slug"]] + wanted) as log:
            _, removed = write_items(log, project["id"], (), wanted)
            muts = list(log.muts)
    return Result(True, _flat(OP_REMOVE, muts), [], batch_id, dry_run, {"card": project["slug"], "removed": removed})


# --- tags that follow a transfer (#590) ------------------------------------------------

def plan_transfer_tags(slugs, src, dst, *, drop_source, ignore_card_ids=()):
    """What a move / copy / merge does to the files' tags, as a read-only plan (so a dry run can
    show it). The files gain `dst`'s linked tag in both stores (post_tags and the free-text chip).
    With `drop_source` they lose `src`'s linked tag in both stores, but only that exact tag (a tag
    the user added by hand under another name stays), and not while the file is still on another
    card whose linked tag is the same tag (or has the same name, for the chip).
    `ignore_card_ids`: cards that are going away in the same batch (a merge's absorbed cards), so
    they don't count as "still on another card". Returns a list of per-file dicts
    {slug, link_add, chip_add, link_drop, chip_drop} with only the files that change, plus the two
    tag dicts: (changes, dst_tag, src_tag)."""
    dst_tag = db.get_tag(dst["tag_id"]) if dst.get("tag_id") else None
    src_tag = db.get_tag(src["tag_id"]) if drop_source and src.get("tag_id") else None
    if src_tag is not None and dst_tag is not None and src_tag["id"] == dst_tag["id"]:
        src_tag = None
    if dst_tag is None and src_tag is None:
        return [], None, None
    skip_ids = {src["id"], *ignore_card_ids}
    marks = ",".join("?" for _ in skip_ids)
    out = []
    conn = db.get_conn()
    try:
        for slug in slugs:
            row = db.get_by_slug(slug)
            if row is None:
                continue
            have = {r["tag_id"] for r in conn.execute("SELECT tag_id FROM post_tags WHERE post_slug = ?", (slug,))}
            chips = list(row.get("tags") or [])
            d = {"slug": slug, "link_add": False, "chip_add": False, "link_drop": False, "chip_drop": False}
            if dst_tag is not None:
                d["link_add"] = dst_tag["id"] not in have
                d["chip_add"] = dst_tag["name"] not in chips
            if src_tag is not None:
                elsewhere = conn.execute(
                    "SELECT t.id, t.name FROM project_items pi JOIN projects p ON p.id = pi.project_id "
                    f"JOIN blog_tags t ON t.id = p.tag_id WHERE pi.post_slug = ? AND p.id NOT IN ({marks})",
                    (slug, *skip_ids)).fetchall()
                same_tag = any(r["id"] == src_tag["id"] for r in elsewhere)
                same_name = any(r["name"] == src_tag["name"] for r in elsewhere)
                d["link_drop"] = src_tag["id"] in have and not same_tag
                d["chip_drop"] = (src_tag["name"] in chips and not same_name
                                  and not (dst_tag is not None and dst_tag["name"] == src_tag["name"]))
            if any(d[k] for k in ("link_add", "chip_add", "link_drop", "chip_drop")):
                out.append(d)
    finally:
        conn.close()
    return out, dst_tag, src_tag


def apply_transfer_tags(plan, dst_tag, src_tag, op, actor, batch_id, affected_slugs=None):
    """Writes a `plan_transfer_tags` plan as one imaged change-log row under the caller's `op`
    and batch, so the batch's one undo reverses it with the membership change."""
    if not plan:
        return
    with db.ImageLog(op, actor, batch_id, affected_slugs or [d["slug"] for d in plan]) as log:
        for d in plan:
            if d["link_drop"]:
                tags.unlink(log, d["slug"], src_tag["id"])
            if d["chip_drop"]:
                tags.remove_free_text_name(log, d["slug"], src_tag["name"])
            if d["link_add"]:
                tags.link(log, d["slug"], dst_tag["id"])
            if d["chip_add"]:
                tags.merge_free_text_name(log, d["slug"], dst_tag["name"])


def _flat(op, muts):
    from .items import _flat as items_flat  # same change-summary shape as the item service
    return items_flat(op, muts)
