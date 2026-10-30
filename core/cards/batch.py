"""Bulk (6): one op over many cards in one transaction, and resolve_decisions (many decisions at once).

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import card_rules, changes, db
from ..card_rules import CardError
from ..errors import AppError
from .base import CARD_DECISION_KINDS, CARD_DECISION_PREFIX, _changes_from_log, is_card_decision_slug
from .ops import set_highlight, set_kind, set_provenance, set_status, set_whereabouts
from .families import add_to_family, nest, remove_from_family, unnest
from .links import link, retype_link, unlink
from .hobbies import set_hobby_activity
from .decisions import resolve_decision
from .reshape import add_to_hobby, remove_from_hobby, set_home


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


def bulk(op, items, *, dry_run=True, partial_ok=False, actor=None, batch_id=None):
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
    except _BulkAbort:  # silent-ok: sentinel that unwinds the transaction; every failure is in `results`
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

def resolve_decisions(items, *, accept_suggested=False, dry_run=True, partial_ok=False, actor=None,
                      batch_id=None):
    """Clears many card decisions at once (spec 7.3). DRY-RUN BY DEFAULT.

    `items`: [{decision_id, choice | choices}], or with accept_suggested=True a list of
    decision ids (or {decision_id}); each is answered with its own suggested option. A
    decision with no usable suggestion is SKIPPED (reported, not an error). Items run in one
    transaction and one batch_id; a decision that can't be applied makes the whole call
    write nothing unless partial_ok=True. Returns {ok, dry_run, batch_id, applied, skipped,
    failed, changes, items: [{decision_id, card_slug, choice, status: applied|would_apply|
    skipped|failed, changes?, reason?, error?}]}."""
    from .. import decisions as _decisions
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
                except AppError as e:  # #548: CardError and the decisions.* refusals share one shape
                    failed += 1
                    entry.update(status="failed", error=e.to_dict())
            if failed and not partial_ok:
                raise _BulkAbort()
    except _BulkAbort:  # silent-ok: sentinel that unwinds the transaction; every failure is in `results`
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
