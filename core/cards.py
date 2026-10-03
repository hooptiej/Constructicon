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

import json
import re
import time
from dataclasses import dataclass, field

from . import card_rules, changes, db, timeline
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
NEED_STATUS_CONFLICT = "status_conflict"
NEED_BLANK_WRITEUP_WITH_FILES = "blank_writeup_with_files"


@dataclass
class Result:
    ok: bool = True
    changes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    batch_id: str | None = None
    dry_run: bool = False
    data: dict = field(default_factory=dict)  # extra keys an operation returns (e.g. split's new cards)

    def to_dict(self):
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "changes": self.changes,
            "warnings": self.warnings,
            "batch_id": self.batch_id,
            **self.data,
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
    if row.get("whereabouts"):
        # Whereabouts doesn't apply to an action / event / family (3.4).
        card_rules.validate_whereabouts(kind, row["whereabouts"], row.get("stage"))
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


# --- Whereabouts, card provenance, highlight (3.4, 3.5, 3.12) ---------------------

def set_whereabouts(card, whereabouts=None, note=..., *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Sets (or, with whereabouts=None, clears) where the physical thing is now, plus
    an optional free-text note (note=... leaves the note alone; '' / None clears it).
    Validated by card_rules.validate_whereabouts (bad_whereabouts): applicable kinds
    only, and the in_use / never_built cross-rules against the card's current stage."""
    row = get_card(card)
    value = card_rules.validate_whereabouts(row.get("kind") or "project", whereabouts, row.get("stage")) \
        if whereabouts not in (None, "") else None
    fields = {"whereabouts": value}
    if note is not ...:
        fields["whereabouts_note"] = (note or "").strip() or None
    rows = _change_rows(row["slug"], {c: row.get(c) for c in fields}, fields)
    warnings = []
    if value is None and row.get("whereabouts_note") and note is ...:
        warnings.append("Whereabouts cleared; the note was kept.")
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_card_columns(row["id"], fields, "set_whereabouts", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def set_provenance(card, provenance=None, credit=..., *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Sets (or clears, with None) the CARD's provenance and optionally its credit
    (who designed it / where it came from; credit=... leaves it alone). Distinct from
    the per-file provenance (db.set_provenance / constructicon_set_provenance), which
    is untouched. One value per card: mixed origins are separate cards (3.5)."""
    row = get_card(card)
    value = card_rules.validate_provenance(provenance, current=row.get("provenance"))
    fields = {"provenance": value}
    if credit is not ...:
        fields["provenance_credit"] = (credit or "").strip() or None
    rows = _change_rows(row["slug"], {c: row.get(c) for c in fields}, fields)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_card_columns(row["id"], fields, "set_provenance", actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def set_highlight(card, on, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """The card's own 0/1 "this one is special" flag (3.12). Independent of the
    per-file highlight (capture_events.highlight), which is untouched."""
    row = get_card(card)
    fields = {"highlight": 1 if on else 0}
    rows = _change_rows(row["slug"], {"highlight": row.get("highlight") or 0}, fields)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_card_columns(row["id"], fields, "set_highlight", actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def suggest_provenance(card):
    """Majority file provenance mapped through the 3.5 table, or None. A suggestion
    needs >50% of the card's non-write-up files to map to one card value (so
    `documented` / unset files dilute it). Returns {value, share, files, counted}
    or None. Never applied."""
    items = [i for i in db.list_project_items(card["id"]) if i["slug"] != card.get("writeup_slug")]
    if not items:
        return None
    votes = {}
    for i in items:
        mapped = card_rules.FILE_PROVENANCE_TO_CARD.get(i.get("provenance"))
        if mapped:
            votes[mapped] = votes.get(mapped, 0) + 1
    if not votes:
        return None
    value, n = max(votes.items(), key=lambda kv: kv[1])
    if n * 2 <= len(items):
        return None
    return {"value": value, "files": n, "counted": len(items), "share": round(n / len(items), 3)}


def whereabouts_fields(card):
    """Whereabouts + provenance + highlight as pages and tools return them."""
    w, p = card.get("whereabouts"), card.get("provenance")
    return {
        "whereabouts": w,
        "whereabouts_label": card_rules.whereabouts_label(w) if w else None,
        "whereabouts_note": card.get("whereabouts_note"),
        "whereabouts_applies": (card.get("kind") or "project") in card_rules.WHEREABOUTS_KINDS,
        "provenance": p,
        "provenance_label": card_rules.card_provenance_label(p) if p else None,
        "provenance_credit": card.get("provenance_credit"),
        "highlight": bool(card.get("highlight")),
    }


NEED_MISSING_PROVENANCE = "missing_provenance"
NEED_MISSING_PROVENANCE_CREDIT = "missing_provenance_credit"
NEED_MISSING_WHEREABOUTS = "missing_whereabouts"
# Provenances where "who made it / where it came from" is worth a credit.
CREDIT_PROVENANCE = ("found", "collected")


def provenance_whereabouts_needs(kind=None, hobby_ids=None):
    """Computed needs (never stored): missing_provenance (every non-family card with
    no provenance; carries the majority-file suggestion when there is one),
    missing_provenance_credit (found / collected card with no credit) and
    missing_whereabouts (a Thing with no whereabouts)."""
    from . import provenance_options  # lazy, as elsewhere in this module
    prov_options = provenance_options.list_options("card")  # #529: the live, editable list
    rows = []
    for c in db.list_projects():
        ck = c.get("kind") or "project"
        if (kind and ck != kind) or (hobby_ids is not None and c["id"] not in hobby_ids):
            continue
        prov = c.get("provenance")
        if not prov and ck != "family":
            sug = suggest_provenance(c)
            rows.append({
                "need": NEED_MISSING_PROVENANCE, "card_slug": c["slug"], "title": c["title"],
                "detail": "No provenance recorded (" + ", ".join(o["label"].lower() for o in prov_options) + ").",
                "suggested": sug["value"] if sug else None,
                "suggested_reason": (f"{sug['files']} of {sug['counted']} files map to "
                                     f"{card_rules.card_provenance_label(sug['value'])}.") if sug else None,
                "confidence": ("medium" if sug and sug["share"] >= 0.8 else "low") if sug else None,
                "decision_id": None,
                "options": [{"key": o["key"], "label": o["label"]} for o in prov_options],
            })
        elif prov in CREDIT_PROVENANCE and not (c.get("provenance_credit") or "").strip():
            rows.append({
                "need": NEED_MISSING_PROVENANCE_CREDIT, "card_slug": c["slug"], "title": c["title"],
                "detail": f"{card_rules.card_provenance_label(prov)} card with no credit (who designed it / where it came from).",
                "suggested": None, "suggested_reason": None, "confidence": None,
                "decision_id": None, "options": [],
            })
        if ck == "thing" and not c.get("whereabouts"):
            rows.append({
                "need": NEED_MISSING_WHEREABOUTS, "card_slug": c["slug"], "title": c["title"],
                "detail": "No whereabouts recorded (have it, partial, parted out, sold, gifted, lost, never built).",
                "suggested": None, "suggested_reason": None, "confidence": None,
                "decision_id": None,
                "options": [{"key": k, "label": card_rules.WHEREABOUTS_LABELS[k]} for k in card_rules.WHEREABOUTS],
            })
    return rows


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


def resolve_decision(decision_id, choice=None, choices=None, actor=changes.ACTOR_MCP, batch_id=None):
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

    batch_id = batch_id or changes.new_batch_id()
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
    }, log={"op": "resolve_decision", "actor": actor, "batch_id": batch_id})
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


def list_needs_decision(kind=None, need=None, hobby=None, limit=None, card=None):
    """Open card questions for the owner (spec 6): the STORED card_* decisions plus
    the computed needs from the pieces that exist so far (piece 2: the two hobby
    flags; missing provenance, untyped links, ... join in with their pieces).

    Row: {need, card_slug, title, detail, suggested|None, decision_id|None, ...}.
    Filters: `kind` (the card's kind), `need` (the decision kind), `hobby`
    (hobby slug or id), `limit`, `card` (one card's slug or id). Sorted by card title.
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
    # Computed provenance / whereabouts needs (3.4, 3.5; piece 5).
    if kind != "hobby" and (not need or need in (NEED_MISSING_PROVENANCE, NEED_MISSING_PROVENANCE_CREDIT,
                                                  NEED_MISSING_WHEREABOUTS)):
        rows += [r for r in provenance_whereabouts_needs(kind=kind or None, hobby_ids=hobby_ids)
                 if not need or r["need"] == need]
    # Computed status_conflict / blank_writeup_with_files (3.2 rule 6, 3.11; piece 6).
    if kind != "hobby" and (not need or need in (NEED_STATUS_CONFLICT, NEED_BLANK_WRITEUP_WITH_FILES)):
        rows += [r for r in status_and_writeup_needs(kind=kind or None, hobby_ids=hobby_ids)
                 if not need or r["need"] == need]
    if card not in (None, ""):
        picked_card = db.get_project(card)
        rows = [r for r in rows if picked_card is not None and r.get("card_slug") == picked_card["slug"]]
    rows.sort(key=lambda r: (r["title"].lower(), r["need"]))
    return rows[:limit] if limit else rows


# =====================================================================================
# Piece 6: reorganizing toolkit (spec sections 3.10, 3.13, 6). Composite operations run
# inside db.transaction(): one atomic unit, every write imaged in the change log under
# one batch_id, and `dry_run` is the same code path rolled back (so a preview can't
# disagree with the real thing).
# =====================================================================================

def _slim(c):
    return {"id": c["id"], "slug": c["slug"], "title": c["title"], "kind": c.get("kind") or "project",
            "kind_label": card_rules.kind_label(c.get("kind") or "project"), "stage": c.get("stage"),
            "activity": c.get("activity")}


# --- Home (3.10) -----------------------------------------------------------------

def _home_text(kind, ref):
    if kind == "card":
        return f"card:{_slug_of(ref) or ref}"
    if kind == "hobby":
        h = db.get_hobby(ref)
        return f"hobby:{h['slug'] if h else ref}"
    return None


def _parse_home_target(target):
    """-> (home_kind, row). `target`: a card id (int), a dict {type|kind, ref|id|slug},
    or a string: 'card:<ref>', 'hobby:<ref>', or a bare card ref (a bare ref that is no
    card is tried as a hobby)."""
    if isinstance(target, dict):
        kind = target.get("type") or target.get("kind")
        ref = target.get("ref", target.get("id", target.get("slug")))
        card_rules.validate_home_kind(kind)
    elif isinstance(target, str) and ":" in target:
        kind, _, ref = target.partition(":")
        card_rules.validate_home_kind(kind)
    else:
        kind, ref = None, target
    if kind in (None, "card"):
        row = db.get_project(ref)
        if row is not None:
            return "card", row
        if kind == "card":
            raise CardError("not_found", f"No such card: {ref!r}")
    hob = db.get_hobby(ref)
    if hob is None:
        raise CardError("not_found", f"No such card or hobby: {ref!r}")
    return "hobby", hob


def set_home(card, target=None, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Overrides a card's automatic home (3.10), or with target=None clears the override
    so the home goes back to automatic (parent, then first family, then first hobby).
    `target` is a card or a hobby (see _parse_home_target). A card can't be its own home."""
    row = get_card(card)
    if target in (None, ""):
        fields = {"home_kind": None, "home_ref": None}
    else:
        kind, target_row = _parse_home_target(target)
        if kind == "card" and target_row["id"] == row["id"]:
            raise CardError("bad_home", "A card can't be its own home.")
        fields = {"home_kind": kind, "home_ref": target_row["id"]}
    before_text = _home_text(row.get("home_kind"), row.get("home_ref")) if row.get("home_kind") else None
    after_text = _home_text(fields["home_kind"], fields["home_ref"]) if fields["home_kind"] else None
    rows = []
    if (row.get("home_kind"), row.get("home_ref")) != (fields["home_kind"], fields["home_ref"]):
        rows = [{"card": row["slug"], "field": "home", "before": before_text, "after": after_text}]
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db.update_card_columns(row["id"], fields, "set_home", actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def _home_entry(kind, row, source, dangling=False):
    return {"type": kind, "id": row["id"], "slug": row["slug"],
            "title": row.get("title") or row.get("name"), "source": source, "dangling_override": dangling}


def resolve_home(card):
    """The card's home (3.10): {type: card|hobby|None, id, slug, title, source:
    override|parent|family|hobby|none, dangling_override}. A valid manual override wins;
    else the parent; else the earliest family membership; else the earliest hobby;
    else none (the home page). A dangling override (its target was deleted) falls
    through to the automatic default and is flagged, never raised."""
    row = get_card(card)
    dangling = False
    kind, ref = row.get("home_kind"), row.get("home_ref")
    if kind and ref is not None:
        target = db.get_project(ref) if kind == "card" else (db.get_hobby(ref) if kind == "hobby" else None)
        if target is not None and not (kind == "card" and target["id"] == row["id"]):
            return _home_entry(kind, target, "override")
        dangling = True
    if row.get("parent_id"):
        parent = db.get_project(row["parent_id"])
        if parent is not None:
            return _home_entry("card", parent, "parent", dangling)
    fams = db.list_families_for_member(row["id"])
    if fams:
        return _home_entry("card", fams[0], "family", dangling)
    for hobby_row in db.list_hobby_rows(row["id"]):
        hob = db.get_hobby(hobby_row["hobby_tag_id"])
        if hob is not None:
            return _home_entry("hobby", hob, "hobby", dangling)
    return {"type": None, "id": None, "slug": None, "title": None, "source": "none", "dangling_override": dangling}


def home_chain(card, limit=12):
    """The breadcrumb: the card's home, then that home's own home, and so on, nearest
    first, ending at a hobby or the top. Cycle-safe."""
    row = get_card(card)
    chain, seen = [], {row["id"]}
    current = row
    while len(chain) < limit:
        h = resolve_home(current["id"])
        if h["type"] is None:
            break
        chain.append(h)
        if h["type"] != "card" or h["id"] in seen:
            break
        seen.add(h["id"])
        current = db.get_project(h["id"])
        if current is None:
            break
    return chain


# --- Hobby membership under the new names (3.9) ----------------------------------

def add_to_hobby(card, hobby, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Adds a card to a hobby (many allowed; adding twice is a no-op)."""
    row, hob = get_card(card), get_hobby(hobby)
    exists = any(r["hobby_tag_id"] == hob["id"] for r in db.list_hobby_rows(row["id"]))
    rows = [] if exists else [{"card": row["slug"], "field": "in_hobby", "before": None, "after": hob["slug"]}]
    warnings = [f"'{row['title']}' is already in '{hob['name']}'."] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and not exists:
        db.insert_card_hobby(row["id"], hob["id"], "add_to_hobby", actor, batch_id, [row["slug"], hob["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def remove_from_hobby(card, hobby, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Takes a card out of a hobby (no-op if it wasn't in it)."""
    row, hob = get_card(card), get_hobby(hobby)
    exists = any(r["hobby_tag_id"] == hob["id"] for r in db.list_hobby_rows(row["id"]))
    rows = [{"card": row["slug"], "field": "in_hobby", "before": hob["slug"], "after": None}] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and exists:
        db.delete_card_hobby(row["id"], hob["id"], "remove_from_hobby", actor, batch_id, [row["slug"], hob["slug"]])
    return Result(True, rows, [], batch_id, dry_run)


# --- Moving and copying files ----------------------------------------------------

def _as_slug_list(slugs):
    if isinstance(slugs, str):
        slugs = [slugs]
    out = []
    for s in slugs or []:
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)
    if not out:
        raise CardError("bad_files", "Name at least one file (by slug).")
    return out


def _transfer_files(slugs, from_card, to_card, move, dry_run, actor, batch_id):
    src, dst = get_card(from_card), get_card(to_card)
    if src["id"] == dst["id"]:
        raise CardError("bad_files", "The source and destination are the same card.")
    slugs = _as_slug_list(slugs)
    held = {r["post_slug"] for r in db.list_project_item_rows(src["id"])}
    missing = [s for s in slugs if s not in held]
    if missing:
        raise CardError("bad_files", f"Not in '{src['title']}': {', '.join(missing[:8])}.", {"missing": missing})
    for s in slugs:
        if s == src.get("writeup_slug") or s == dst.get("writeup_slug"):
            raise CardError("bad_files", "A card's write-up can't be moved or copied; it belongs to its card.",
                            {"slug": s})
    already = {r["post_slug"] for r in db.list_project_item_rows(dst["id"])}
    rows, warnings = [], []
    for s in slugs:
        if s not in already:
            rows.append({"card": dst["slug"], "field": "file", "before": None, "after": s})
        else:
            warnings.append(f"{s} is already in '{dst['title']}'.")
        if move:
            rows.append({"card": src["slug"], "field": "file", "before": s, "after": None})
    if move and src.get("cover_slug") in slugs:
        warnings.append(f"'{src['title']}' still has {src['cover_slug']} as its cover; pick another if you want.")
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run:
        with db.transaction():
            db.write_card_items(dst["id"], slugs, [], "move_files" if move else "copy_files", actor, batch_id,
                                [src["slug"], dst["slug"]])
            if move:
                db.write_card_items(src["id"], [], slugs, "move_files", actor, batch_id, [src["slug"], dst["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def move_files(slugs, from_card, to_card, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Moves files (by slug) from one card to another: they leave `from_card` and join
    `to_card`. A card's own write-up can't be moved."""
    return _transfer_files(slugs, from_card, to_card, True, dry_run, actor, batch_id)


def copy_files(slugs, from_card, to_card, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Adds files from one card to another WITHOUT removing them (files are many-to-many)."""
    return _transfer_files(slugs, from_card, to_card, False, dry_run, actor, batch_id)


# --- split_card (6) ----------------------------------------------------------------

def _resolve_hobby_refs(spec, src):
    """'inherit' -> the source's hobbies (attach order); a list -> those hobbies; else none."""
    if spec == "inherit":
        return [h for h in (db.get_hobby(r["hobby_tag_id"]) for r in db.list_hobby_rows(src["id"])) if h]
    if spec in (None, "", []):
        return []
    return [get_hobby(h) for h in spec]


def _resolve_family_refs(spec, src):
    if spec == "inherit":
        return [f for f in db.list_families_for_member(src["id"])]
    if spec in (None, "", []):
        return []
    return [get_card(f) for f in spec]


def split_card(source, parts, *, keep_in_source=False, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Carves files (and a description) out of `source` into new cards (spec 6).

    Each part: {title, kind='project', relation='sibling'|'child', stage, stop_reason,
    provenance, provenance_credit, whereabouts, whereabouts_note, description,
    move_description, file_slugs, link_to_source: {type, note?}|None,
    hobbies: 'inherit'|[hobby refs], families: 'inherit'|[family refs], highlight}.
    A `child` is created nested under the source (nest rules apply). A `sibling` has no
    parent and by default gets the source's hobbies and family memberships copied; a
    child defaults to none. Files named in `file_slugs` are MOVED out of the source
    (several parts may name the same file; it lands in each), unless keep_in_source=True
    (then copied). Every part is a full card, blank write-up included. One transaction and
    one batch_id: any rule violation writes nothing; `undo` removes the parts and restores
    the source's files."""
    src = get_card(source)
    if not isinstance(parts, list) or not parts:
        raise CardError("bad_split", "Give at least one part to split out.")
    relations = [card_rules.validate_split_part(p, i) for i, p in enumerate(parts)]
    held = {r["post_slug"] for r in db.list_project_item_rows(src["id"])}
    moved_all = []
    for i, p in enumerate(parts):
        files = _as_slug_list(p["file_slugs"]) if p.get("file_slugs") else []
        missing = [s for s in files if s not in held]
        if missing:
            raise CardError("bad_files", f"Part {i + 1} names file(s) not in '{src['title']}': {', '.join(missing[:8])}.",
                            {"missing": missing})
        if src.get("writeup_slug") in files:
            raise CardError("bad_files", "A card's write-up can't be split out; it belongs to its card.")
        moved_all += [s for s in files if s not in moved_all]
    batch_id = batch_id or changes.new_batch_id()
    rows, warnings, created = [], [], []
    with db.transaction(dry_run=dry_run):
        for i, p in enumerate(parts):
            relation = relations[i]
            kind = card_rules.validate_kind(p.get("kind") or card_rules.DEFAULT_KIND)
            stage = p.get("stage") or card_rules.DEFAULT_STAGE
            description = p.get("description") or ""
            if p.get("move_description"):
                description = src.get("description") or ""
            new = db.create_project(p["title"].strip(), description=description, kind=kind, stage=stage,
                                    stop_reason=p.get("stop_reason"),
                                    parent_id=src["id"] if relation == "child" else None,
                                    actor=actor, batch_id=batch_id)
            if p.get("provenance") or p.get("provenance_credit"):
                set_provenance(new["id"], p.get("provenance"), p.get("provenance_credit") or ...,
                               actor=actor, batch_id=batch_id)
            if p.get("whereabouts") or p.get("whereabouts_note"):
                set_whereabouts(new["id"], p.get("whereabouts"), p.get("whereabouts_note") or ...,
                                actor=actor, batch_id=batch_id)
            if p.get("highlight"):
                set_highlight(new["id"], True, actor=actor, batch_id=batch_id)
            files = _as_slug_list(p["file_slugs"]) if p.get("file_slugs") else []
            if files:
                db.write_card_items(new["id"], files, [], "split_card", actor, batch_id, [new["slug"], src["slug"]])
                if src.get("cover_slug") in files:
                    db.write_card_row(new["id"], {"cover_slug": src["cover_slug"]}, "split_card", actor, batch_id)
            hobby_spec = p.get("hobbies", "inherit" if relation == "sibling" else None)
            for hob in _resolve_hobby_refs(hobby_spec, src):
                add_to_hobby(new["id"], hob["id"], actor=actor, batch_id=batch_id)
            family_spec = p.get("families", "inherit" if relation == "sibling" else None)
            for fam in _resolve_family_refs(family_spec, src):
                add_to_family(fam["id"], new["id"], actor=actor, batch_id=batch_id)
            if p.get("link_to_source"):
                spec = p["link_to_source"]
                link(new["id"], src["id"], spec.get("type"), spec.get("note") or "", actor=actor, batch_id=batch_id)
            fresh = db.get_project(new["id"])
            created.append({**_slim(fresh), "relation": relation, "files": files})
            rows.append({"card": fresh["slug"], "field": "created", "before": None,
                         "after": f"{fresh['title']} ({card_rules.kind_label(kind)}, {relation})"})
            for s in files:
                rows.append({"card": fresh["slug"], "field": "file", "before": None, "after": s})
        if any(p.get("move_description") for p in parts):
            db.write_card_row(src["id"], {"description": ""}, "split_card", actor, batch_id)
            rows.append({"card": src["slug"], "field": "description", "before": src.get("description"), "after": ""})
        if moved_all and not keep_in_source:
            db.write_card_items(src["id"], [], moved_all, "split_card", actor, batch_id, [src["slug"]])
            for s in moved_all:
                rows.append({"card": src["slug"], "field": "file", "before": s, "after": None})
            if src.get("cover_slug") in moved_all:
                warnings.append(f"'{src['title']}' still has {src['cover_slug']} as its cover; pick another if you want.")
    return Result(True, rows, warnings, batch_id, dry_run, {"created": created})


# --- merge_cards (6) ----------------------------------------------------------------

def _merge_one(keep, a, actor, batch_id, rows, warnings):
    """Folds card `a` into `keep` (runs inside the caller's transaction)."""
    keep_group = (keep.get("kind") or "project") in card_rules.GROUP_KINDS
    a_group = (a.get("kind") or "project") in card_rules.GROUP_KINDS
    if keep_group != a_group:
        raise CardError("bad_merge", f"Can't merge '{a['title']}' ({card_rules.kind_label(a.get('kind') or 'project')}) "
                        f"into '{keep['title']}' ({card_rules.kind_label(keep.get('kind') or 'project')}): "
                        "families and collections only merge with their own kind.")
    slugs = [keep["slug"], a["slug"]]
    op = "merge_cards"
    descendants = db._descendant_project_ids(a["id"])
    if keep["id"] in descendants and keep.get("parent_id") != a["id"]:
        raise CardError("nest_cycle", f"'{keep['title']}' is nested inside '{a['title']}' (not directly), so "
                        "merging would make a cycle. Unnest one of them first.")
    if keep.get("parent_id") == a["id"]:
        db.update_card_columns(keep["id"], {"parent_id": a.get("parent_id")}, op, actor, batch_id=batch_id)
        rows.append({"card": keep["slug"], "field": "parent", "before": a["slug"], "after": _slug_of(a.get("parent_id"))})
    # Children of the absorbed card now belong to keep.
    for child in db.list_child_projects(a["id"]):
        if child["id"] == keep["id"]:
            continue
        db.check_nest(child, keep["id"], replace=True)
        db.update_card_columns(child["id"], {"parent_id": keep["id"]}, op, actor, batch_id=batch_id)
        rows.append({"card": child["slug"], "field": "parent", "before": a["slug"], "after": keep["slug"]})
    # Files: unioned into keep. A blank auto write-up is deleted with the card; a real one stays as a file.
    item_rows = db.list_project_item_rows(a["id"])
    file_slugs = [r["post_slug"] for r in item_rows]
    blank_writeup = a.get("writeup_slug") if a.get("writeup_slug") and db.blank_document_body(a["writeup_slug"]) else None
    moving = [s for s in file_slugs if s != blank_writeup]
    added, _ = db.write_card_items(keep["id"], moving, [], op, actor, batch_id, slugs)
    for s in added:
        rows.append({"card": keep["slug"], "field": "file", "before": None, "after": s})
    if a.get("writeup_slug") and not blank_writeup and a["writeup_slug"] in moving:
        warnings.append(f"'{a['title']}' had a write-up with text; it's now an ordinary file in '{keep['title']}'.")
    db.write_card_items(a["id"], [], file_slugs, op, actor, batch_id, slugs)
    if blank_writeup:
        def _drop_doc(log):
            for r in db.list_post_tag_rows(blank_writeup):
                log.delete("post_tags", {"post_slug": blank_writeup, "tag_id": r["tag_id"]})
            log.delete("capture_events", {"slug": blank_writeup})
        db.write_images(op, actor, batch_id, slugs, _drop_doc)
    # Cover: keep's wins; adopt the absorbed one only if keep has none.
    if not keep.get("cover_slug") and not keep.get("cover_project_id"):
        adopt = {}
        if a.get("cover_slug"):
            adopt["cover_slug"] = a["cover_slug"]
        elif a.get("cover_project_id") and a["cover_project_id"] != keep["id"]:
            adopt["cover_project_id"] = a["cover_project_id"]
        if adopt:
            db.write_card_row(keep["id"], adopt, op, actor, batch_id)
            rows.append({"card": keep["slug"], "field": "cover", "before": None, "after": list(adopt.values())[0]})
    # Hobbies: unioned.
    for r in db.list_hobby_rows(a["id"]):
        if db.insert_card_hobby(keep["id"], r["hobby_tag_id"], op, actor, batch_id, slugs):
            hob = db.get_hobby(r["hobby_tag_id"])
            rows.append({"card": keep["slug"], "field": "in_hobby", "before": None, "after": hob["slug"] if hob else r["hobby_tag_id"]})
        db.delete_card_hobby(a["id"], r["hobby_tag_id"], op, actor, batch_id, slugs)
    # Family memberships (as a member), then members (as a family): unioned.
    for r in db.list_family_rows(a["id"], as_member=True):
        fam = db.get_project(r["family_id"])
        if fam is not None and fam["id"] != keep["id"]:
            res = add_to_family(fam["id"], keep["id"], actor=actor, batch_id=batch_id, _op=op)
            rows += res.changes
        db.remove_family_member(r["family_id"], a["id"], op, actor, batch_id=batch_id, affected_slugs=slugs)
    for r in db.list_family_rows(a["id"], as_member=False):
        mem = db.get_project(r["member_id"])
        if mem is not None and mem["id"] != keep["id"]:
            res = add_to_family(keep["id"], mem["id"], actor=actor, batch_id=batch_id, _op=op)
            rows += res.changes
        db.remove_family_member(a["id"], r["member_id"], op, actor, batch_id=batch_id, affected_slugs=slugs)
    # Links: re-pointed; self-links and duplicates dropped; a typed link beats `related` on the same pair.
    a_links = db.list_project_link_rows(slug=a["slug"])
    if a_links:
        existing = {(r["slug_a"], r["slug_b"], r["type"]) for r in db.list_project_link_rows(slug=keep["slug"])}
        deletes = [(r["slug_a"], r["slug_b"], r["type"]) for r in a_links]
        inserts, planned = [], set()
        for r in a_links:
            na = keep["slug"] if r["slug_a"] == a["slug"] else r["slug_a"]
            nb = keep["slug"] if r["slug_b"] == a["slug"] else r["slug_b"]
            key = (na, nb, r["type"])
            if na == nb or key in existing or key in planned:
                continue
            planned.add(key)
            inserts.append({"slug_a": na, "slug_b": nb, "type": r["type"], "note": r.get("note") or ""})
            rows.append({"card": keep["slug"], "field": "link", "before": None, "after": f"{r['type']} {nb if na == keep['slug'] else na}"})
        all_rows = existing | planned
        typed_pairs = {frozenset((x, y)) for x, y, t in all_rows if t != "related"}
        for (x, y, t) in sorted(all_rows):
            if t == "related" and frozenset((x, y)) in typed_pairs:
                if (x, y, t) in planned:
                    inserts = [i for i in inserts if (i["slug_a"], i["slug_b"], i["type"]) != (x, y, t)]
                else:
                    deletes.append((x, y, t))
                warnings.append(f"'{x}' and '{y}' ended up both related and typed after the merge; the typed link was kept.")
        db.write_project_links(deletes, inserts, op, actor, batch_id=batch_id, affected_slugs=slugs)
    # Blog entries that featured the absorbed card now feature keep.
    def _repoint_entries(log):
        have = {r["entry_id"] for r in db.list_entry_project_rows(keep["id"])}
        for r in db.list_entry_project_rows(a["id"]):
            log.delete("blog_entry_projects", {"entry_id": r["entry_id"], "project_id": a["id"]})
            if r["entry_id"] not in have:
                log.insert("blog_entry_projects", {"entry_id": r["entry_id"], "project_id": keep["id"]},
                           {"sort_order": r["sort_order"], "note": r["note"]})
    db.write_images(op, actor, batch_id, slugs, _repoint_entries)
    # Open questions about the absorbed card are moot.
    for d in open_card_decisions(a["slug"]):
        db.resolve_pending_decision(d["id"], {"stale": f"merged into {keep['slug']}"},
                                    log={"op": op, "actor": actor, "batch_id": batch_id})
        rows.append({"card": a["slug"], "field": "decision", "before": d["kind"], "after": "resolved (stale)"})
    # Other cards that borrowed the absorbed card's cover or named it as their home.
    for other in db.list_projects_referencing(a["id"]):
        if other["id"] == a["id"]:
            continue
        if other.get("cover_project_id") == a["id"]:
            db.write_card_row(other["id"], {"cover_project_id": keep["id"] if other["id"] != keep["id"] else None},
                              op, actor, batch_id)
        if other.get("home_kind") == "card" and other.get("home_ref") == a["id"]:
            new_home = (None, None) if other["id"] == keep["id"] else ("card", keep["id"])
            db.update_card_columns(other["id"], {"home_kind": new_home[0], "home_ref": new_home[1]}, op, actor,
                                   batch_id=batch_id)
    db.write_card_row(keep["id"], {}, op, actor, batch_id)
    db.write_images(op, actor, batch_id, slugs, lambda log: log.delete("projects", {"id": a["id"]}))
    rows.append({"card": keep["slug"], "field": "merged", "before": a["slug"], "after": None})


def merge_cards(keep, absorb, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Folds one or more cards into `keep` (spec 6): files unioned (deduped), hobbies and
    family memberships unioned, children re-parented, links re-pointed (self-links and
    duplicates dropped), blog-entry attachments re-pointed. keep's cover and write-up win; an
    absorbed card's blank write-up goes with it, a real one stays as an ordinary file.
    Open questions on absorbed cards are resolved as stale. The absorbed card rows are
    deleted; their row images make `undo` restore them (same id, same files). Refuses
    (writing nothing) when it would create a nest cycle or merge a group kind with a
    non-group kind."""
    keep_row = get_card(keep)
    absorb = [absorb] if not isinstance(absorb, (list, tuple)) else list(absorb)
    ids, absorbed = {keep_row["id"]}, []
    for ref in absorb:
        a = get_card(ref)
        if a["id"] == keep_row["id"]:
            raise CardError("bad_merge", "A card can't be merged into itself.")
        if a["id"] in ids:
            continue
        ids.add(a["id"])
        absorbed.append(a)
    if not absorbed:
        raise CardError("bad_merge", "Name at least one card to absorb.")
    batch_id = batch_id or changes.new_batch_id()
    rows, warnings = [], []
    with db.transaction(dry_run=dry_run):
        for a in absorbed:
            _merge_one(db.get_project(keep_row["id"]), db.get_project(a["id"]), actor, batch_id, rows, warnings)
    return Result(True, rows, warnings, batch_id, dry_run, {"keep": keep_row["slug"], "absorbed": [a["slug"] for a in absorbed]})


# --- delete_card (#497) -------------------------------------------------------------

def _clear_card_dependents(card, op, actor, batch_id, rows, warnings, *, dissolving=False):
    """Removes everything that hangs off a card so no ghost rows survive it (runs inside the
    caller's transaction, every row imaged). `dissolving` = the card row itself is handled by
    the caller (convert-to-hobby): children, files and the card row are left alone, except
    that a blank auto write-up is still dropped. Returns {children, files, writeup}."""
    slug, cid = card["slug"], card["id"]
    slugs = [slug]
    out = {"children": 0, "files": 0, "writeup": None}
    # Open questions about this card are moot: resolve them with a note (never leave them dangling).
    for d in open_card_decisions(slug):
        db.resolve_pending_decision(d["id"], {"stale": f"card '{slug}' was deleted"},
                                    log={"op": op, "actor": actor, "batch_id": batch_id})
        rows.append({"card": slug, "field": "decision", "before": d["kind"], "after": "resolved (stale)"})
    # Children are orphaned (parent_id cleared), never deleted.
    if not dissolving:
        for child in db.list_child_projects(cid):
            db.update_card_columns(child["id"], {"parent_id": None}, op, actor, batch_id=batch_id)
            rows.append({"card": child["slug"], "field": "parent", "before": slug, "after": None})
            out["children"] += 1
        if out["children"]:
            warnings.append(f"{out['children']} nested card(s) now stand on their own.")
    # The auto write-up goes only while it's still blank; one with text stays as an ordinary unfiled file.
    ws = card.get("writeup_slug")
    blank_ws = ws if ws and db.blank_document_body(ws) else None
    file_slugs = [r["post_slug"] for r in db.list_project_item_rows(cid)]
    if blank_ws:
        for d in db.list_all_pending_decisions(post_slug=blank_ws):
            if d["resolved_at"] is None:
                db.resolve_pending_decision(d["id"], {"stale": f"write-up of deleted card '{slug}'"},
                                            log={"op": op, "actor": actor, "batch_id": batch_id})

        def _drop_doc(log):
            if blank_ws in file_slugs:
                log.delete("project_items", {"project_id": cid, "post_slug": blank_ws})
            for r in db.list_post_tag_rows(blank_ws):
                log.delete("post_tags", {"post_slug": blank_ws, "tag_id": r["tag_id"]})
            log.delete("capture_events", {"slug": blank_ws})
        db.write_images(op, actor, batch_id, slugs, _drop_doc)
        out["writeup"] = "deleted"
        rows.append({"card": slug, "field": "writeup", "before": blank_ws, "after": None})
    elif ws and ws in file_slugs and db.get_by_slug(ws) is not None:
        out["writeup"] = "kept"
        warnings.append(f"The write-up '{ws}' has text, so it was kept as an ordinary unfiled document.")
    if not dissolving:
        keep_files = [s for s in file_slugs if s != blank_ws]
        db.write_card_items(cid, [], keep_files, op, actor, batch_id, slugs)
        out["files"] = len(keep_files)
    for r in db.list_hobby_rows(cid):
        db.delete_card_hobby(cid, r["hobby_tag_id"], op, actor, batch_id, slugs)
    for r in db.list_family_rows(cid, as_member=True):
        db.remove_family_member(r["family_id"], cid, op, actor, batch_id=batch_id, affected_slugs=slugs)
    for r in db.list_family_rows(cid, as_member=False):
        db.remove_family_member(cid, r["member_id"], op, actor, batch_id=batch_id, affected_slugs=slugs)
    links = db.list_project_link_rows(slug=slug)
    if links:
        db.write_project_links([(r["slug_a"], r["slug_b"], r["type"]) for r in links], [], op, actor,
                               batch_id=batch_id, affected_slugs=slugs)
        for r in links:
            rows.append({"card": slug, "field": "link", "before": f"{r['slug_a']} {r['type']} {r['slug_b']}", "after": None})
    def _drop_entry_rows(log):
        for r in db.list_entry_project_rows(cid):
            log.delete("blog_entry_projects", {"entry_id": r["entry_id"], "project_id": cid})
    db.write_images(op, actor, batch_id, slugs, _drop_entry_rows)
    # Other cards that borrowed this card's cover or named it as their home.
    for other in db.list_projects_referencing(cid):
        if other["id"] == cid:
            continue
        if other.get("cover_project_id") == cid:
            db.write_card_row(other["id"], {"cover_project_id": None}, op, actor, batch_id)
        if other.get("home_kind") == "card" and other.get("home_ref") == cid:
            db.update_card_columns(other["id"], {"home_kind": None, "home_ref": None}, op, actor, batch_id=batch_id)
    return out


def delete_card(card, *, dry_run=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Deletes a card cleanly (#497), in one transaction with row images, so `undo(batch_id)`
    brings everything back. Removes its blank auto write-up (a write-up with text is kept as an
    ordinary unfiled document, with a warning), its files membership, hobby and family rows
    (as member and as family), typed links in either direction, blog-entry attachments, and
    resolves its open decisions. Nested children are orphaned, not deleted; other cards that
    used it as a cover or home lose that pointer. Files themselves are never deleted.
    Returns Result with data {deleted, children_orphaned, items_detached, writeup}."""
    row = get_card(card)
    batch_id = batch_id or changes.new_batch_id()
    rows, warnings = [], []
    with db.transaction(dry_run=dry_run):
        fresh = db.get_project(row["id"])
        out = _clear_card_dependents(fresh, "delete_card", actor, batch_id, rows, warnings)
        db.write_images("delete_card", actor, batch_id, [row["slug"]], lambda log: log.delete("projects", {"id": row["id"]}))
        rows.append({"card": row["slug"], "field": "deleted", "before": row["title"], "after": None})
    return Result(True, rows, warnings, batch_id, dry_run,
                  {"deleted": row["slug"], "children_orphaned": out["children"], "items_detached": out["files"],
                   "writeup": out["writeup"]})


def convert_project_to_hobby(card, *, actor=changes.ACTOR_MCP):
    """Convert-to-hobby through the same clean-up (#497): links, family rows, hobby rows,
    blog-entry attachments and a blank write-up no longer survive the converted card, and
    the whole conversion is one transaction. (The tag side of the conversion is not
    row-imaged, so this is not undoable.) Returns the hobby tag dict, or None if not found."""
    row = db.get_project(card)
    if row is None:
        return None
    with db.transaction():
        fresh = db.get_project(row["id"])
        _clear_card_dependents(fresh, "convert_project_to_hobby", actor, changes.new_batch_id(), [], [], dissolving=True)
        return db.convert_project_to_hobby(row["id"])


# --- Computed needs added in piece 6 -------------------------------------------------

def status_and_writeup_needs(kind=None, hobby_ids=None):
    """Computed needs (never stored): status_conflict (a 3.2 rule 6 warning that holds right
    now: active/inactive disagrees with the parent, or Done with nested work still in
    progress) and blank_writeup_with_files (the auto-made write-up is still empty although
    the card has files)."""
    rows = []
    for c in db.list_projects():
        ck = c.get("kind") or "project"
        if (kind and ck != kind) or (hobby_ids is not None and c["id"] not in hobby_ids):
            continue
        if c.get("stage"):
            warns = _status_warnings(c, c.get("activity"), c.get("stage"))
            if warns:
                rows.append({"need": NEED_STATUS_CONFLICT, "card_slug": c["slug"], "title": c["title"],
                             "detail": " ".join(warns), "suggested": None, "suggested_reason": None,
                             "confidence": None, "decision_id": None, "options": []})
        if ck not in card_rules.GROUP_KINDS and c.get("writeup_slug") and db.blank_document_body(c["writeup_slug"]):
            n = len([r for r in db.list_project_item_rows(c["id"]) if r["post_slug"] != c["writeup_slug"]])
            if n:
                rows.append({"need": NEED_BLANK_WRITEUP_WITH_FILES, "card_slug": c["slug"], "title": c["title"],
                             "detail": f"The write-up is still blank, but the card has {n} file(s).",
                             "suggested": None, "suggested_reason": None, "confidence": None,
                             "decision_id": None, "options": []})
    return rows


# --- explain_card (6) ---------------------------------------------------------------

def _file_summary(card, items):
    files = [i for i in items if i["slug"] != card.get("writeup_slug")]
    by_type = {}
    for f in files:
        by_type[f.get("media_type") or "unknown"] = by_type.get(f.get("media_type") or "unknown", 0) + 1
    dates = [timeline.resolve_item_date(f) for f in files]
    return {"total": len(files), "by_type": by_type,
            "earliest": min(dates) if dates else None, "latest": max(dates) if dates else None}


def explain_card(card):
    """Everything about a card in one call (spec 6) -- what to read before proposing a
    change. Returns a dict: identity, status (kind/activity/stage/stop_reason, `provisional`),
    whereabouts, provenance, highlight, hobbies, families, members, parent, children, links,
    home (+ `home_chain`), files (counts per type, date span), level (pips + reasons),
    open_decisions, needs (computed + stored for this card), warnings, suggestions,
    recent_changes. Read-only."""
    from . import card_level  # lazy: card_level imports db/timeline only, but keeps cards.py's import graph flat
    row = get_card(card)
    items = db.list_project_items(row["id"])
    parent = db.get_project(row["parent_id"]) if row.get("parent_id") else None
    home = resolve_home(row["id"])
    needs = list_needs_decision(card=row["slug"])
    warnings = _status_warnings(row, row.get("activity"), row.get("stage")) if row.get("stage") else []
    if home.get("dangling_override"):
        warnings.append("The manual home points at something that no longer exists; the automatic home is used.")
    suggestions = {"provenance": suggest_provenance(row),
                   "links": [n["link"] for n in needs if n["need"] == NEED_UNTYPED_LINK and n.get("link")]}
    recent = []
    for r in db.list_change_log(card_slug=row["slug"], limit=5):
        recent.append({"id": r["id"], "op": r["op"], "actor": r["actor"], "batch_id": r["batch_id"],
                       "timestamp": r["timestamp"], "undone": r.get("undone_by") is not None})
    out = {
        "id": row["id"], "slug": row["slug"], "title": row["title"], "description": row.get("description") or "",
        **status_fields(row),
        "provisional": provisional_legacy_status(row) is not None,
        **whereabouts_fields(row),
        "hobbies": [hobby_fields(get_hobby(r["hobby_tag_id"]), with_flags=False)
                    for r in db.list_hobby_rows(row["id"]) if db.get_hobby(r["hobby_tag_id"])],
        **family_fields(row),
        "parent": _slim(parent) if parent else None,
        "children": [_slim(c) for c in db.list_child_projects(row["id"])],
        "links": list_links(row["id"]),
        "home": home,
        "home_chain": home_chain(row["id"]),
        "home_override": ({"kind": row["home_kind"], "ref": row["home_ref"],
                           "text": _home_text(row["home_kind"], row["home_ref"])} if row.get("home_kind") else None),
        "files": _file_summary(row, items),
        "level": card_level.card_level(row, items),
        "open_decisions": [decision_summary(d) for d in open_card_decisions(row["slug"])],
        "needs": [n for n in needs if n["decision_id"] is None],
        "warnings": warnings,
        "suggestions": suggestions,
        "recent_changes": recent,
    }
    return out


# --- Card face (8.1) ---------------------------------------------------------------
# Everything a card renderer needs, in one dict. Reuses card_level / resolve_home /
# list_links / family_fields rather than recomputing; explain_card is the long form
# for a single card, this is the light form for a whole page of them.

FACE_FACT_LINES = 4
FLAVOR_MAX = 90
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _month_year(ts):
    t = time.gmtime(ts)
    return f"{_MONTHS[t.tm_mon - 1]} {t.tm_year}"


def date_range_label(start, end, active=False, now=None):
    """`Nov 2025 - Jul 2026`; a single moment `Jul 2026`; an active card whose end is in
    the past `Nov 2025 - now` (8.1). Month granularity, UTC."""
    if start is None:
        return ""
    first = _month_year(start)
    last = _month_year(end if end is not None else start)
    if active:
        now_label = _month_year(now if now is not None else time.time())
        if last != now_label:
            return f"{first} - now"
    return first if first == last else f"{first} - {last}"


def _flavor_line(description):
    text = " ".join((description or "").split())
    if not text:
        return ""
    end = re.search(r"[.!?](\s|$)", text)
    sentence = text[:end.end()].strip() if end else text
    if len(sentence) > FLAVOR_MAX:
        sentence = sentence[:FLAVOR_MAX - 1].rstrip(" ,;:") + "..."
    return sentence


def card_face(card, items=None):
    """The card face data for one project/card row (a `db.get_project` dict): zone content
    for web/templates/_card.html. `cover_slug` is the resolved cover (the caller turns it
    into a URL). Hobby and file cards are built by the page from their own rows."""
    from . import card_level  # lazy, as in explain_card
    row = get_card(card) if not isinstance(card, dict) else card
    if items is None:
        items = db.list_project_items(row["id"])
    kind = row.get("kind") or "project"
    start, end = timeline.resolve_project_span(row, items)
    status = status_fields(row)
    level = card_level.card_level(row, items)
    home = resolve_home(row["id"])
    fams = db.list_families_for_member(row["id"])
    parent = db.get_project(row["parent_id"]) if row.get("parent_id") else None
    hobbies = []
    for hr in db.list_hobby_rows(row["id"]):
        h = db.get_hobby(hr["hobby_tag_id"])
        if h:
            hobbies.append({"id": h["id"], "slug": h["slug"], "name": h["name"],
                            "code": h.get("group_code") or "",
                            "active": (h.get("hobby_status") or "active") == "active"})
    links = list_links(row["id"])
    wf = whereabouts_fields(row)

    facts = []
    if wf["whereabouts_applies"] and wf["whereabouts"]:
        facts.append(wf["whereabouts_label"] + (f" - {wf['whereabouts_note']}" if wf["whereabouts_note"] else ""))
    if parent:
        facts.append(f"Part of {parent['title']}")
    for f in fams:
        facts.append(f"In family {f['title']}")
    for lk in links:
        facts.append(f"{lk['label']} {lk['title']}")
    facts_more = max(0, len(facts) - FACE_FACT_LINES)
    facts = facts[:FACE_FACT_LINES]

    type_line = status["kind_label"]
    if home.get("title"):
        type_line += f" - {home['title']}"
    elif kind in card_rules.GROUP_KINDS:
        type_line += " - group"

    files = [i for i in items if i["slug"] != row.get("writeup_slug")]
    if kind in card_rules.GROUP_KINDS:
        n_members = db.count_family_members(row["id"])
        stats = [("members", n_members)]
    else:
        stats = [("files", len(files)), ("nested", len(db.list_child_projects(row["id"]))), ("links", len(links))]

    order = None
    if len(fams) == 1:
        rows = db.list_family_rows(fams[0]["id"], as_member=False)
        orders = [r["sort_order"] for r in rows]
        if len(rows) > 1 and len(set(orders)) == len(orders):
            ranked = sorted(rows, key=lambda r: r["sort_order"])
            order = {"n": [r["member_id"] for r in ranked].index(row["id"]) + 1, "of": len(rows)}

    provenance = wf["provenance_label"] or ""
    if provenance and wf["provenance_credit"]:
        provenance += f" - {wf['provenance_credit']}"

    return {
        "slug": row["slug"], "title": row["title"], "description": row.get("description") or "",
        **status,
        "highlight": wf["highlight"],
        "effective_start": start, "effective_end": end, "created_at": row["created_at"],
        "dates": date_range_label(start, end, active=row.get("activity") == "active"),
        "level": level["score"], "level_checks": level["checks"],
        "show_level": True,
        "cover_slug": db.resolve_project_cover_slug(row),
        "type_line": type_line,
        "hobbies": hobbies,
        "codes": [h["code"] for h in hobbies if h["code"]],
        "facts": facts, "facts_more": facts_more,
        "flavor": _flavor_line(row.get("description")),
        "stats": [{"label": l, "n": n} for l, n in stats],
        "provenance": provenance,
        "order": order,
        "family_ids": [f["id"] for f in fams],
        "card_id": row["id"],
    }


STACK_UNDER = 3     # tilted cards under the flat top card in a pile
STACK_FAN_MAX = 7   # cards shown when a pile is fanned out (then "+N")


def _day_label(ts):
    d = timeline.epoch_to_local(ts)
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _asset_card(r, ts, spec, project_slug, thumb_fn):
    """One file's asset-card dict (8.1) for a pile or a fan."""
    prov = (r.get("provenance") or "").strip()
    return {
        "slug": r["slug"], "kind": "asset",
        "title": r.get("content_description") or r.get("description") or r.get("filename") or r["slug"],
        "dates": _day_label(ts),
        "type_line": spec.label,
        "provenance": prov.capitalize(),
        "cover_url": thumb_fn(r) if thumb_fn else None,
        "href": f"/object/{r['slug']}?from=project:{project_slug}",
        "show_level": False, "codes": [], "facts": [], "stats": [],
    }


def project_pile(card, items=None, thumb_fn=None):
    """One mixed-type pile of a card's files (the hobby page, #525): the same asset cards and
    the same flat-top / tilted-under / count-badge shape as a detail-page pile, but across
    every file type. Earliest first, like file_stacks. Returns None for a card with no files.
    `count` is every file in the card's grid (write-up included), matching file_stacks."""
    from . import object_types  # lazy, same reason as the other lazy imports in this module
    row = get_card(card) if not isinstance(card, dict) else card
    if items is None:
        items = db.list_project_items(row["id"])
    if not items:
        return None
    dated = sorted(((timeline.resolve_item_date(r), r) for r in items), key=lambda p: p[0])
    shown = dated[:STACK_FAN_MAX]
    cards = [_asset_card(r, ts, object_types.get_object_type(r.get("media_type") or "unknown"), row["slug"], thumb_fn)
             for ts, r in shown]
    return {"count": len(items), "span": date_range_label(dated[0][0], dated[-1][0]),
            "cards": cards, "more": max(0, len(items) - len(cards))}


def hobby_card_face(hobby_row, projects, items_by_project, flags=None, needs_input=False):
    """The card face for a hobby (#525), in the same dict shape `card_face` returns so
    web/templates/_card.html draws it (kind 'hobby': the green frame). Built from the hobby
    row and its member projects: the date line is the computed span of the member projects'
    real dates, the stats are its project / file counts, the cover is the first member
    project's resolved cover (the caller turns `cover_slug` into a URL).
    `items_by_project` maps project id -> db.list_project_items rows."""
    status = hobby_row.get("hobby_status") or "active"
    spans = [timeline.resolve_project_span(p, items_by_project.get(p["id"], [])) for p in projects]
    starts = [s for s, _ in spans if s is not None]
    ends = [e for _, e in spans if e is not None]
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    code = hobby_row.get("group_code") or ""
    n_files = len({i["slug"] for its in items_by_project.values() for i in its})
    cover_slug = None
    for p in sorted(projects, key=lambda p: (p.get("activity") != "active", -(p.get("updated_at") or 0))):
        cover_slug = db.resolve_project_cover_slug(p)
        if cover_slug:
            break
    return {
        "slug": hobby_row["slug"], "kind": "hobby", "kind_label": "Hobby",
        "href": "#stacks",
        "title": hobby_row["name"], "description": "",
        "activity": status, "stage": None,
        "stage_label": card_rules.HOBBY_ACTIVITY_LABELS.get(status, status),
        "stop_reason_label": None,
        "needs_input": bool(needs_input),
        "highlight": False,
        "effective_start": start, "effective_end": end, "created_at": hobby_row.get("created_at") or 0,
        "dates": date_range_label(start, end, active=status == "active"),
        "level": 0, "show_level": False,
        "cover_slug": cover_slug,
        "type_line": "Hobby",
        "codes": [code] if code else [],
        "facts": [f["label"] for f in (flags or [])], "facts_more": 0, "flavor": "",
        "stats": [{"label": "projects", "n": len(projects)}, {"label": "files", "n": n_files}],
        "provenance": "", "order": None,
    }


def file_stacks(card, items=None, thumb_fn=None):
    """Piles for the detail page (8.3): one per `media_type` of the card's files, biggest
    first. Each pile: `media_type`, `label` (registry label), `count`, `span` (the
    `Mon YYYY - Mon YYYY` date span of the files' resolved dates), `cards` (up to
    STACK_FAN_MAX asset-card dicts, earliest first: title, own real date, type line,
    cover_url, href, provenance), and `more` (files beyond the fan). `thumb_fn(row)` gives
    a row's thumbnail URL or None (the page decides, since thumbnails depend on storage).
    Counts here are every file in the card's grid, write-up included."""
    from . import object_types  # lazy, same reason as the other lazy imports in this module
    row = get_card(card) if not isinstance(card, dict) else card
    if items is None:
        items = db.list_project_items(row["id"])
    by_type = {}
    for it in items:
        by_type.setdefault(it.get("media_type") or "unknown", []).append(it)
    piles = []
    for mt, rows in by_type.items():
        spec = object_types.get_object_type(mt)
        dated = sorted(((timeline.resolve_item_date(r), r) for r in rows), key=lambda p: p[0])
        shown = dated[:STACK_FAN_MAX]
        cards = [_asset_card(r, ts, spec, row["slug"], thumb_fn) for ts, r in shown]
        piles.append({
            "media_type": mt, "label": spec.label, "count": len(rows),
            "span": date_range_label(dated[0][0], dated[-1][0]),
            "cards": cards, "more": max(0, len(rows) - len(cards)),
        })
    piles.sort(key=lambda p: (-p["count"], p["label"]))
    return piles


def card_json(card):
    """GET /api/cards/{slug}: the card face for one card."""
    return card_face(card)


# --- Bulk (6) ---------------------------------------------------------------------

def _no_extra(args, allowed, op):
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise CardError("bad_bulk_args", f"{op}: unknown argument(s) {', '.join(extra)}. Allowed: {', '.join(allowed)}.")


def _need(args, key, op):
    if args.get(key) in (None, ""):
        raise CardError("bad_bulk_args", f"{op}: argument '{key}' is required.")
    return args[key]


def _bulk_adapters(actor, batch_id):
    """op -> fn(card, args) -> Result. `card` is the item's card (the subject of the op)."""
    common = {"actor": actor, "batch_id": batch_id}

    def set_status_(card, a):
        _no_extra(a, ("stage", "stop_reason", "activity"), "set_status")
        return set_status(card, _need(a, "stage", "set_status"), a.get("stop_reason"), activity=a.get("activity"), **common)

    def set_kind_(card, a):
        _no_extra(a, ("kind", "force"), "set_kind")
        return set_kind(card, _need(a, "kind", "set_kind"), force=bool(a.get("force")), **common)

    def set_whereabouts_(card, a):
        _no_extra(a, ("whereabouts", "value", "note"), "set_whereabouts")
        return set_whereabouts(card, a.get("whereabouts", a.get("value")), a["note"] if "note" in a else ..., **common)

    def set_prov_(card, a):
        _no_extra(a, ("provenance", "value", "credit"), "set_card_provenance")
        return set_provenance(card, a.get("provenance", a.get("value")), a["credit"] if "credit" in a else ..., **common)

    def set_home_(card, a):
        _no_extra(a, ("target",), "set_home")
        return set_home(card, a.get("target"), **common)

    def highlight_(card, a):
        _no_extra(a, ("on",), "set_card_highlight")
        return set_highlight(card, bool(a.get("on")), **common)

    def add_hobby_(card, a):
        _no_extra(a, ("hobby",), "add_to_hobby")
        return add_to_hobby(card, _need(a, "hobby", "add_to_hobby"), **common)

    def remove_hobby_(card, a):
        _no_extra(a, ("hobby",), "remove_from_hobby")
        return remove_from_hobby(card, _need(a, "hobby", "remove_from_hobby"), **common)

    def add_family_(card, a):
        _no_extra(a, ("family",), "add_to_family")
        return add_to_family(_need(a, "family", "add_to_family"), card, **common)

    def remove_family_(card, a):
        _no_extra(a, ("family",), "remove_from_family")
        return remove_from_family(_need(a, "family", "remove_from_family"), card, **common)

    def nest_(card, a):
        _no_extra(a, ("parent", "replace"), "nest")
        return nest(card, _need(a, "parent", "nest"), replace=bool(a.get("replace")), **common)

    def unnest_(card, a):
        _no_extra(a, (), "unnest")
        return unnest(card, **common)

    def link_(card, a):
        _no_extra(a, ("b", "type", "note"), "link")
        return link(card, _need(a, "b", "link"), _need(a, "type", "link"), a.get("note") or "", **common)

    def unlink_(card, a):
        _no_extra(a, ("b", "type"), "unlink")
        return unlink(card, _need(a, "b", "unlink"), a.get("type"), **common)

    def retype_(card, a):
        _no_extra(a, ("b", "from_type", "to_type", "note"), "retype_link")
        return retype_link(card, _need(a, "b", "retype_link"), a.get("from_type") or "related",
                           _need(a, "to_type", "retype_link"), note=a.get("note"), **common)

    def hobby_activity_(card, a):
        _no_extra(a, ("value", "status"), "set_hobby_activity")
        return set_hobby_activity(card, a.get("value", a.get("status")), **common)

    return {"set_status": set_status_, "set_kind": set_kind_, "set_whereabouts": set_whereabouts_,
            "set_card_provenance": set_prov_, "set_home": set_home_, "set_card_highlight": highlight_,
            "add_to_hobby": add_hobby_, "remove_from_hobby": remove_hobby_, "add_to_family": add_family_,
            "remove_from_family": remove_family_, "nest": nest_, "unnest": unnest_, "link": link_,
            "unlink": unlink_, "retype_link": retype_, "set_hobby_activity": hobby_activity_}


class _BulkAbort(Exception):
    pass


def bulk(op, items, *, dry_run=True, partial_ok=False, actor=changes.ACTOR_MCP, batch_id=None):
    """Runs one allow-listed setter (card_rules.BULK_OPS) over a list of items
    [{card, args: {...}}] (spec 6). DRY-RUN BY DEFAULT. Items run in order inside one
    transaction, so later items see earlier ones; every item is validated and reported
    (before/after) whether or not it succeeds. All-or-nothing: if any item fails nothing is
    written, unless partial_ok=True, which keeps the valid ones. Never raises for an item
    error. For add/remove_to_family `card` is the member and args.family the family; for
    set_hobby_activity `card` is the hobby.
    Returns {ok, dry_run, op, changes, warnings, batch_id, applied, items: [{card, ok, applied,
    changes, warnings, error?}]}."""
    card_rules.validate_bulk_op(op)
    if not isinstance(items, list) or not items:
        raise CardError("bad_bulk_args", "Give a list of items: [{card, args}].")
    batch_id = batch_id or changes.new_batch_id()
    adapter = _bulk_adapters(actor, batch_id)[op]
    results, all_changes, failed = [], [], 0
    try:
        with db.transaction(dry_run=dry_run) as tx:
            for it in items:
                entry = {"card": it.get("card") if isinstance(it, dict) else None}
                try:
                    if not isinstance(it, dict) or it.get("card") in (None, ""):
                        raise CardError("bad_bulk_args", "Each item needs a 'card'.")
                    with tx.savepoint():
                        res = adapter(it["card"], dict(it.get("args") or {}))
                    entry.update(ok=True, changes=res.changes, warnings=res.warnings)
                    all_changes += res.changes
                except CardError as e:
                    failed += 1
                    entry.update(ok=False, changes=[], warnings=[], error=e.to_dict())
                results.append(entry)
            if failed and not partial_ok:
                raise _BulkAbort()
    except _BulkAbort:
        pass
    wrote = not dry_run and (not failed or partial_ok)
    for entry in results:
        entry["applied"] = bool(wrote and entry["ok"])
    ok = not failed or partial_ok
    warnings = []
    if failed:
        warnings.append(f"{failed} of {len(results)} item(s) can't be applied; "
                        + ("the valid ones were applied." if wrote else "nothing was written."))
    return {"ok": bool(ok), "dry_run": dry_run, "op": op,
            "changes": [c for e in results if e["ok"] for c in e["changes"]] if ok or dry_run else [],
            "warnings": warnings, "batch_id": batch_id,
            "applied": sum(1 for e in results if e["applied"]), "items": results}


# --- Bulk decisions (6) -------------------------------------------------------------

def _changes_from_log(rows):
    """Flattens change-log rows into [{op, table, key, field, before, after, card}]."""
    out = []
    for r in rows:
        card = r["affected_slugs"][0] if len(r.get("affected_slugs") or []) == 1 else None
        for m in r["mutations"]:
            b, a = m.get("before"), m.get("after")
            if m["table"] == "pending_decisions":
                # Summarize: the payload column is the whole question, far too big to echo.
                def _state(img):
                    if not img:
                        return None
                    if img.get("resolved_at") is None:
                        return "open"
                    try:
                        res = (json.loads(img["payload"]).get("resolution") or {}) if img.get("payload") else {}
                    except ValueError:
                        res = {}
                    return f"resolved ({res.get('choice') or res.get('stale') or '?'})"
                slug = None
                dec = db.get_pending_decision(m["key"]["id"])
                if dec and is_card_decision_slug(dec["post_slug"]):
                    slug = dec["post_slug"][len(CARD_DECISION_PREFIX):]
                out.append({"op": r["op"], "card": slug or card, "table": m["table"], "key": m["key"],
                            "field": "decision", "before": _state(b) if b and "resolved_at" in b else "open",
                            "after": _state(a) if a and "resolved_at" in a else None})
                continue
            if b is None or a is None:
                out.append({"op": r["op"], "card": card, "table": m["table"], "key": m["key"],
                            "field": "(row)", "before": b, "after": a})
                continue
            for col in a:
                if col == "updated_at" or b.get(col) == a[col]:
                    continue
                out.append({"op": r["op"], "card": card, "table": m["table"], "key": m["key"], "field": col,
                            "before": b.get(col), "after": a[col]})
    return out


def resolve_decisions(items, *, accept_suggested=False, dry_run=True, partial_ok=False, actor=changes.ACTOR_MCP,
                      batch_id=None):
    """Clears many card decisions at once (spec 7.3). DRY-RUN BY DEFAULT.

    `items`: [{decision_id, choice | choices}], or with accept_suggested=True a list of
    decision ids (or {decision_id}); each is answered with its own suggested option. A
    decision with no usable suggestion is SKIPPED (reported, not an error). Items run in one
    transaction and one batch_id; a decision that can't be applied makes the whole call
    write nothing unless partial_ok=True. Returns {ok, dry_run, batch_id, applied, skipped,
    failed, changes, items: [{decision_id, card_slug, choice, status: applied|would_apply|
    skipped|failed, changes?, reason?, error?}]}."""
    from . import decisions as _decisions
    if not isinstance(items, list) or not items:
        raise CardError("bad_bulk_args", "Give a list of decisions to resolve.")
    batch_id = batch_id or changes.new_batch_id()
    results, failed, skipped = [], 0, 0
    seen_log_ids = 0
    all_changes = []
    try:
        with db.transaction(dry_run=dry_run) as tx:
            for it in items:
                if isinstance(it, int) or (isinstance(it, str) and it.isdigit()):
                    it = {"decision_id": int(it)}
                if not isinstance(it, dict) or it.get("decision_id") is None:
                    raise CardError("bad_bulk_args", "Each item needs a decision_id.")
                did = int(it["decision_id"])
                entry = {"decision_id": did, "card_slug": None, "choice": None}
                results.append(entry)
                d = db.get_pending_decision(did)
                if d is None:
                    failed += 1
                    entry.update(status="failed", error={"code": "not_found", "message": f"No such decision: {did}"})
                    continue
                entry["card_slug"] = d["post_slug"][len(CARD_DECISION_PREFIX):] if is_card_decision_slug(d["post_slug"]) else None
                if d["resolved_at"] is not None:
                    failed += 1
                    entry.update(status="failed", error={"code": "already_resolved", "message": f"Decision {did} is already resolved."})
                    continue
                if d["kind"] not in CARD_DECISION_KINDS or not is_card_decision_slug(d["post_slug"]):
                    failed += 1
                    entry.update(status="failed", error={"code": "not_card_decision",
                                                         "message": f"Decision {did} is a {d['kind']} question; resolve it one at a time."})
                    continue
                keys = {o["key"] for o in d["payload"].get("options", [])}
                choice, choices = it.get("choice"), it.get("choices")
                if accept_suggested:
                    sug = d["payload"].get("suggested")
                    if isinstance(sug, (list, tuple)):
                        # Multi-select question (card_family_members): the suggestion is a
                        # list of option keys, answered as `choices` like the single path.
                        picked = [k for k in sug if isinstance(k, str) and k in keys]
                        if not picked or len(picked) != len(sug):
                            skipped += 1
                            entry.update(status="skipped", reason="no suggested answer for this question")
                            continue
                        choice, choices = None, picked
                    elif not isinstance(sug, str) or sug not in keys:
                        skipped += 1
                        entry.update(status="skipped", reason="no suggested answer for this question")
                        continue
                    else:
                        choice, choices = sug, None
                entry["choice"] = choice if choice else choices
                try:
                    with tx.savepoint():
                        resolve_decision(did, choice=choice, choices=choices, actor=actor, batch_id=batch_id)
                    new_rows = [r for r in db.get_change_rows(batch_id=batch_id) if r["id"] > seen_log_ids]
                    seen_log_ids = max([seen_log_ids] + [r["id"] for r in new_rows])
                    entry["changes"] = _changes_from_log(new_rows)
                    entry["status"] = "would_apply" if dry_run else "applied"
                    all_changes += entry["changes"]
                except (CardError, _decisions.DecisionNotFound, _decisions.DecisionAlreadyResolved,
                        _decisions.UnknownDecisionKind, _decisions.InvalidChoice) as e:
                    failed += 1
                    entry.update(status="failed",
                                 error=e.to_dict() if isinstance(e, CardError) else {"code": type(e).__name__, "message": str(e)})
            if failed and not partial_ok:
                raise _BulkAbort()
    except _BulkAbort:
        pass
    wrote = not dry_run and (not failed or partial_ok)
    if failed and not partial_ok and not dry_run:
        for e in results:
            if e["status"] == "applied":
                e["status"] = "rolled_back"
    ok = not failed or partial_ok
    return {"ok": bool(ok), "dry_run": dry_run, "batch_id": batch_id,
            "applied": sum(1 for e in results if e["status"] == "applied"),
            "would_apply": sum(1 for e in results if e["status"] == "would_apply"),
            "skipped": skipped, "failed": failed, "changes": all_changes if (ok or dry_run) else [],
            "warnings": ([f"{failed} decision(s) can't be applied; " + ("the others were applied." if wrote else "nothing was written.")]
                         if failed else []),
            "items": results}


# --- Undo and the change log (3.13) -------------------------------------------------

def _undo_rows(target):
    """Resolves an audit row id (int / digit string shorter than a batch id) or a batch id."""
    if isinstance(target, bool) or target in (None, ""):
        raise CardError("bad_undo", "Give an audit id or a batch id.")
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit() and len(target) < 16):
        rows = db.get_change_rows(audit_id=int(target))
    else:
        rows = db.get_change_rows(batch_id=str(target))
    if not rows:
        raise CardError("not_found", f"No change-log entry for {target!r}.")
    return rows


def undo(target, *, force=False, dry_run=False, actor=changes.ACTOR_MCP):
    """Reverses a change-log entry or a whole batch (spec 3.13). Inverts every row image in
    reverse order inside one transaction and logs its own row, so an undo is itself undoable.
    REFUSES (writing nothing) when: a row was made by the migration (restore from the
    pre-deploy snapshot instead); an entry is already undone; or any affected row no longer
    equals what the log recorded (someone changed it since: undo_conflict, with the field
    and values). force=True skips the staleness check and applies best-effort."""
    rows = _undo_rows(target)
    for r in rows:
        if r.get("actor") == changes.ACTOR_MIGRATION:
            raise CardError("undo_refused", "That change was made by the migration and can't be undone here; restore "
                            "from the pre-deploy snapshot instead.", {"audit_id": r["id"]})
        if r.get("undone_by") is not None:
            raise CardError("undo_refused", f"Entry {r['id']} was already undone (by entry {r['undone_by']}).",
                            {"audit_id": r["id"], "undone_by": r["undone_by"]})
        if not r["mutations"]:
            raise CardError("undo_refused", f"Entry {r['id']} ({r['op']}) recorded no row images, so it can't be undone.",
                            {"audit_id": r["id"]})
    batch_id = changes.new_batch_id()
    applied, slugs = [], []
    try:
        with db.transaction(dry_run=dry_run):
            conn = db.get_conn()
            for r in sorted(rows, key=lambda x: x["id"], reverse=True):
                slugs += r["affected_slugs"]
                for m in reversed(r["mutations"]):
                    applied.append(db.invert_image(conn, m, force=force))
            undo_id = db.insert_change_log(conn, "undo", actor, applied, batch_id=batch_id,
                                           affected_slugs=sorted(set(slugs)))
            reversed_ids = [r["id"] for r in rows]
            db.mark_change_rows_undone(reversed_ids, undo_id)
            # Undoing an undo re-opens the entries that undo had reversed.
            for r in rows:
                if r["op"] == "undo":
                    orig = [o["id"] for o in _rows_undone_by(r["id"])]
                    db.mark_change_rows_undone(orig, None)
    except db.UndoConflict as e:
        raise CardError("undo_conflict", str(e), e.details)
    flat = _changes_from_log([{"op": "undo", "affected_slugs": sorted(set(slugs)), "mutations": applied}])
    return Result(True, flat, [], batch_id, dry_run,
                  {"undone": [r["id"] for r in rows], "undone_ops": [r["op"] for r in rows]})


def _rows_undone_by(undo_id):
    return [r for r in db.list_change_log(limit=100000) if r.get("undone_by") == undo_id]


def list_changes(card=None, batch_id=None, limit=50):
    """Newest-first change-log rows (spec 7.3): {id, op, actor, batch_id, timestamp,
    affected_slugs, undone_by, mutations}. `card` is a slug or id."""
    slug = None
    if card not in (None, ""):
        c = db.get_project(card)
        slug = c["slug"] if c else str(card)
    return [{"id": r["id"], "op": r["op"], "actor": r["actor"], "batch_id": r["batch_id"],
             "timestamp": r["timestamp"], "affected_slugs": r["affected_slugs"], "undone_by": r.get("undone_by"),
             "mutations": r["mutations"]} for r in db.list_change_log(card_slug=slug, batch_id=batch_id, limit=limit)]
