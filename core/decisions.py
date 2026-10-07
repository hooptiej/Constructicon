"""Shared decision queue logic for web and MCP (#240/#446/#448).

The "Needs your input" queue is a generic ask-don't-guess mechanism:
- project_match: an upload matched multiple project titles
- retype: a file's pre_store_fn deferred classification to the owner
- item_supersedes: a new file looks like another revision of an existing one (#477);
  answered with a candidate slug (creates the supersedes link) or "none"

This module centralizes the list and resolve logic so both the web API and the
MCP server use the same decision workflow.

#551 item 3 / #541 phase D: reads never write. list_open() leaves out a question that
can't be answered any more (stale_reason); the explicit, logged, undoable
sweep_stale() resolves those (the web worker runs it at startup and hourly; MCP
constructicon_sweep_stale_decisions). Answering a question (resolve) is imaged in the
same batch as whatever the answer applied.
"""

import json
import time

from core import automatch, cards, changes, db, ingest, items, membership, object_types, policy, revisions
from core.errors import Conflict, InvalidInput, NotFound


class DecisionNotFound(NotFound):
    """No pending decision with this ID. (#548: an AppError, 404 not_found.)"""


class DecisionAlreadyResolved(Conflict):
    """The decision has already been resolved. (409 already_resolved.)"""
    default_code = "already_resolved"


class UnknownDecisionKind(InvalidInput):
    """The decision's kind is not recognized. (400 unknown_decision_kind.)"""
    default_code = "unknown_decision_kind"


class InvalidChoice(InvalidInput):
    """A retype answer that isn't one of the question's options. Raised
    rather than resolving with nothing applied, so a typo (e.g. from an MCP
    call) can't silently discard the question. (400 invalid_choice.)"""
    default_code = "invalid_choice"


OP_SWEEP = "sweep_stale_decisions"
OP_RESOLVE = "resolve_decision"

# The web worker's sweep (web/app.py, _decision_sweep_loop): once at startup, after the
# migrations, then every SWEEP_INTERVAL_SECONDS.
SWEEP_INTERVAL_SECONDS = 3600

STALE_CARD_DELETED = "card deleted"
STALE_OBJECT_DELETED = "object deleted"
STALE_FEW_CANDIDATES = "fewer than two candidates remain"
STALE_NO_CANDIDATES = "no candidates left"


def stale_reason(decision):
    """Why an OPEN decision can no longer be answered, or None while it still can. A pure read
    (#551 item 3): the exact rules list_open() used to apply while it wrote, unchanged:
    - a card question (post_slug "card:<slug>") whose card is gone -> "card deleted"
      (validated against `projects`, never capture_events: spec 4.3);
    - a file question whose object is gone -> "object deleted";
    - project_match with fewer than 2 of its candidate cards left;
    - item_supersedes with no live candidate left (revisions.live_candidates).
    retype questions only go stale with their object."""
    if cards.is_card_decision_slug(decision["post_slug"]):
        return STALE_CARD_DELETED if cards._decision_card(decision) is None else None
    if db.get_by_slug(decision["post_slug"]) is None:
        return STALE_OBJECT_DELETED
    if decision["kind"] == automatch.KIND_PROJECT_MATCH:
        live = [pid for pid in decision["payload"].get("candidate_project_ids", []) if db.get_project(pid) is not None]
        return STALE_FEW_CANDIDATES if len(live) < 2 else None
    if decision["kind"] == revisions.KIND_ITEM_SUPERSEDES:
        return STALE_NO_CANDIDATES if not revisions.live_candidates(decision) else None
    return None


def sweep_stale(*, dry_run=False, actor=None, batch_id=None):
    """Resolves every open decision that stale_reason() says can't be answered any more, with
    {"stale": <reason>} as its resolution, exactly as list_open() used to on every read. Now an
    explicit op (#551 item 3): one transaction, each resolution imaged (one change-log row, op
    sweep_stale_decisions), so `cards.undo(batch_id)` re-opens them. Nothing stale = nothing
    written and no change-log row. Run by the web worker (startup + hourly, as `system`) and by
    MCP constructicon_sweep_stale_decisions. data: {resolved: [{id, kind, post_slug, reason}],
    count, by_kind}."""
    batch_id = batch_id or changes.new_batch_id()
    resolved = []
    with db.transaction(dry_run=dry_run):
        stale = []
        for d in db.list_pending_decisions():
            reason = stale_reason(d)
            if reason:
                stale.append((d, reason))
        if stale:
            now = time.time()
            with db.ImageLog(OP_SWEEP, actor, batch_id, [d["post_slug"] for d, _ in stale]) as log:
                for d, reason in stale:
                    # the same resolution shape db._resolve_pending_decision writes
                    payload = {**(d["payload"] or {}), "resolution": {"stale": reason}}
                    log.update("pending_decisions", {"id": d["id"]}, {"resolved_at": now, "payload": json.dumps(payload)})
                    resolved.append({"id": d["id"], "kind": d["kind"], "post_slug": d["post_slug"], "reason": reason})
        rows = db.get_change_rows(batch_id=batch_id) if stale else []
    by_kind = {}
    for r in resolved:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    return cards.Result(True, cards._changes_from_log(rows), [], batch_id, dry_run,
                        {"resolved": resolved, "count": len(resolved), "by_kind": by_kind})


def count_open():
    """How many questions are open and answerable: what list_open() returns (stale-but-not-yet-
    swept questions are left out, as list_open() leaves them out)."""
    return len(list_open())


def list_open():
    """List all open pending decisions that can still be answered. A pure read: it never writes
    (#551 item 3). A decision stale_reason() calls stale is left out (it used to be resolved here,
    unlogged, on every GET); the explicit, logged sweep_stale() resolves it later.

    Returns a list of dicts, each with:
        {
            "id": int,
            "kind": "project_match" | "retype",
            "created_at": float (UTC unix seconds),
            "post_slug": str,
            "payload": dict,
            "row": dict (the capture_events row),
            "candidates": [{"id": int, "title": str, "slug": str}] (project_match only),
            "options": [{"key": str, "label": str, ...}] (retype only, filtered),
            "question": str (retype only),
            "current_type": str (retype only, row's media_type),
        }
    """
    items = []
    for decision in db.list_pending_decisions():
        if stale_reason(decision):
            continue  # left for the sweep (sweep_stale), never resolved by a read
        if cards.is_card_decision_slug(decision["post_slug"]):
            # V2 card decision (post_slug = "card:<project slug>"): validated against
            # `projects`, NOT capture_events (stale_reason, spec 4.3).
            card_row = cards._decision_card(decision)
            payload = decision["payload"]
            items.append({
                "id": decision["id"],
                "kind": decision["kind"],
                "created_at": decision["created_at"],
                "post_slug": decision["post_slug"],
                "payload": payload,
                "row": card_row,
                "card": card_row,
                "question": payload.get("question", ""),
                "options": payload.get("options", []),
                "suggested": payload.get("suggested"),
                "suggested_reason": payload.get("suggested_reason"),
                "confidence": payload.get("confidence"),
            })
            continue
        row = db.get_by_slug(decision["post_slug"])
        if row is not None and not policy.can_view(row):
            continue  # #467 step 2: a question about a restricted item is for admins only

        entry = {
            "id": decision["id"],
            "kind": decision["kind"],
            "created_at": decision["created_at"],
            "post_slug": decision["post_slug"],
            "payload": decision["payload"],
            "row": row,
        }

        if decision["kind"] == automatch.KIND_PROJECT_MATCH:
            candidates = []
            for pid in decision["payload"].get("candidate_project_ids", []):
                project = db.get_project(pid)
                if project is not None:
                    candidates.append(
                        {"id": project["id"], "title": project["title"], "slug": project["slug"]}
                    )
            entry["candidates"] = candidates  # >= 2 (stale_reason)

        elif decision["kind"] == "retype":
            # Filter options to only include registered media types
            payload_options = decision["payload"].get("options", [])
            registered_keys = set(object_types.OBJECT_TYPES.keys())
            entry["options"] = [o for o in payload_options if o.get("key") in registered_keys]
            entry["question"] = decision["payload"].get("question", "")
            entry["current_type"] = row.get("media_type")

        elif decision["kind"] == revisions.KIND_ITEM_SUPERSEDES:
            # #477: "does this replace ...?" -- drop candidates that have since been superseded
            # or removed; with none left (or the file already linked by hand) it's stale.
            live = revisions.live_candidates(decision)  # non-empty (stale_reason)
            payload = decision["payload"]
            # #586: option keys are a bare candidate slug, "reverse:<slug>", "same:<slug>" or "none".
            def _live_key(k):
                action, cand = revisions.option_target(k)
                return action == "none" or cand in live
            entry["options"] = [o for o in payload.get("options", []) if _live_key(o["key"])]
            entry["question"] = payload.get("question", "")
            sug = payload.get("suggested")
            entry["suggested"] = sug if sug and _live_key(sug) else None
            entry["suggested_reason"] = payload.get("suggested_reason") if entry["suggested"] else None
            entry["confidence"] = payload.get("confidence") if entry["suggested"] else None

        items.append(entry)

    return items


def resolve(decision_id, choice="", project_ids=(), choices=(), actor=None):
    """Resolve a pending decision with the owner's choice.

    For project_match: `project_ids` is a tuple/list of project IDs to attach to.
    For retype: `choice` is the media_type key to retype to.
    For the V2 card_* kinds: `choice` is one option key (or `choices` several);
    the option's patch runs through core.cards. May raise card_rules.CardError
    (the decision then stays open); `actor` defaults to the current actor context (#560).

    Returns a dict:
        {"ok": True, "applied": [...], "remaining": count}

    The "applied" list contains:
    - For project_match: the project IDs that were actually attached.
    - For retype: a list containing the choice key if the retype succeeded.

    Raises:
        DecisionNotFound: no pending decision with this ID.
        DecisionAlreadyResolved: decision.resolved_at is not None.
        UnknownDecisionKind: decision.kind is not recognized.
    """
    decision = db.get_pending_decision(decision_id)
    if decision is None:
        raise DecisionNotFound("No such pending decision", details={"decision_id": decision_id})
    if decision["resolved_at"] is not None:
        raise DecisionAlreadyResolved("Already resolved", details={"decision_id": decision_id})

    applied = []

    if cards.is_card_decision_slug(decision["post_slug"]):
        return cards.resolve_decision(decision_id, choice=choice or None, choices=list(choices or []), actor=actor)

    # #541 phase D: what the answer applies and the resolution itself share ONE batch, and the
    # resolution is imaged, so one undo re-opens the question and reverses what it did.
    batch_id = changes.new_batch_id()
    log = {"op": OP_RESOLVE, "actor": actor, "batch_id": batch_id}

    if decision["kind"] == automatch.KIND_PROJECT_MATCH:
        allowed = {int(pid) for pid in decision["payload"].get("candidate_project_ids", [])}
        chosen = []
        for raw in project_ids:
            raw = str(raw).strip()
            if raw.isdigit() and int(raw) in allowed and int(raw) not in chosen:
                chosen.append(int(raw))
        if db.get_by_slug(decision["post_slug"]) is not None:
            for pid in chosen:
                if db.get_project(pid) is None:
                    continue  # a candidate deleted meanwhile is skipped, as attach_to_project did
                membership.add_files(pid, [decision["post_slug"]], batch_id=batch_id, actor=actor,
                                     **membership.UI_EFFECTS)
                applied.append(pid)
        db._resolve_pending_decision(decision_id, {"project_ids": applied}, log=log)

    elif decision["kind"] == "retype":
        allowed_keys = {o["key"] for o in decision["payload"].get("options", [])}
        if choice and choice not in allowed_keys:
            raise InvalidChoice(f"'{choice}' is not one of this question's options: {sorted(allowed_keys)}")
        row = db.get_by_slug(decision["post_slug"])
        if row is not None and choice:
            if choice in allowed_keys:
                # Only call retype if the choice is different from the current type
                if choice != row.get("media_type"):
                    items.retype(decision["post_slug"], choice, ingest.run_in_thread, actor=actor, batch_id=batch_id)
                    applied.append(choice)
                else:
                    # Choice matches current type — just resolve without retying
                    applied.append(choice)
        db._resolve_pending_decision(decision_id, {
            "choice": choice or None,
            "kept": bool(row and choice and choice == row.get("media_type")),
        }, log=log)

    elif decision["kind"] == revisions.KIND_ITEM_SUPERSEDES:
        allowed_keys = {o["key"] for o in decision["payload"].get("options", [])}
        if choice not in allowed_keys:
            raise InvalidChoice(f"'{choice}' is not one of this question's options: {sorted(allowed_keys)}")
        # May raise card_rules.CardError (e.g. the candidate was superseded meanwhile): the decision stays open.
        return revisions.resolve_decision(decision, choice, actor=actor)

    else:
        raise UnknownDecisionKind(f"Unknown decision kind: {decision['kind']}")

    return {"ok": True, "applied": applied, "batch_id": batch_id, "remaining": count_open()}


def close_retype_questions(slug, new_type, *, actor=None, batch_id=None):
    """The .exe Reclassify action answers any open "installer or app?" question about the file:
    resolve them (imaged, in the caller's batch) with the type it chose."""
    batch_id = batch_id or changes.new_batch_id()
    closed = []
    for decision in db.list_pending_decisions("retype"):
        if decision["post_slug"] == slug:
            db._resolve_pending_decision(decision["id"], {"choice": new_type, "via": "reclassify action"},
                                         log={"op": OP_RESOLVE, "actor": actor, "batch_id": batch_id})
            closed.append(decision["id"])
    return closed
