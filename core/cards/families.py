"""Group spans (#772), "part of" nesting and families / collections (spec 3.6, 3.7).

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import card_rules, changes, db, policy, timeline
from .base import get_card, Result


# --- Group spans (#772) -------------------------------------------------------------

def member_spans(card, *, filter_cards=None, filter_items=None):
    """#772: the (start, end) of each member of a family / collection, for
    timeline.resolve_project_span(member_spans=...). [] for any other kind. One level only: a
    member's span is its own (members can't be groups, card_rules.validate_membership), so there
    is no cycle to guard. `filter_cards` / `filter_items` let a page keep to what its viewer may
    see (policy.filter_visible_cards / filter_visible); omitted = every member and file."""
    if (card.get("kind") or "project") not in card_rules.GROUP_KINDS:
        return []
    members = db.list_family_members(card["id"])
    if filter_cards is not None:
        members = filter_cards(members)
    spans = []
    for m in members:
        items = db.list_project_items(m["id"])
        if filter_items is not None:
            items = filter_items(items)
        spans.append(timeline.resolve_project_span(m, items))
    return spans


def card_span(card, items=None, *, filter_cards=None, filter_items=None):
    """A card's effective (start, end): timeline.resolve_project_span over its own items, rolled up
    over its members' spans for a family / collection (#772). The card's own Timeline start/end
    overrides still win. `items` = its own rows (fetched when omitted)."""
    if items is None:
        items = db.list_project_items(card["id"])
        if filter_items is not None:
            items = filter_items(items)
    return timeline.resolve_project_span(
        card, items, member_spans=member_spans(card, filter_cards=filter_cards, filter_items=filter_items))


# --- Nesting ("part of") and families (3.6, 3.7) ----------------------------------

def _slug_of(card_id):
    c = db.get_project(card_id) if card_id is not None else None
    return c["slug"] if c else None


def nest(child, parent, *, replace=False, dry_run=False, actor=None, batch_id=None):
    """Makes `child` part of `parent` (parent_id). Rules (3.7, card_rules.validate_nest):
    no self/cycle, neither end a family or collection, and a card that already
    has a different parent is refused (nest_second_parent) unless replace=True."""
    row, parent_row = get_card(child), get_card(parent)
    db.check_nest(row, parent_row["id"], replace=replace)
    old = row.get("parent_id")
    rows = []
    warnings = []
    if old != parent_row["id"]:
        rows = [{"card": row["slug"], "field": "parent", "before": _slug_of(old), "after": parent_row["slug"]}]
        if old is not None:
            warnings.append(f"Moved '{row['title']}' out of '{_slug_of(old)}' into '{parent_row['title']}'.")
        if row.get("activity") and parent_row.get("activity") and row["activity"] != parent_row["activity"]:
            warnings.append(f"'{row['title']}' is {row['activity']} but '{parent_row['title']}' is {parent_row['activity']}.")
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db._update_card_columns(row["id"], {"parent_id": parent_row["id"]}, "nest", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def unnest(child, *, dry_run=False, actor=None, batch_id=None, _op="unnest"):
    """Takes `child` out of its parent (it becomes top-level). No-op when it has none."""
    row = get_card(child)
    old = row.get("parent_id")
    rows = []
    if old is not None:
        rows = [{"card": row["slug"], "field": "parent", "before": _slug_of(old), "after": None}]
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db._update_card_columns(row["id"], {"parent_id": None}, _op, actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def add_to_family(family, member, *, dry_run=False, actor=None, batch_id=None, _op="add_to_family"):
    """Adds `member` to a family or collection (many-to-many; a card can be in
    several). Validated by card_rules.validate_membership. Adding twice is a no-op."""
    fam, mem = get_card(family), get_card(member)
    card_rules.validate_membership(fam, mem)
    exists = db.is_family_member(fam["id"], mem["id"])
    rows = [] if exists else [{"card": mem["slug"], "field": "in_family", "before": None, "after": fam["slug"]}]
    warnings = [f"'{mem['title']}' is already in '{fam['title']}'."] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and not exists:
        db._add_family_member(fam["id"], mem["id"], _op, actor, batch_id=batch_id,
                             affected_slugs=[fam["slug"], mem["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def remove_from_family(family, member, *, dry_run=False, actor=None, batch_id=None):
    """Takes `member` out of a family or collection. No-op if it wasn't in it.
    Neither card is otherwise changed."""
    fam, mem = get_card(family), get_card(member)
    exists = db.is_family_member(fam["id"], mem["id"])
    rows = [{"card": mem["slug"], "field": "in_family", "before": fam["slug"], "after": None}] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and exists:
        db._remove_family_member(fam["id"], mem["id"], "remove_from_family", actor, batch_id=batch_id,
                                affected_slugs=[fam["slug"], mem["slug"]])
    return Result(True, rows, [], batch_id, dry_run)


def family_fields(card):
    """Membership facts for pages and tools: the families/collections this card is
    in, and (for a group kind) its members."""
    def slim(c):
        return {"id": c["id"], "slug": c["slug"], "title": c["title"], "kind": c.get("kind") or "project",
                "kind_label": card_rules.kind_label(c.get("kind") or "project"), "stage": c.get("stage"),
                "activity": c.get("activity")}
    acc = policy.card_access()  # #604 step 3: never name a private card the actor isn't on
    out = {"families": [slim(f) for f in policy.filter_visible_cards(db.list_families_for_member(card["id"]), access=acc)],
           "members": []}
    if (card.get("kind") or "project") in card_rules.GROUP_KINDS:
        out["members"] = [slim(m) for m in policy.filter_visible_cards(db.list_family_members(card["id"]), access=acc)]
    return out
