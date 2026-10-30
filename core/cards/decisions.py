"""A card's live status, and the card_* decisions (piece 1, piece 3's family members): resolve_decision,
decision_summary.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

import time

from .. import actor as actor_ctx, card_rules, changes, db
from ..card_rules import CardError
from .base import (CARD_DECISION_KINDS, CARD_DECISION_PREFIX, card_decision_slug, FAMILY_PATCH_OPS, get_card,
                   is_card_decision_slug, KIND_CARD_BUILT_FOR, KIND_CARD_FAMILY_MEMBERS, KIND_CARD_STATUS,
                   SUPPORTED_PATCH_OPS)
from .ops import set_kind, set_status
from .families import add_to_family, unnest
from .links import link


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
            db._update_card_columns(card_row["id"], original, "resolve_decision_rollback", actor, batch_id=batch_id)
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


def resolve_decision(decision_id, choice=None, choices=None, actor=None, batch_id=None):
    """Resolves one card_* decision. `choice` is an option key (single-choice
    questions); `choices` is a list of option keys (multi-choice questions, e.g.
    several candidate cards). The option's declarative `patch` ops run through the
    same validators a manual edit uses, in one batch; if the patch is invalid on
    today's data the decision stays open and the CardError propagates.

    Returns {"ok", "applied": [keys], "batch_id", "remaining"}.
    """
    from .. import decisions as _decisions  # exceptions live there; lazy (it imports this module)

    decision = db.get_pending_decision(decision_id)
    if decision is None:
        raise _decisions.DecisionNotFound("No such pending decision", details={"decision_id": decision_id})
    if decision["resolved_at"] is not None:
        raise _decisions.DecisionAlreadyResolved("Already resolved", details={"decision_id": decision_id})
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

    batch_id = batch_id or changes.new_batch_id()
    card_row = _decision_card(decision)
    if card_row is None:
        db._resolve_pending_decision(decision_id, {"stale": "card deleted"},
                                     log={"op": "resolve_decision", "actor": actor, "batch_id": batch_id})
        raise _decisions.DecisionNotFound(f"The card for decision {decision_id} no longer exists")

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
    db._resolve_pending_decision(decision_id, {
        "choice": picked[0] if len(picked) == 1 else picked,
        "applied_batch": batch_id,
        # #467 step 2: a person answering through the web (a signed-in user, or the legacy owner-ui
        # actor) is "owner"; the agent and scripts (mcp, token, script, system) are "claude".
        "by": "owner" if (actor_ctx.resolve(actor) == changes.ACTOR_UI
                          or str(actor_ctx.resolve(actor)).startswith("user:")) else "claude",
        "at": time.time(),
    }, log={"op": "resolve_decision", "actor": actor, "batch_id": batch_id})
    return {"ok": True, "applied": picked, "batch_id": batch_id, "remaining": _decisions.count_open()}


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
