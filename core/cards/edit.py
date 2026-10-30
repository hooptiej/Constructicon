"""Creating a card and editing its own fields (#541 phase D), cover picks (#554), agent notes, and the
blank write-up a card leaves behind.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

import time

from .. import card_rules, changes, datefmt, db, item_title, policy
from ..card_rules import CardError
from ..errors import InvalidInput
from .base import _changes_from_log, get_card, Result
from .text import writeup_lead
from .reshape import _membership


# --- Creating a card and editing its own fields (#541 phase D) ------------------------

OP_CREATE_CARD = "create_card"
OP_UPDATE_CARD = "update_card"


def create(title, *, description="", cover_slug=None, parent=None, kind=None, stage=None, stop_reason=None,
           tag_id=None, link_tag=True, with_writeup=True, dry_run=False, actor=None, batch_id=None):
    """Creates a card as ONE undoable batch (#541 phase D): its linked root tag (#1; reused when a
    tag of that name exists, else created and imaged, exactly the old get_or_create_tag lookup),
    the card row and its blank write-up document (#156/#423: added to the card, tagged with the
    linked tag, set as writeup_slug). `cards.undo(batch_id)` removes all of it again; a tag the
    create reused is left alone. Every rule is checked before anything is written: an empty title
    (bad_request), kind / stage / stop_reason (card_rules, bad_status / bad_kind) and the nest
    rules for `parent` (an id or slug; not_found / nest_*). `tag_id` links an existing tag
    instead (hobby -> card conversion); link_tag=False makes a card with no linked tag.
    data: {card: the new projects row (as create_project returned it), tag_created: bool}."""
    title = (title or "").strip()
    if not title:
        raise InvalidInput("Project name can't be empty")
    kind = card_rules.validate_kind(kind or card_rules.DEFAULT_KIND)
    card_rules.validate_status(kind, stage or card_rules.DEFAULT_STAGE, stop_reason)
    parent_row = None
    if parent not in (None, ""):
        parent_row = db.get_project(parent)
        if parent_row is None or not policy.can_view_card(parent_row):  # #604 step 3: hidden = missing
            raise CardError("not_found", f"No such parent card: {parent!r}")
        card_rules.validate_nest({"id": None, "kind": kind, "title": title, "parent_id": None}, parent_row, ())
    from .. import tags as _tags  # lazy: tags imports Result from here
    batch_id = batch_id or changes.new_batch_id()
    tag_created = False
    with db.transaction(dry_run=dry_run):
        if tag_id is None and link_tag:
            with db.ImageLog(_tags.OP_CREATE, actor, batch_id) as log:
                tag, tag_created = _tags.ensure(log, title, None)
            tag_id = tag["id"]
        project = db._create_project(title, description=description, cover_slug=cover_slug, tag_id=tag_id,
                                     parent_id=parent_row["id"] if parent_row else None, with_writeup=with_writeup,
                                     kind=kind, stage=stage, stop_reason=stop_reason, actor=actor, batch_id=batch_id)
        flat = _changes_from_log(db.get_change_rows(batch_id=batch_id))
    return Result(True, flat, [], batch_id, dry_run, {"card": project, "tag_created": tag_created})


def _writeup_target(slug):
    """Validates a write-up slug (the web form's and MCP's old checks, same messages)."""
    from .. import object_types  # lazy: the registry imports a lot
    row = db.get_by_slug(slug)
    if row is None:
        raise InvalidInput("writeup slug not found")
    if not object_types.can_be_writeup(row):
        label = object_types.get_object_type(row.get("media_type")).label
        raise InvalidInput(f"{label} items can't be a project write-up (their type declares no writeup_body_key)")
    return row


def update(card, *, title=None, description=None, cover_slug=None, cover_project_id=..., writeup_slug=...,
           start=..., end=..., dry_run=False, actor=None, batch_id=None):
    """A card's own fields as ONE imaged change-log row (op update_card; #541 phase D: was
    db.update_project + set_project_date_overrides, unlogged, so undoing a Save restored the kind
    and stage but not the title). Same semantics as before:
      title / description / cover_slug: None = leave alone (cover_slug "" stores "").
      cover_project_id (#356): ... = leave, None = clear, an id = borrow that CHILD card's cover.
      writeup_slug (#156): ... = leave, None/"" = clear, a slug = that item (it must exist and its
        type must declare a writeup body). The card's cached write-up lead (#596) follows it.
      start / end: the timeline overrides; ... = leave, None = clear, unix seconds = set.
    A non-empty cover_slug clears cover_project_id and vice versa (mutually exclusive). Every
    call bumps updated_at, as the old Save did. Parent, kind and status have their own ops
    (nest / unnest / set_kind / set_status). data: {card: the row after}."""
    row = get_card(card)
    if writeup_slug is not ... and writeup_slug:
        _writeup_target(writeup_slug)
    if cover_project_id is not ... and cover_project_id is not None:
        child = db.get_project(cover_project_id)
        if child is None:
            raise InvalidInput("Child project not found")
        if not can_lend_to(row, child):  # #554: a nested child, or a member of this family / collection
            raise InvalidInput(f"'{child['title']}' isn't nested in '{row['title']}' or a member of it, "
                               "so it can't lend its cover.")
        cover_project_id = child["id"]
    fields = {}
    if title is not None:
        fields["title"] = title
    if description is not None:
        fields["description"] = description
    new_cover_slug = cover_slug if cover_slug is not None else row.get("cover_slug")
    new_cover_project = cover_project_id if cover_project_id is not ... else row.get("cover_project_id")
    if cover_slug not in (None, ""):
        new_cover_project = None
    if cover_project_id is not ... and cover_project_id is not None:
        new_cover_slug = None
    fields["cover_slug"], fields["cover_project_id"] = new_cover_slug, new_cover_project
    if writeup_slug is not ...:
        fields["writeup_slug"] = writeup_slug or None
        fields["writeup_lead"] = writeup_lead(writeup_slug) if writeup_slug else None  # #596: the face's cache
    if start is not ...:
        fields["start_date_override"] = None if start is None else float(start)
    if end is not ...:
        fields["end_date_override"] = None if end is None else float(end)
    fields["updated_at"] = time.time()
    ghost = _blank_writeup_left_behind(row, writeup_slug)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        warnings = []
        with db.ImageLog(OP_UPDATE_CARD, actor, batch_id, [row["slug"]]) as log:
            log.update("projects", {"id": row["id"]}, fields)
            if title is not None and title != row["title"]:
                # #555: the card's linked tag follows its title, in this same change-log row (one undo).
                from .. import tags as _tags  # lazy: tags imports Result from here
                warn = _tags.follow_card_title(log, row, title)
                if warn:
                    warnings.append(warn)
            muts = [{"op": OP_UPDATE_CARD, "affected_slugs": [row["slug"]], "mutations": list(log.muts)}]
        changed = _changes_from_log(muts)
        if ghost:
            changed += _retire_blank_writeup(row, ghost, dry_run=dry_run, actor=actor, batch_id=batch_id)
        fresh = db.get_project(row["id"])
    return Result(True, changed, warnings, batch_id, dry_run, {"card": fresh})


# --- Cover picks, the same everywhere (#554, batch 5) ----------------------------------------
# One control on every page: tick a file to make it the cover, or tick a child card (a nested part,
# a family / collection member, a hobby's card) to BORROW its cover, which then follows that card's
# cover (projects.cover_project_id / hobby_settings.cover_project_id). One cover at a time. Every
# refusal says what, why and what to do next (bad_cover 422, details.reason).

def can_lend_to(card_row, lender):
    """#554: may `lender` lend its cover to `card_row`? A card nested in it (#356), or a member of
    it when it is a family / collection."""
    if lender.get("id") == card_row.get("id"):
        return False
    if lender.get("parent_id") == card_row["id"]:
        return True
    return ((card_row.get("kind") or card_rules.DEFAULT_KIND) in card_rules.GROUP_KINDS
            and db.is_family_member(card_row["id"], lender["id"]))


def _cover_chain_ids(card_row, limit=12):
    """The ids a card's cover goes through (itself, then each card it borrows from)."""
    ids, cur = [], card_row
    while cur is not None and len(ids) < limit and cur["id"] not in ids:
        ids.append(cur["id"])
        if cur.get("cover_slug") or not cur.get("cover_project_id"):
            break
        cur = db.get_project(cur["cover_project_id"])
    return ids


def cover_file_problem(slug, actor=None):
    """#554: why the file `slug` can't be shown as a cover to this actor right now, as
    (reason, message), or None when it can. Reasons: missing, hidden, redacted, no_picture."""
    from .. import object_types  # lazy: the registry imports a lot
    row = db.get_by_slug(slug) if slug else None
    if row is None:
        return "missing", "its cover file no longer exists (deleted or in the trash)"
    if not policy.can_view(row, actor):
        return "hidden", ("its cover is a file you can't see (marked sensitive, a restricted type, or on a "
                          "private card); only its uploader or an admin can pick it")
    if row.get("redacted"):
        return "redacted", "its cover file was removed (redacted), so there is no picture to show"
    if object_types.get_object_type(row.get("media_type")).thumbnail_source == object_types.ThumbnailSource.NONE:
        return "no_picture", f"{item_title.title_of(row)} is a file with no picture to show"
    return None


def lend_problem(lender, borrower_card_id=None, actor=None):
    """#554: why `lender` (a card row) can't lend its cover right now, as (reason, message), or
    None. Reasons: cover_loop (it already borrows from the borrower), no_cover, and the file reasons
    of cover_file_problem."""
    if borrower_card_id is not None and borrower_card_id in _cover_chain_ids(lender):
        return "cover_loop", (f"'{lender['title']}' already borrows its cover from this card, so this card "
                              "can't borrow it back. Give one of them a cover of its own first.")
    slug = db.resolve_project_cover_slug(lender)
    if not slug:
        return "no_cover", (f"'{lender['title']}' has no cover yet, so there is nothing to borrow. Give it a "
                            "cover first (tick one of its files on its page), then tick it here.")
    problem = cover_file_problem(slug, actor)
    if problem:
        return problem[0], f"'{lender['title']}' can't lend its cover: {problem[1]}."
    return None


def _refuse_cover(reason, message, **details):
    raise CardError("bad_cover", f"Nothing was changed: {message}", {"reason": reason, **details})


def set_cover(card, *, slug=None, from_card=None, dry_run=False, actor=None, batch_id=None):
    """#554: the one cover pick for a card page. Exactly one of `slug` (a file: it must exist, be
    visible to the actor, not be redacted and have a picture) or `from_card` (a nested child or a
    family / collection member, by id or slug: it must have a cover of its own that the actor can
    see, and must not already borrow this card's). Picking a file clears a borrowed card and vice
    versa. One imaged update_card row (cards.update), undoable; the same pick again writes nothing.
    Refusals: not_found (no such file or card), bad_cover (422) with details.reason. data: {card,
    cover_slug, cover_project_id, cover: the resolved slug}."""
    row = get_card(card)
    if (slug in (None, "")) == (from_card in (None, "")):
        raise InvalidInput("Pick exactly one cover: a file (slug) or a card to borrow from (from_card).")
    if slug not in (None, ""):
        item = db.get_by_slug(slug)
        if item is None:
            raise CardError("not_found", f"No file {slug!r}.")
        problem = cover_file_problem(slug, actor)
        if problem:
            _refuse_cover(problem[0], f"{item_title.title_of(item) if problem[0] != 'hidden' else 'that file'} "
                                      f"can't be the cover of '{row['title']}': {problem[1]}.", slug=slug)
        if row.get("cover_slug") == slug and not row.get("cover_project_id"):
            return Result(True, [], ["Nothing changed."], None, dry_run, _cover_data(row))
        res = update(row["id"], cover_slug=slug, dry_run=dry_run, actor=actor, batch_id=batch_id)
    else:
        lender = get_card(from_card)
        if not can_lend_to(row, lender):
            _refuse_cover("not_related", f"'{lender['title']}' isn't nested in '{row['title']}' or a member of it, "
                                         "so it can't lend its cover here.", card=lender["slug"])
        problem = lend_problem(lender, row["id"], actor)
        if problem:
            _refuse_cover(problem[0], problem[1], card=lender["slug"])
        if row.get("cover_project_id") == lender["id"] and not row.get("cover_slug"):
            return Result(True, [], ["Nothing changed."], None, dry_run, _cover_data(row))
        res = update(row["id"], cover_project_id=lender["id"], dry_run=dry_run, actor=actor, batch_id=batch_id)
    res.data = _cover_data(res.data["card"])
    return res


def _cover_data(row):
    return {"card": row["slug"], "cover_slug": row.get("cover_slug") or None,
            "cover_project_id": row.get("cover_project_id"), "cover": db.resolve_project_cover_slug(row)}


OP_CARD_AGENT_NOTES = "card_agent_notes"


def appended_notes(existing, text, epoch=None):
    """(#763) Agent notes with a dated block added under `existing`: "--- 2026-10-27 ---" (the
    Mountain Time day of `epoch`, default now), then `text`; a blank line separates it from what was
    there. `text` must be non-blank text (InvalidInput otherwise: an empty append is a mistake, not a
    no-op). Pure apart from the clock. The one place both append paths (an item's notes, a card's)
    build the new text."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidInput("append=true needs notes: the text to add under a dated header. Nothing was changed.")
    header = f"--- {datefmt.iso_day(time.time() if epoch is None else epoch)} ---"
    block = f"{header}\n{text.strip()}"
    before = (existing or "").rstrip()
    return f"{before}\n\n{block}" if before else block


def set_agent_notes(card, notes, *, append=False, dry_run=False, actor=None, batch_id=None):
    """(#745) Sets (or, with None, clears) the card's private agent notes: the card twin of an item's
    agent_notes (#206, `items.update(agent_notes=...)`), stored exactly as given, the same way. ONE
    imaged change-log row (op card_agent_notes), undoable with the generic undo; a no-op logs nothing.
    Deliberately NOT part of `update`: it leaves updated_at alone, so a private note never reorders
    the card lists or shows as an edit anywhere public. Only the MCP (`constructicon_set_agent_notes`)
    calls it. Change summaries leave the text out (`_changes_from_log` strips private card
    columns); this op's own summary says only that the field changed. data: {card: the row after
    (no notes, see db._card_row), agent_notes: the notes after}.
    append=True (#763): `notes` is added under the existing notes as a dated block
    (`appended_notes`), read and written inside the one transaction (its write lock is taken
    first), so two appends never lose each other's text."""
    if append:
        appended_notes("", notes)  # validates: non-blank text
    elif notes is not None and not isinstance(notes, str):
        raise InvalidInput("notes must be text, or null to clear them.")
    row = get_card(card)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        if append:
            notes = appended_notes(db.get_card_agent_notes(row["id"]), notes)
        with db.ImageLog(OP_CARD_AGENT_NOTES, actor, batch_id, [row["slug"]]) as log:
            log.update("projects", {"id": row["id"]}, {"agent_notes": notes})
            muts = list(log.muts)
        after = db.get_card_agent_notes(row["id"])
        fresh = db.get_project(row["id"])
    changed = [{"op": OP_CARD_AGENT_NOTES, "card": row["slug"], "table": "projects", "key": m["key"],
                "field": "agent_notes", "before": "(private)", "after": "(private)"} for m in muts]
    return Result(True, changed, [] if changed else ["Nothing changed."], batch_id if changed else None,
                  dry_run, {"card": fresh, "agent_notes": after})


def _blank_writeup_left_behind(row, writeup_slug):
    """#672: the card's previous write-up when `writeup_slug` points the card at a DIFFERENT item
    and the previous one is an empty document (the blank write-up every card is born with), else
    None. A previous write-up with any text is never touched."""
    old = row.get("writeup_slug")
    if writeup_slug is ... or not writeup_slug or not old or old == writeup_slug:
        return None
    return old if db.blank_document_body(old) else None


def _retire_blank_writeup(row, slug, *, dry_run, actor, batch_id):
    """#672: takes the card's old, empty write-up off the card and trashes it, in the caller's
    batch (one undo restores it with the old pointer). If another card still holds it (as its
    write-up or as a file), it only leaves this card. Returns change rows."""
    others = [c for c in db.list_projects_for_post(slug) if c["id"] != row["id"]]
    if others or [i for i in db.card_ids_with_writeup(slug) if i != row["id"]]:
        res = _membership().remove_files(row["id"], [slug], dry_run=dry_run, actor=actor, batch_id=batch_id)
        return list(res.changes)
    from .. import items  # lazy: core.items imports this module
    res = items.delete(slug, dry_run=dry_run, actor=actor, batch_id=batch_id)
    return list(res.changes)
