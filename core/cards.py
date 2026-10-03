"""V2 card operations (docs/design/v2-cards.md section 6).

Every function takes `card` as a project id or slug, runs the validators in
core/card_rules, writes inside one transaction (via core/db.py helpers, which
own all SQL), records a change-log row (core/changes), and returns a Result.
All accept `dry_run` (compute and return `changes`, write nothing) and `actor`
('owner-ui' | 'mcp'). The web routes and the MCP tools are thin wrappers over
these; no rule lives anywhere else.

Piece 1 scope: set_status, set_kind, resolve_decision (for the card_* decision
kinds), list_needs_decision (stored decisions only), plus the helpers the
Curator and the pages use to read a card's live status.

Piece 3 adds families/collections (add_to_family, remove_from_family), "part of"
nesting (nest, unnest) and the card_family_members decision resolution.
"""

import re
import time
from dataclasses import dataclass, field

from . import card_rules, changes, db
from .card_rules import CardError

CARD_DECISION_PREFIX = "card:"
KIND_CARD_STATUS = "card_status"
KIND_CARD_BUILT_FOR = "card_built_for"
KIND_CARD_KIND = "card_kind"
KIND_CARD_FAMILY_MEMBERS = "card_family_members"
CARD_DECISION_KINDS = (KIND_CARD_STATUS, KIND_CARD_BUILT_FOR, KIND_CARD_KIND, KIND_CARD_FAMILY_MEMBERS)

# Patch ops the single-card decision resolver can apply (piece 4 added `link`).
# card_family_members decisions use their own multi-card path
# (_apply_family_patches) with FAMILY_PATCH_OPS.
SUPPORTED_PATCH_OPS = ("set_status", "set_kind", "link")
FAMILY_PATCH_OPS = ("unnest", "add_to_family")

# Computed needs (never stored, always current) that list_needs_decision merges in.
NEED_HOBBY_INACTIVE_WITH_ACTIVE_WORK = "hobby_inactive_with_active_work"
NEED_HOBBY_ACTIVE_UNTOUCHED = "hobby_active_untouched"
NEED_UNTYPED_LINK = "untyped_link"


@dataclass
class Result:
    ok: bool = True
    changes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    batch_id: str | None = None
    dry_run: bool = False

    def to_dict(self):
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "changes": self.changes,
            "warnings": self.warnings,
            "batch_id": self.batch_id,
        }


def card_decision_slug(slug):
    """pending_decisions.post_slug for a card decision (4.3)."""
    return f"{CARD_DECISION_PREFIX}{slug}"


def is_card_decision_slug(post_slug):
    return isinstance(post_slug, str) and post_slug.startswith(CARD_DECISION_PREFIX)


def get_card(card):
    """Resolves a project id or slug to its row, or raises CardError('not_found')."""
    row = db.get_project(card)
    if row is None:
        raise CardError("not_found", f"No such card: {card!r}")
    return row


def _change_rows(slug, before, after):
    return [
        {"card": slug, "field": f, "before": before.get(f), "after": after[f]}
        for f in after
        if before.get(f) != after[f]
    ]


def _status_warnings(card, new_activity, new_stage):
    """Spec 3.2 rule 6: warnings, never errors."""
    warnings = []
    parent = db.get_project(card["parent_id"]) if card.get("parent_id") else None
    if parent is not None and parent.get("activity"):
        if new_activity == "inactive" and parent["activity"] == "active":
            warnings.append(f"'{card['title']}' is now inactive but is part of '{parent['title']}', which is active.")
        elif new_activity == "active" and parent["activity"] == "inactive":
            warnings.append(f"'{card['title']}' is now active but is part of '{parent['title']}', which is inactive.")
    if new_stage == "done":
        busy = [c["title"] for c in db.list_child_projects(card["id"]) if c.get("stage") == "in_progress"]
        if busy:
            warnings.append(f"Marked done while nested card(s) are still in progress: {', '.join(busy[:5])}.")
    return warnings


def set_status(card, stage, stop_reason=None, *, activity=None, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None,
               _op="set_status"):
    """Sets stage (and stop_reason); derives and stores activity. See
    card_rules.validate_status for the rules. Any stage can follow any other."""
    row = get_card(card)
    triple = card_rules.validate_status(row.get("kind") or "project", stage, stop_reason, activity=activity,
                                        whereabouts=row.get("whereabouts"))
    before = {c: row.get(c) for c in triple}
    rows = _change_rows(row["slug"], before, triple)
    warnings = _status_warnings(row, triple["activity"], triple["stage"])
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_card_columns(row["id"], triple, _op, actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def set_kind(card, kind, *, force=False, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Changes a card's kind. Rules (3.1): a group kind (family, collection) must
    not be nested or be a nesting parent; leaving a group kind with members is
    refused unless force=True; the card's current stage must still be valid for
    the new kind (an event can't be in use or paused)."""
    row = get_card(card)
    card_rules.validate_kind(kind)
    old_kind = row.get("kind") or "project"
    if kind in card_rules.GROUP_KINDS:
        if row.get("parent_id"):
            raise CardError("nest_group_kind",
                            f"A {kind} can't be part of another card; take it out of its parent first.")
        if db.list_child_projects(row["id"]):
            raise CardError("nest_group_kind",
                            f"A {kind} can't have nested cards; unnest its children first (use membership instead).")
    if kind in card_rules.GROUP_KINDS and db.list_families_for_member(row["id"]):
        raise CardError("bad_membership",
                        f"'{row['title']}' is a member of a family or collection, and a {kind} can't be a member; "
                        "take it out of those first.")
    members = 0
    if old_kind in card_rules.GROUP_KINDS and kind != old_kind:
        members = db.count_family_members(row["id"])
        if members and not force:
            raise CardError("bad_kind",
                            f"This {old_kind} still has {members} member(s); pass force=True to drop the memberships.",
                            {"members": members})
    if row.get("stage"):
        # The stage must stay legal under the new kind (event restriction).
        card_rules.validate_status(kind, row["stage"], row.get("stop_reason"), whereabouts=row.get("whereabouts"))
    fields = {"kind": kind}
    rows = _change_rows(row["slug"], {"kind": old_kind}, fields)
    warnings = []
    if members and force:
        warnings.append(f"Dropped {members} membership(s).")
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        if members and force:
            db.clear_family_members(row["id"], op="set_kind", actor=actor, batch_id=batch_id,
                                    affected_slugs=[row["slug"]])
        db.update_card_columns(row["id"], fields, "set_kind", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


# --- Nesting ("part of") and families (3.6, 3.7) ----------------------------------

def _slug_of(card_id):
    c = db.get_project(card_id) if card_id is not None else None
    return c["slug"] if c else None


def nest(child, parent, *, replace=False, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
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
        db.update_card_columns(row["id"], {"parent_id": parent_row["id"]}, "nest", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def unnest(child, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None, _op="unnest"):
    """Takes `child` out of its parent (it becomes top-level). No-op when it has none."""
    row = get_card(child)
    old = row.get("parent_id")
    rows = []
    if old is not None:
        rows = [{"card": row["slug"], "field": "parent", "before": _slug_of(old), "after": None}]
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_card_columns(row["id"], {"parent_id": None}, _op, actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def add_to_family(family, member, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None, _op="add_to_family"):
    """Adds `member` to a family or collection (many-to-many; a card can be in
    several). Validated by card_rules.validate_membership. Adding twice is a no-op."""
    fam, mem = get_card(family), get_card(member)
    card_rules.validate_membership(fam, mem)
    exists = db.is_family_member(fam["id"], mem["id"])
    rows = [] if exists else [{"card": mem["slug"], "field": "in_family", "before": None, "after": fam["slug"]}]
    warnings = [f"'{mem['title']}' is already in '{fam['title']}'."] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and not exists:
        db.add_family_member(fam["id"], mem["id"], _op, actor, batch_id=batch_id,
                             affected_slugs=[fam["slug"], mem["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def remove_from_family(family, member, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Takes `member` out of a family or collection. No-op if it wasn't in it.
    Neither card is otherwise changed."""
    fam, mem = get_card(family), get_card(member)
    exists = db.is_family_member(fam["id"], mem["id"])
    rows = [{"card": mem["slug"], "field": "in_family", "before": fam["slug"], "after": None}] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and exists:
        db.remove_family_member(fam["id"], mem["id"], "remove_from_family", actor, batch_id=batch_id,
                                affected_slugs=[fam["slug"], mem["slug"]])
    return Result(True, rows, [], batch_id, dry_run)


def family_fields(card):
    """Membership facts for pages and tools: the families/collections this card is
    in, and (for a group kind) its members."""
    def slim(c):
        return {"id": c["id"], "slug": c["slug"], "title": c["title"], "kind": c.get("kind") or "project",
                "kind_label": card_rules.kind_label(c.get("kind") or "project"), "stage": c.get("stage"),
                "activity": c.get("activity")}
    out = {"families": [slim(f) for f in db.list_families_for_member(card["id"])], "members": []}
    if (card.get("kind") or "project") in card_rules.GROUP_KINDS:
        out["members"] = [slim(m) for m in db.list_family_members(card["id"])]
    return out


# --- Typed links (3.8) -------------------------------------------------------------
# A link row (a, b, type) reads "a <type> b". Directed types are stored once;
# `related` is symmetric and stored twice ((a,b) and (b,a)), exactly as in v1, so
# list_related_projects is unchanged. A pair can't be both typed and related.

def _logical_links(rows):
    """Collapses stored rows into logical links: [{a, b, type}]. The two rows of a
    `related` pair become one entry (a = the alphabetically first slug)."""
    out, seen = [], set()
    for r in rows:
        if r["type"] == "related":
            key = ("related", *sorted((r["slug_a"], r["slug_b"])))
            if key in seen:
                continue
            seen.add(key)
            a, b = sorted((r["slug_a"], r["slug_b"]))
            out.append({"a": a, "b": b, "type": "related", "note": r.get("note") or ""})
        else:
            out.append({"a": r["slug_a"], "b": r["slug_b"], "type": r["type"], "note": r.get("note") or ""})
    return out


def _link_text(link_type, other_slug):
    return f"{link_type} {other_slug}"


def _plan_link(ar, br, link_type, note, existing):
    """Validates and plans "a <link_type> b" against the rows already on the pair
    (`existing`). Returns (deletes, inserts, change_rows, warnings)."""
    verdict = card_rules.validate_link(ar, br, link_type, existing)
    deletes, inserts, warnings = [], [], []
    before = None
    if verdict["drop_related"]:
        deletes = [(r["slug_a"], r["slug_b"], "related") for r in existing if r["type"] == "related"]
        before = _link_text("related", br["slug"])
        warnings.append(f"'{ar['title']}' and '{br['title']}' were related; the typed link replaces that.")
    if link_type == "related":
        inserts = [{"slug_a": ar["slug"], "slug_b": br["slug"], "type": "related", "note": note},
                   {"slug_a": br["slug"], "slug_b": ar["slug"], "type": "related", "note": note}]
    else:
        inserts = [{"slug_a": ar["slug"], "slug_b": br["slug"], "type": link_type, "note": note}]
    rows = [{"card": ar["slug"], "field": "link", "before": before, "after": _link_text(link_type, br["slug"])}]
    return deletes, inserts, rows, warnings


def link(a, b, link_type, note="", *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None, _op="link"):
    """Adds the link "a <link_type> b" (types: card_rules.LINK_TYPES). Directed types
    store one row; `related` stores two. Adding a typed link over a `related` pair
    upgrades it (the related rows are removed in the same transaction); adding
    `related` over a typed pair is refused. CardErrors: bad_link, link_conflict, not_found."""
    ar, br = get_card(a), get_card(b)
    existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
    deletes, inserts, rows, warnings = _plan_link(ar, br, link_type, note or "", existing)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run:
        db.write_project_links(deletes, inserts, _op, actor, batch_id=batch_id,
                               affected_slugs=[ar["slug"], br["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def _plan_unlink(ar, br, link_type, existing):
    """(deletes, change_rows, warnings) for removing links between a pair."""
    if link_type is not None:
        card_rules.validate_link_type(link_type)
    deletes, rows, warnings = [], [], []
    for lg in _logical_links(existing):
        if link_type is not None and lg["type"] != link_type:
            continue
        if lg["type"] == "related":
            deletes += [(ar["slug"], br["slug"], "related"), (br["slug"], ar["slug"], "related")]
            rows.append({"card": ar["slug"], "field": "link", "before": _link_text("related", br["slug"]), "after": None})
        elif link_type is None or (lg["a"] == ar["slug"]):
            deletes.append((lg["a"], lg["b"], lg["type"]))
            rows.append({"card": lg["a"], "field": "link",
                         "before": _link_text(lg["type"], lg["b"]), "after": None})
        else:
            warnings.append(f"'{ar['title']}' {link_type.replace('_', ' ')} '{br['title']}' doesn't exist, but the reverse does "
                            f"('{br['title']}' {link_type.replace('_', ' ')} '{ar['title']}'); pass the cards the other way round.")
    return deletes, rows, warnings


def unlink(a, b, link_type=None, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Removes link(s) between two cards. With `link_type` only that type (for a
    directed type, "a <type> b" exactly; `related` removes both rows); without it,
    every link between the pair in either direction. No-op (with a warning) if
    nothing matched."""
    ar, br = get_card(a), get_card(b)
    existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
    deletes, rows, warnings = _plan_unlink(ar, br, link_type, existing)
    if not rows and not warnings:
        what = f"{link_type} " if link_type else ""
        warnings.append(f"No {what}link between '{ar['title']}' and '{br['title']}'.")
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and deletes:
        db.write_project_links(deletes, [], "unlink", actor, batch_id=batch_id,
                               affected_slugs=[ar["slug"], br["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def _plan_retype(ar, br, from_type, to_type, existing, note=None):
    """(deletes, inserts, change_rows, warnings) for turning the `from_type` link
    on this pair into "a <to_type> b". The old link is found in either direction;
    the new one always reads a -> b. Raises not_found when there is no such link."""
    card_rules.validate_link_type(from_type)
    card_rules.validate_link_type(to_type)
    if from_type == to_type:
        raise CardError("bad_link", f"The link is already {from_type!r}; pick a different type.")
    old = [r for r in existing if r["type"] == from_type]
    if not old:
        raise CardError("not_found", f"No {from_type!r} link between '{ar['title']}' and '{br['title']}'.")
    remaining = [r for r in existing if r["type"] != from_type]
    deletes = [(r["slug_a"], r["slug_b"], r["type"]) for r in old]
    carried = note if note is not None else (old[0].get("note") or "")
    verdict_deletes, inserts, rows, warnings = _plan_link(ar, br, to_type, carried, remaining)
    # `remaining` excludes the old rows and a pair never holds related + typed rows together,
    # so the validator can't ask to drop anything extra here: verdict_deletes is always empty.
    deletes += verdict_deletes
    rows = [{"card": ar["slug"], "field": "link", "before": _link_text(from_type, br["slug"]),
             "after": _link_text(to_type, br["slug"])}]
    warnings = [w for w in warnings if "were related" not in w]
    return deletes, inserts, rows, warnings


def retype_link(a, b, from_type, to_type, *, note=None, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """The single upgrade/downgrade path (3.8): replaces the `from_type` link on the
    pair (found in either direction) with "a <to_type> b", in one transaction. The
    note carries over unless one is given. CardErrors: not_found (no such link),
    bad_link, link_conflict."""
    ar, br = get_card(a), get_card(b)
    existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
    deletes, inserts, rows, warnings = _plan_retype(ar, br, from_type, to_type, existing, note)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run:
        db.write_project_links(deletes, inserts, "retype_link", actor, batch_id=batch_id,
                               affected_slugs=[ar["slug"], br["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def retype_links(mapping, *, dry_run=True, partial_ok=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Bulk retype (7.3), for clearing v1's untyped `related` links. `mapping` is a
    list of {a, b, to_type, from_type='related'}. DRY-RUN BY DEFAULT: nothing is
    written unless dry_run=False. Validates every item first; all-or-nothing in one
    transaction and one batch_id (partial_ok=True applies the valid items and
    reports the rest). Returns {ok, dry_run, changes, warnings, batch_id, applied,
    items: [{a, b, to_type, ok, changes, error?}]}. Never raises for item errors."""
    batch_id = batch_id or changes.new_batch_id()
    items, deletes, inserts, all_rows, seen_pairs, slugs = [], [], [], [], set(), []
    for m in mapping or []:
        item = {"a": m.get("a"), "b": m.get("b"), "to_type": m.get("to_type"),
                "from_type": m.get("from_type") or "related"}
        try:
            ar, br = get_card(item["a"]), get_card(item["b"])
            pair = frozenset((ar["slug"], br["slug"]))
            if pair in seen_pairs:
                raise CardError("link_conflict", "This pair appears more than once in the mapping.",
                                {"reason": "duplicate_item"})
            existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
            d, i, rows, _w = _plan_retype(ar, br, item["from_type"], item["to_type"], existing)
            seen_pairs.add(pair)
            item.update(ok=True, changes=rows)
            deletes += d
            inserts += i
            all_rows += rows
            slugs += [ar["slug"], br["slug"]]
        except CardError as e:
            item.update(ok=False, changes=[], error=e.to_dict())
        items.append(item)
    failed = [it for it in items if not it["ok"]]
    ok = not failed or partial_ok
    applied = 0
    if ok and not dry_run and (deletes or inserts):
        db.write_project_links(deletes, inserts, "retype_links", actor, batch_id=batch_id,
                               affected_slugs=sorted(set(slugs)))
        applied = len([it for it in items if it["ok"]])
    warnings = [f"{len(failed)} item(s) can't be retyped; "
                + ("the valid ones are applied." if partial_ok and not dry_run else "nothing was written.")] if failed else []
    return {"ok": bool(ok), "dry_run": dry_run, "changes": all_rows, "warnings": warnings,
            "batch_id": batch_id, "applied": applied, "items": items}


def list_links(card):
    """Every link touching `card`, both directions, with labels (3.8):
    [{slug, title, kind, stage, type, direction: out|in|both, label, note}].
    `out` = this card is the source ("this <type> that"), `in` = it is the target,
    `both` = symmetric (`related`). Ordered by type (card_rules.LINK_TYPES), then title."""
    row = get_card(card)
    rows = db.list_project_link_rows(slug=row["slug"])
    cache, out = {}, []

    def other(slug):
        if slug not in cache:
            cache[slug] = db.get_project(slug)
        return cache[slug]

    for lg in _logical_links(rows):
        if lg["type"] == "related":
            direction, o_slug = "both", lg["b"] if lg["a"] == row["slug"] else lg["a"]
        elif lg["a"] == row["slug"]:
            direction, o_slug = "out", lg["b"]
        else:
            direction, o_slug = "in", lg["a"]
        o = other(o_slug)
        if o is None:
            continue
        out.append({"slug": o["slug"], "title": o["title"], "kind": o.get("kind") or "project",
                    "stage": o.get("stage"), "type": lg["type"], "direction": direction,
                    "label": card_rules.link_label(lg["type"], direction), "note": lg["note"]})
    order = {t: i for i, t in enumerate(card_rules.LINK_TYPES)}
    out.sort(key=lambda r: (order.get(r["type"], 99), r["direction"] != "out", r["title"].lower()))
    return out


_BUILT_FOR_RE = r"built (?:for|to fit|to go on|to suit)\b[^.\n]{0,40}?"
_INSPIRED_RE = r"inspired by\b[^.\n]{0,40}?"
_SOFTWARE_RE = re.compile(r"\b(code|script|software|firmware|app|lua|library|design|tool)\b", re.I)


def untyped_link_needs(kind=None, hobby_ids=None):
    """Computed need `untyped_link` (never stored): v1 `related` pairs where a typed
    reading is plausible, with the suggested type and direction. Heuristics, all
    deterministic and never applied: a card's text says 'built for' / 'inspired by'
    followed by the other card's title; a software/design card nested under the
    other (applies_to). Row: {need, card_slug (the source), title, detail,
    suggested ('<type>'), link: {a, b, type}, decision_id: None, options}."""
    related = [r for r in db.list_project_link_rows() if r["type"] == "related" and r["slug_a"] < r["slug_b"]]
    if not related:
        return []
    text_cache = {}

    def text_of(c):
        if c["slug"] not in text_cache:
            body = ""
            if c.get("writeup_slug"):
                w = db.get_by_slug(c["writeup_slug"])
                if w:
                    body = (w.get("type_metadata") or {}).get("body", "") or ""
            text_cache[c["slug"]] = f"{c.get('description') or ''} {body}"
        return text_cache[c["slug"]]

    def reading(x, y):
        """A typed reading of 'x <type> y', or None."""
        t = text_of(x)
        title = re.escape((y["title"] or "").strip())
        if len(title) >= 4:
            if re.search(_BUILT_FOR_RE + title, t, re.I):
                return "built_for", f"'{x['title']}' says it was built for '{y['title']}'"
            if re.search(_INSPIRED_RE + title, t, re.I):
                return "inspired_by", f"'{x['title']}' says it was inspired by '{y['title']}'"
        if x.get("parent_id") == y["id"] and _SOFTWARE_RE.search(f"{x['title']} {x.get('description') or ''}"):
            return "applies_to", f"'{x['title']}' is nested under '{y['title']}' and reads like software or a design"
        return None

    rows = []
    for r in related:
        p, q = db.get_project(r["slug_a"]), db.get_project(r["slug_b"])
        if p is None or q is None:
            continue
        for x, y in ((p, q), (q, p)):
            found = reading(x, y)
            if not found:
                continue
            if kind and (x.get("kind") or "project") != kind:
                break
            if hobby_ids is not None and x["id"] not in hobby_ids:
                break
            t, why = found
            rows.append({
                "need": NEED_UNTYPED_LINK,
                "card_slug": x["slug"],
                "title": x["title"],
                "detail": f"'{x['title']}' is just 'related' to '{y['title']}'. Reads like: {t.replace('_', ' ')}.",
                "suggested": t,
                "suggested_reason": why,
                "confidence": "low",
                "link": {"a": x["slug"], "b": y["slug"], "type": t},
                "decision_id": None,
                "options": [{"key": k, "label": card_rules.LINK_LABELS[k][0]} for k in card_rules.DIRECTED_LINK_TYPES],
            })
            break
    return rows


# --- Reading a card's live status ------------------------------------------------

def open_card_decisions(card_slug):
    """Open (unresolved) card_* decisions for one card."""
    return [d for d in db.list_all_pending_decisions(kind_prefix="card_", post_slug=card_decision_slug(card_slug))
            if d["resolved_at"] is None]


def provisional_legacy_status(card):
    """The legacy v1 status word recorded on an open status/built-for decision,
    but only while the card still carries exactly the provisional value the
    migration applied (once anything edits it, it's no longer a guess)."""
    if not card.get("stage"):
        return None
    for d in open_card_decisions(card["slug"]):
        if d["kind"] not in (KIND_CARD_STATUS, KIND_CARD_BUILT_FOR):
            continue
        prov = d["payload"].get("provisional") or {}
        legacy = d["payload"].get("legacy_status")
        if legacy and prov.get("stage") == card.get("stage") and prov.get("stop_reason") == card.get("stop_reason"):
            return legacy
    return None


def curator_status_for(card):
    """The v1-vocabulary status the Curator scores a card by (spec 4.1)."""
    return card_rules.curator_status(card, provisional_legacy=provisional_legacy_status(card))


def status_fields(card):
    """The live status fields in the shape every API/page returns, plus labels."""
    stage = card.get("stage")
    return {
        "kind": card.get("kind") or "project",
        "kind_label": card_rules.kind_label(card.get("kind") or "project"),
        "activity": card.get("activity"),
        "stage": stage,
        "stage_label": card_rules.stage_label(stage),
        "stop_reason": card.get("stop_reason"),
        "stop_reason_label": card_rules.STOP_REASON_LABELS.get(card.get("stop_reason")),
        "needs_input": bool(open_card_decisions(card["slug"])) if card.get("slug") else False,
    }


# --- Decisions ------------------------------------------------------------------

def _decision_card(decision):
    slug = decision["post_slug"][len(CARD_DECISION_PREFIX):]
    return db.get_project(slug)


def _apply_patch(card_row, patch, actor, batch_id):
    """Runs one decision option's patch ops through the normal validators.
    On a mid-way failure, restores the card's original columns and re-raises so
    the decision stays open and nothing is half-applied."""
    original = {c: card_row.get(c) for c in ("kind", "activity", "stage", "stop_reason")}
    # Pre-flight: every op must be one we can run today.
    for op in patch:
        if op.get("op") not in SUPPORTED_PATCH_OPS:
            raise CardError("unsupported_patch",
                            f"This answer needs '{op.get('op')}', which isn't a step this question can run. "
                            "The question stays open.")
    applied = []
    try:
        for op in patch:
            if op["op"] == "set_status":
                set_status(card_row["id"], op["stage"], op.get("stop_reason"), actor=actor, batch_id=batch_id,
                           _op="resolve_decision")
            elif op["op"] == "set_kind":
                set_kind(card_row["id"], op["kind"], actor=actor, batch_id=batch_id)
            elif op["op"] == "link":
                link(op.get("a") or card_row["slug"], op["b"], op["type"], op.get("note") or "",
                     actor=actor, batch_id=batch_id, _op="resolve_decision")
            applied.append(op)
    except CardError:
        if applied:
            db.update_card_columns(card_row["id"], original, "resolve_decision_rollback", actor, batch_id=batch_id)
        raise


def _apply_family_patches(family_row, patches_by_option, actor, batch_id):
    """card_family_members resolution (4.5). The chosen options' patch ops are
    pooled and run in a safe order: unnest the chosen children, turn the card into
    a family (if it isn't one), then add the members. The order matters because a
    card with nested children can't become a family, and a non-family can't take
    members. Everything is validated up front so a refusal leaves nothing
    half-applied (the decision stays open and the CardError propagates)."""
    ops = [op for patch in patches_by_option for op in patch]
    for op in ops:
        if op.get("op") not in FAMILY_PATCH_OPS:
            raise CardError("unsupported_patch", f"Unsupported answer step: {op.get('op')!r}.")
    unnests = [op for op in ops if op["op"] == "unnest"]
    adds = [op for op in ops if op["op"] == "add_to_family"]
    fam = db.get_project(family_row["id"]) or family_row
    is_group = (fam.get("kind") or "project") in card_rules.GROUP_KINDS

    unnest_ids = set()
    for op in unnests:
        unnest_ids.add(get_card(op["card"])["id"])
    prospective = {**fam, "kind": fam["kind"] if is_group else "family"}
    if not is_group:
        remaining = [c["title"] for c in db.list_child_projects(fam["id"]) if c["id"] not in unnest_ids]
        if remaining:
            raise CardError(
                "nest_group_kind",
                f"'{fam['title']}' can't become a family while {', '.join(remaining[:5])} are still nested under it. "
                "Choose them as members too, or unnest them first.")
        if fam.get("parent_id") is not None:
            raise CardError("nest_group_kind",
                            f"'{fam['title']}' is part of another card; a family can't be nested. Unnest it first.")
        if db.list_families_for_member(fam["id"]):
            raise CardError("bad_membership",
                            f"'{fam['title']}' is a member of another family, and a family can't be a member.")
    members = []
    for op in adds:
        m = get_card(op["member"])
        card_rules.validate_membership(prospective, m)
        members.append(m)

    for op in unnests:
        child = get_card(op["card"])
        if child.get("parent_id") == fam["id"]:
            unnest(child["id"], actor=actor, batch_id=batch_id, _op="resolve_decision")
    if not is_group:
        set_kind(fam["id"], "family", actor=actor, batch_id=batch_id)
    for m in members:
        add_to_family(fam["id"], m["id"], actor=actor, batch_id=batch_id, _op="resolve_decision")


def resolve_decision(decision_id, choice=None, choices=None, actor=changes.ACTOR_MCP):
    """Resolves one card_* decision. `choice` is an option key (single-choice
    questions); `choices` is a list of option keys (multi-choice questions, e.g.
    several candidate cards). The option's declarative `patch` ops run through the
    same validators a manual edit uses, in one batch; if the patch is invalid on
    today's data the decision stays open and the CardError propagates.

    Returns {"ok", "applied": [keys], "batch_id", "remaining"}.
    """
    from . import decisions as _decisions  # exceptions live there; lazy (it imports this module)

    decision = db.get_pending_decision(decision_id)
    if decision is None:
        raise _decisions.DecisionNotFound(f"No such pending decision: {decision_id}")
    if decision["resolved_at"] is not None:
        raise _decisions.DecisionAlreadyResolved(f"Decision {decision_id} already resolved")
    if decision["kind"] not in CARD_DECISION_KINDS or not is_card_decision_slug(decision["post_slug"]):
        raise _decisions.UnknownDecisionKind(f"Not a card decision: {decision['kind']}")

    payload = decision["payload"]
    options = {o["key"]: o for o in payload.get("options", [])}
    picked = list(choices or [])
    if choice:
        picked.append(choice)
    if not picked:
        raise _decisions.InvalidChoice(f"Pick an answer: one of {sorted(options)}")
    for key in picked:
        if key not in options:
            raise _decisions.InvalidChoice(f"'{key}' is not one of this question's options: {sorted(options)}")

    card_row = _decision_card(decision)
    if card_row is None:
        db.resolve_pending_decision(decision_id, {"stale": "card deleted"})
        raise _decisions.DecisionNotFound(f"The card for decision {decision_id} no longer exists")

    batch_id = changes.new_batch_id()
    if decision["kind"] == KIND_CARD_FAMILY_MEMBERS:
        _apply_family_patches(card_row, [options[k].get("patch") or [] for k in picked], actor, batch_id)
    else:
        # Pre-flight every picked answer's link steps (dry run) so a bad one refuses
        # the whole resolve before any earlier answer has been applied.
        for key in picked:
            for op in options[key].get("patch") or []:
                if op.get("op") == "link":
                    link(op.get("a") or card_row["slug"], op["b"], op["type"], op.get("note") or "", dry_run=True)
        for key in picked:
            _apply_patch(card_row, options[key].get("patch") or [], actor, batch_id)
            card_row = db.get_project(card_row["id"]) or card_row
    db.resolve_pending_decision(decision_id, {
        "choice": picked[0] if len(picked) == 1 else picked,
        "applied_batch": batch_id,
        "by": "owner" if actor == changes.ACTOR_UI else "claude",
        "at": time.time(),
    })
    return {"ok": True, "applied": picked, "batch_id": batch_id, "remaining": db.count_pending_decisions()}


def decision_summary(decision):
    """Flattened view of one open card decision for lists (MCP + web)."""
    p = decision["payload"]
    slug = decision["post_slug"][len(CARD_DECISION_PREFIX):]
    return {
        "decision_id": decision["id"],
        "need": decision["kind"],
        "card_slug": slug,
        "title": p.get("title") or slug,
        "field": p.get("field"),
        "question": p.get("question"),
        "legacy_status": p.get("legacy_status"),
        "provisional": p.get("provisional"),
        "suggested": p.get("suggested"),
        "suggested_reason": p.get("suggested_reason"),
        "confidence": p.get("confidence"),
        "options": [{"key": o["key"], "label": o.get("label", o["key"])} for o in p.get("options", [])],
    }


# --- Hobbies (3.3, 3.9) ----------------------------------------------------------

def get_hobby(hobby):
    """Resolves a hobby id or slug to its blog_tags row, or raises CardError('not_found')."""
    row = db.get_hobby(hobby)
    if row is None:
        raise CardError("not_found", f"No such hobby: {hobby!r}")
    return row


def hobby_flags(hobby):
    """The computed mismatch flags for one hobby (never stored). See db.hobby_flags."""
    return db.hobby_flags(get_hobby(hobby)["id"])


def hobby_fields(hobby_row, with_flags=True):
    """A hobby in the shape every API/page returns: activity (as `status`, the key the
    v1 callers already read), label, group_code, and the computed flags."""
    status = hobby_row.get("hobby_status") or "active"
    out = {
        "id": hobby_row["id"],
        "name": hobby_row["name"],
        "slug": hobby_row["slug"],
        "status": status,
        "status_label": card_rules.HOBBY_ACTIVITY_LABELS.get(status, status),
        "group_code": hobby_row.get("group_code"),
    }
    if "project_count" in hobby_row:
        out["project_count"] = hobby_row["project_count"]
    if with_flags:
        out["flags"] = db.hobby_flags(hobby_row["id"])
    return out


def set_hobby_activity(hobby, value, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Sets a hobby's manual Active/Inactive switch (3.3) and logs it. `dormant` and
    `abandoned` are deprecated aliases for `inactive` (the result carries a warning).
    Setting it never writes a flag: flags are computed on read."""
    row = get_hobby(hobby)
    activity, warnings = card_rules.validate_hobby_activity(value)
    rows = _change_rows(row["slug"], {"hobby_status": row.get("hobby_status")}, {"hobby_status": activity})
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_hobby_columns(row["id"], {"hobby_status": activity}, "set_hobby_activity", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def set_group_code(hobby, code, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Owner-editable hobby code (3.9): 2-4 letters/digits, unique across hobbies."""
    row = get_hobby(hobby)
    code = card_rules.validate_group_code(code)
    if code in {c.upper() for c in db.all_group_codes(exclude_tag_id=row["id"])}:
        raise CardError("group_code_conflict", f"The code {code} is already used by another hobby.")
    rows = _change_rows(row["slug"], {"group_code": row.get("group_code")}, {"group_code": code})
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_hobby_columns(row["id"], {"group_code": code}, "set_group_code", actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def hobby_needs():
    """Computed hobby needs for list_needs_decision (never stored, always current):
    hobby_inactive_with_active_work and hobby_active_untouched."""
    need_of = {
        card_rules.HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK: NEED_HOBBY_INACTIVE_WITH_ACTIVE_WORK,
        card_rules.HOBBY_FLAG_ACTIVE_UNTOUCHED: NEED_HOBBY_ACTIVE_UNTOUCHED,
    }
    rows = []
    for h in db.list_hobbies():
        for flag in db.hobby_flags(h["id"]):
            if flag["code"] == card_rules.HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK:
                suggested = "active"
                reason = "It has active work in it; the switch probably should be Active."
            else:
                suggested = "inactive"
                reason = "Nothing in it has been touched for about 2 years."
            rows.append({
                "need": need_of[flag["code"]],
                "card_slug": None,
                "hobby_slug": h["slug"],
                "title": h["name"],
                "detail": f"{flag['label']}. {flag['detail']}",
                "suggested": suggested,
                "suggested_reason": reason,
                "confidence": "low",
                "decision_id": None,
                "options": [{"key": "active", "label": "Active"}, {"key": "inactive", "label": "Inactive"}],
            })
    return rows


def list_needs_decision(kind=None, need=None, hobby=None, limit=None):
    """Open card questions for the owner (spec 6): the STORED card_* decisions plus
    the computed needs from the pieces that exist so far (piece 2: the two hobby
    flags; missing provenance, untyped links, ... join in with their pieces).

    Row: {need, card_slug, title, detail, suggested|None, decision_id|None, ...}.
    Filters: `kind` (the card's kind), `need` (the decision kind), `hobby`
    (hobby slug or id), `limit`. Sorted by card title.
    """
    hobby_ids = None
    if hobby not in (None, ""):
        h = db.get_hobby(hobby)
        hobby_ids = {p["id"] for p in db.list_projects_for_hobby(h["id"])} if h else set()
    rows = []
    for d in db.list_all_pending_decisions(kind_prefix="card_"):
        if d["resolved_at"] is not None or not is_card_decision_slug(d["post_slug"]):
            continue
        if need and d["kind"] != need:
            continue
        s = decision_summary(d)
        card_row = db.get_project(s["card_slug"])
        if card_row is None:
            continue
        if kind and (card_row.get("kind") or "project") != kind:
            continue
        if hobby_ids is not None and card_row["id"] not in hobby_ids:
            continue
        rows.append({
            "need": s["need"],
            "card_slug": s["card_slug"],
            "title": card_row["title"],
            "detail": s["question"],
            "suggested": s["suggested"],
            "suggested_reason": s["suggested_reason"],
            "confidence": s["confidence"],
            "decision_id": s["decision_id"],
            "options": s["options"],
        })
    # Computed hobby needs (3.3). They belong to a hobby, not a project card, so a
    # `kind` filter other than 'hobby' leaves them out; `hobby` narrows to one hobby.
    if kind in (None, "", "hobby"):
        picked = db.get_hobby(hobby) if hobby not in (None, "") else None
        for r in hobby_needs():
            if need and r["need"] != need:
                continue
            if hobby not in (None, "") and (picked is None or r["hobby_slug"] != picked["slug"]):
                continue
            rows.append(r)
    # Computed untyped_link (3.8): v1 'related' pairs with a plausible typed reading.
    if not need or need == NEED_UNTYPED_LINK:
        rows += untyped_link_needs(kind=kind if kind not in (None, "", "hobby") else None, hobby_ids=hobby_ids)             if kind != "hobby" else []
    rows.sort(key=lambda r: (r["title"].lower(), r["need"]))
    return rows[:limit] if limit else rows
