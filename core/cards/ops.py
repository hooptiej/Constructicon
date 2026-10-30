"""One-card setters: status, kind, whereabouts, provenance, highlight (spec 3.2-3.5, 3.12), plus the
provenance suggestion and the whereabouts fields they show with.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import card_rules, changes, db
from ..card_rules import CardError
from .base import _change_rows, get_card, Result, _status_warnings


def set_status(card, stage, stop_reason=None, *, activity=None, dry_run=False, actor=None, batch_id=None,
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
        db._update_card_columns(row["id"], triple, _op, actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def set_kind(card, kind, *, force=False, dry_run=False, actor=None, batch_id=None):
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
            db._clear_family_members(row["id"], op="set_kind", actor=actor, batch_id=batch_id,
                                    affected_slugs=[row["slug"]])
        db._update_card_columns(row["id"], fields, "set_kind", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


# --- Whereabouts, card provenance, highlight (3.4, 3.5, 3.12) ---------------------

def set_whereabouts(card, whereabouts=None, note=..., *, dry_run=False, actor=None, batch_id=None):
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
        db._update_card_columns(row["id"], fields, "set_whereabouts", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def set_provenance(card, provenance=None, credit=..., *, dry_run=False, actor=None, batch_id=None):
    """Sets (or clears, with None) the CARD's provenance and optionally its credit
    (who designed it / where it came from; credit=... leaves it alone). Distinct from
    the per-file provenance (db._set_provenance / constructicon_update fields.provenance), which
    is untouched. One value per card: mixed origins are separate cards (3.5)."""
    row = get_card(card)
    value = card_rules.validate_provenance(provenance, current=row.get("provenance"))
    fields = {"provenance": value}
    if credit is not ...:
        fields["provenance_credit"] = (credit or "").strip() or None
    rows = _change_rows(row["slug"], {c: row.get(c) for c in fields}, fields)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db._update_card_columns(row["id"], fields, "set_provenance", actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def set_highlight(card, on, *, dry_run=False, actor=None, batch_id=None):
    """The card's own 0/1 "this one is special" flag (3.12). Independent of the
    per-file highlight (capture_events.highlight), which is untouched."""
    row = get_card(card)
    fields = {"highlight": 1 if on else 0}
    rows = _change_rows(row["slug"], {"highlight": row.get("highlight") or 0}, fields)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db._update_card_columns(row["id"], fields, "set_highlight", actor, batch_id=batch_id)
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
