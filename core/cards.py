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
"""

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

# Patch ops the decision resolver can apply today. link / add_to_family arrive
# with pieces 4 and 3; an option whose patch needs them stays unresolvable (the
# decision remains open) until then.
SUPPORTED_PATCH_OPS = ("set_status", "set_kind")


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
            db.clear_family_members(row["id"])
        db.update_card_columns(row["id"], fields, "set_kind", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


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
                            f"This answer needs '{op.get('op')}', which isn't available yet "
                            "(typed links arrive with piece 4, families with piece 3). The question stays open.")
    applied = []
    try:
        for op in patch:
            if op["op"] == "set_status":
                set_status(card_row["id"], op["stage"], op.get("stop_reason"), actor=actor, batch_id=batch_id,
                           _op="resolve_decision")
            elif op["op"] == "set_kind":
                set_kind(card_row["id"], op["kind"], actor=actor, batch_id=batch_id)
            applied.append(op)
    except CardError:
        if applied:
            db.update_card_columns(card_row["id"], original, "resolve_decision_rollback", actor, batch_id=batch_id)
        raise


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


def list_needs_decision(kind=None, need=None, hobby=None, limit=None):
    """Open card questions for the owner (spec 6). Piece 1 returns the STORED
    card_* decisions only; the computed needs (missing provenance, hobby flags,
    ...) join in with the pieces that add those fields.

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
    rows.sort(key=lambda r: (r["title"].lower(), r["need"]))
    return rows[:limit] if limit else rows
