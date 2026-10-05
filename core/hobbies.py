"""Hobby service (#541 phase D): the ONE place hobbies are made, unmade and converted.

Same contract as core/cards.py: validate everything first, one `db.transaction()`, every row
written through `db.ImageLog` (row images in the change log), a `Result`, `dry_run`, and `actor`
(None = the request's actor context). Undo is the generic `cards.undo` (POST
/api/changes/{batch}/undo, MCP constructicon_undo). The web routes (web/routes/hobbies.py, the
project page's "Convert to hobby") and the MCP tools are thin adapters over these.

A hobby is a root `blog_tags` row with is_hobby=1, an activity (hobby_status) and a 2-4 char
group_code; cards join it through `project_hobbies` (#360, V2 cards 3.3 / 3.9).

  create(name, status)                 tag (reused or created) marked as a hobby; fills group_code
  unmark(hobby)                        no longer a hobby (the tag stays): its card memberships go,
                                       and cards that named it as their home lose that override
  add_card(hobby, card) / remove_card  = cards.add_to_hobby / remove_from_hobby (already logged)
  set_activity / set_group_code        = cards.set_hobby_activity / set_group_code (already logged)
  convert_from_card(card)              project -> hobby (was db.convert_project_to_hobby, not
                                       undoable); now fully imaged
  convert_to_card(hobby, kind, title, into_hobby)   hobby -> card, the reverse (see below)

Hobby -> card rules (convert_to_card), owner request 2026-10-03 ("GI Joe" -> a family card under
Collecting):
  - A new card of `kind` (family | collection | project) is created, titled after the hobby unless
    `title` is given, REUSING the hobby's tag as its linked tag (so every file tagged with the hobby
    is reachable from the card's tag too), with the usual blank write-up. Its stage follows the
    hobby's activity: active -> in_progress, inactive -> paused.
  - `into_hobby` (optional): the new card joins that hobby.
  - The hobby's member cards move onto the card. Only TOP-LEVEL members are attached: a member
    already nested under another member stays nested under it and comes along with it.
      family / collection: each top-level member becomes a family member (flat, many-to-many).
        A member that is itself a family or collection can't be a member: the whole conversion is
        refused (bad_membership).
      project: each top-level member is nested under the card ("part of", parent_id). A member that
        is a family/collection can't be nested (nest_group_kind), and a member that is already part
        of a card OUTSIDE the hobby keeps its one parent (nest_second_parent): refused, not moved.
    Every member leaves the old hobby.
  - The hobby's loose objects (tagged with it, in none of its member cards; older revisions too)
    are put on the card (membership rows only: they already carry the tag).
  - Cards whose manual home override pointed at the hobby now point at the card.
  - The hobby is unmarked (its tag stays, as the card's linked tag).
  One batch: undo restores the hobby (activity, code, memberships, home overrides) and removes the
  card, its write-up and its memberships.
"""

from . import card_rules, cards, changes, db, membership
from . import tags as tags_svc
from .card_rules import CardError
from .cards import Result
from .errors import InvalidInput

OP_CREATE = "hobby_create"
OP_UNMARK = "hobby_unmark"
OP_FROM_CARD = "convert_project_to_hobby"
OP_TO_CARD = "convert_hobby_to_card"

CONVERT_KINDS = ("family", "collection", "project")

# Already logged + undoable card ops, re-exported under the hobby service's names.
set_activity = cards.set_hobby_activity
set_group_code = cards.set_group_code


def get_hobby(hobby):
    """A hobby id or slug -> its blog_tags row, or CardError not_found (404)."""
    return cards.get_hobby(hobby)


def _status(value):
    """The hobby activity to store (dormant/abandoned -> inactive, with a warning). An unknown
    word is a 400 bad_request with the message the old raw writer used."""
    try:
        return card_rules.validate_hobby_activity(value)
    except CardError:
        raise InvalidInput(f"Invalid hobby_status: {value}. Must be one of {db.HOBBY_STATUSES}") from None


def public(hobby_row):
    """{id, name, slug, status, group_code} (the MCP create_hobby shape)."""
    return {"id": hobby_row["id"], "name": hobby_row["name"], "slug": hobby_row["slug"],
            "status": hobby_row.get("hobby_status"), "group_code": hobby_row.get("group_code")}


def _flat(rows):
    return cards._changes_from_log(rows)


# --- writers on an open ImageLog ----------------------------------------------------------

def _mark(log, tag_id, status):
    """is_hobby=1 + the activity, and a derived group_code when the tag has none (3.9)."""
    row = log.get("blog_tags", {"id": tag_id})
    fields = {"is_hobby": 1, "hobby_status": status}
    if not row.get("group_code"):
        fields["group_code"] = card_rules.derive_group_code(row["name"], db.all_group_codes(log.conn))
    log.update("blog_tags", {"id": tag_id}, fields)


def _unmark(log, tag_id):
    for r in log.conn.execute("SELECT project_id FROM project_hobbies WHERE hobby_tag_id = ? ORDER BY rowid",
                              (tag_id,)).fetchall():
        log.delete("project_hobbies", {"project_id": r["project_id"], "hobby_tag_id": tag_id})
    log.update("blog_tags", {"id": tag_id}, {"is_hobby": 0, "hobby_status": None, "group_code": None})


def _homes_pointing_at(conn, hobby_id):
    return [dict(r) for r in conn.execute(
        "SELECT id, slug FROM projects WHERE home_kind = 'hobby' AND home_ref = ? ORDER BY id", (hobby_id,))]


# --- ops ----------------------------------------------------------------------------------

def create(name, status="active", *, dry_run=False, actor=None, batch_id=None):
    """Makes a hobby from a name: the root tag of that name (reused when one exists anywhere in
    the tree, #213; else created) is marked as a hobby with `status` (default active) and a
    group code. As before, naming an existing hobby re-marks it with `status`. data: {hobby,
    tag_created}."""
    name = (name or "").strip()
    if not name:
        raise InvalidInput("Hobby name can't be empty")
    status, warnings = _status(status)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_CREATE, actor, batch_id) as log:
            tag, made = tags_svc.ensure(log, name, None)
            _mark(log, tag["id"], status)
            log.slugs.append(tag["slug"])
        rows = db.get_change_rows(batch_id=batch_id)
        hobby = db.get_hobby(tag["id"])
    return Result(True, _flat(rows), warnings, batch_id, dry_run, {"hobby": public(hobby), "tag_created": made})


def unmark(hobby, *, dry_run=False, actor=None, batch_id=None):
    """Takes the hobby designation off a tag (activity and group code cleared; the tag itself and
    its file tags stay). Its card memberships are removed (they'd otherwise point at a non-hobby)
    and cards whose home override named it lose the override (their home goes back to automatic).
    data: {hobby: slug, cards_removed, homes_cleared}."""
    hob = get_hobby(hobby)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_UNMARK, actor, batch_id, [hob["slug"]]) as log:
            homes = _homes_pointing_at(log.conn, hob["id"])
            for c in homes:
                log.update("projects", {"id": c["id"]}, {"home_kind": None, "home_ref": None})
                log.slugs.append(c["slug"])
            n_cards = log.conn.execute("SELECT COUNT(*) AS n FROM project_hobbies WHERE hobby_tag_id = ?",
                                       (hob["id"],)).fetchone()["n"]
            _unmark(log, hob["id"])
        rows = db.get_change_rows(batch_id=batch_id)
    return Result(True, _flat(rows), [], batch_id, dry_run,
                  {"hobby": hob["slug"], "cards_removed": n_cards, "homes_cleared": [c["slug"] for c in homes]})


def add_card(hobby, card, *, dry_run=False, actor=None, batch_id=None):
    """Puts a card in a hobby (logged; adding twice is a no-op with a warning)."""
    return cards.add_to_hobby(card, hobby, dry_run=dry_run, actor=actor, batch_id=batch_id)


def remove_card(hobby, card, *, dry_run=False, actor=None, batch_id=None):
    """Takes a card out of a hobby (logged; a no-op if it wasn't in it)."""
    return cards.remove_from_hobby(card, hobby, dry_run=dry_run, actor=actor, batch_id=batch_id)



def convert_from_card(card, *, dry_run=False, actor=None, batch_id=None):
    """Project -> hobby (#360), DESTRUCTIVE but undoable now (#541 phase D). In one transaction:
    the card's dependents are cleared exactly as delete_card clears them (#497: open questions,
    a blank write-up, its own hobby/family rows, typed links, blog-entry attachments, cover/home
    pointers at it); its linked tag (or a root tag named after it) becomes an active hobby; its
    child cards join the hobby and stop being nested; its files get the hobby tag; the card row and
    its membership rows go. data: {hobby, card, children_moved, items_moved}."""
    row = cards.get_card(card)
    batch_id = batch_id or changes.new_batch_id()
    warnings = []
    with db.transaction(dry_run=dry_run):
        fresh = db.get_project(row["id"])
        cards._clear_card_dependents(fresh, OP_FROM_CARD, actor, batch_id, [], warnings, dissolving=True)
        child_ids = [c["id"] for c in db.list_child_projects(row["id"])]
        item_slugs = [r["post_slug"] for r in db.list_project_item_rows(row["id"])]
        with db.ImageLog(OP_FROM_CARD, actor, batch_id, [row["slug"]]) as log:
            tag = db.get_tag(fresh["tag_id"]) if fresh.get("tag_id") else None
            if tag is None:
                tag, _ = tags_svc.ensure(log, fresh["title"], None)
            _mark(log, tag["id"], "active")
            log.slugs.append(tag["slug"])
            for cid in child_ids:
                key = {"project_id": cid, "hobby_tag_id": tag["id"]}
                if log.get("project_hobbies", key) is None:
                    log.insert("project_hobbies", key, {})
                log.update("projects", {"id": cid}, {"parent_id": None})
            for slug in item_slugs:
                tags_svc.link(log, slug, tag["id"])
            membership.write_items(log, row["id"], (), item_slugs)
            log.delete("projects", {"id": row["id"]})
        rows = db.get_change_rows(batch_id=batch_id)
        hobby = db.get_hobby(tag["id"])
    return Result(True, _flat(rows), warnings, batch_id, dry_run,
                  {"hobby": public(hobby), "card": row["slug"], "children_moved": len(child_ids),
                   "items_moved": len(item_slugs)})


def _plan_members(hob, kind, title):
    """(top-level members, nested members) of the hobby, or CardError listing every member the
    rules for `kind` refuse (see the module docstring)."""
    members = db.list_projects_for_hobby(hob["id"])
    ids = {m["id"] for m in members}
    top = [m for m in members if m.get("parent_id") not in ids]
    nested = [m for m in members if m.get("parent_id") in ids]
    group = kind in card_rules.GROUP_KINDS
    problems = []
    for m in top:
        mk = m.get("kind") or card_rules.DEFAULT_KIND
        if mk in card_rules.GROUP_KINDS:
            problems.append(("bad_membership" if group else "nest_group_kind", m,
                             f"'{m['title']}' is a {card_rules.kind_label(mk)}"))
        elif not group and m.get("parent_id") is not None:
            problems.append(("nest_second_parent", m, f"'{m['title']}' is already part of another card"))
    if problems:
        what = "members of" if group else "part of"
        raise CardError(problems[0][0],
                        f"Can't make these cards {what} the new {card_rules.kind_label(kind).lower()} "
                        f"'{title}': " + "; ".join(p[2] for p in problems) + ". Nothing was changed.",
                        {"members": [p[1]["slug"] for p in problems]})
    return top, nested


def convert_to_card(hobby, kind, title=None, into_hobby=None, *, dry_run=False, actor=None, batch_id=None):
    """Hobby -> card: the reverse of convert_from_card. See the module docstring for the rules per
    kind. Everything is validated before the first write; one batch, undoable, `dry_run` shows the
    whole plan. data: {card, card_id, kind, title, into_hobby, hobby, members: [{slug, how}],
    loose_moved, homes_moved}."""
    hob = get_hobby(hobby)
    kind = (kind or "").strip().lower()
    if kind not in CONVERT_KINDS:
        raise CardError("bad_kind", f"Convert a hobby into one of: {', '.join(CONVERT_KINDS)} (not {kind!r}).")
    title = (title or "").strip() or hob["name"]
    target = None
    if into_hobby not in (None, ""):
        target = get_hobby(into_hobby)
        if target["id"] == hob["id"]:
            raise CardError("bad_hobby", "A hobby can't be converted into a card inside itself.")
    stage = "paused" if (hob.get("hobby_status") or "active") == "inactive" else "in_progress"
    card_rules.validate_status(kind, stage)
    top, nested = _plan_members(hob, kind, title)
    loose = [r["slug"] for r in db.list_loose_hobby_objects(hob["id"], include_superseded=True)]
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        made = cards.create(title, kind=kind, stage=stage, tag_id=hob["id"], actor=actor, batch_id=batch_id)
        new = made.data["card"]
        if target is not None:
            cards.add_to_hobby(new["id"], target["id"], actor=actor, batch_id=batch_id)
        moved = []
        for m in top:
            if kind in card_rules.GROUP_KINDS:
                cards.add_to_family(new["id"], m["id"], actor=actor, batch_id=batch_id, _op=OP_TO_CARD)
                moved.append({"slug": m["slug"], "how": "member"})
            else:
                cards.nest(m["id"], new["id"], actor=actor, batch_id=batch_id)
                moved.append({"slug": m["slug"], "how": "nested"})
        for m in nested:
            moved.append({"slug": m["slug"], "how": "stays under its parent"})
        with db.ImageLog(OP_TO_CARD, actor, batch_id, [hob["slug"], new["slug"]]) as log:
            membership.write_items(log, new["id"], loose, ())
            homes = _homes_pointing_at(log.conn, hob["id"])
            for c in homes:
                log.update("projects", {"id": c["id"]}, {"home_kind": "card", "home_ref": new["id"]})
            _unmark(log, hob["id"])  # also drops every member's project_hobbies row
        rows = db.get_change_rows(batch_id=batch_id)
    return Result(True, _flat(rows), made.warnings, batch_id, dry_run,
                  {"card": new["slug"], "card_id": new["id"], "kind": kind, "title": title,
                   "into_hobby": target["slug"] if target else None, "hobby": hob["slug"], "members": moved,
                   "loose_moved": len(loose), "homes_moved": [c["slug"] for c in homes]})
