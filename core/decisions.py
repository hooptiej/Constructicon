"""Shared decision queue logic for web and MCP (#240/#446/#448).

The "Needs your input" queue is a generic ask-don't-guess mechanism:
- project_match: an upload matched multiple project titles
- retype: a file's pre_store_fn deferred classification to the owner

This module centralizes the list/cleanup and resolve logic so both the web
API and the MCP server use the same decision workflow.
"""

from core import automatch, db, ingest, object_types


class DecisionNotFound(Exception):
    """No pending decision with this ID."""
    pass


class DecisionAlreadyResolved(Exception):
    """The decision has already been resolved."""
    pass


class UnknownDecisionKind(Exception):
    """The decision's kind is not recognized."""
    pass


class InvalidChoice(Exception):
    """A retype answer that isn't one of the question's options. Raised
    rather than resolving with nothing applied, so a typo (e.g. from an MCP
    call) can't silently discard the question."""
    pass


def list_open():
    """List all open pending decisions with automatic stale-cleanup.

    Resolves decisions as stale if:
    - The object (post_slug) has been deleted.
    - A project_match decision has fewer than 2 candidates left.

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
        row = db.get_by_slug(decision["post_slug"])
        if row is None:
            # Object was deleted — mark stale
            db.resolve_pending_decision(decision["id"], {"stale": "object deleted"})
            continue

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
            if len(candidates) < 2:
                # Not ambiguous anymore — fewer than 2 choices remain
                db.resolve_pending_decision(
                    decision["id"], {"stale": "fewer than two candidates remain"}
                )
                continue
            entry["candidates"] = candidates

        elif decision["kind"] == "retype":
            # Filter options to only include registered media types
            payload_options = decision["payload"].get("options", [])
            registered_keys = set(object_types.OBJECT_TYPES.keys())
            entry["options"] = [o for o in payload_options if o.get("key") in registered_keys]
            entry["question"] = decision["payload"].get("question", "")
            entry["current_type"] = row.get("media_type")

        items.append(entry)

    return items


def resolve(decision_id, choice="", project_ids=()):
    """Resolve a pending decision with the owner's choice.

    For project_match: `project_ids` is a tuple/list of project IDs to attach to.
    For retype: `choice` is the media_type key to retype to.

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
        raise DecisionNotFound(f"No such pending decision: {decision_id}")
    if decision["resolved_at"] is not None:
        raise DecisionAlreadyResolved(f"Decision {decision_id} already resolved")

    applied = []

    if decision["kind"] == automatch.KIND_PROJECT_MATCH:
        allowed = {int(pid) for pid in decision["payload"].get("candidate_project_ids", [])}
        chosen = []
        for raw in project_ids:
            raw = str(raw).strip()
            if raw.isdigit() and int(raw) in allowed and int(raw) not in chosen:
                chosen.append(int(raw))
        if db.get_by_slug(decision["post_slug"]) is not None:
            for pid in chosen:
                ingest.attach_to_project(decision["post_slug"], pid)
                applied.append(pid)
        db.resolve_pending_decision(decision_id, {"project_ids": applied})

    elif decision["kind"] == "retype":
        allowed_keys = {o["key"] for o in decision["payload"].get("options", [])}
        if choice and choice not in allowed_keys:
            raise InvalidChoice(f"'{choice}' is not one of this question's options: {sorted(allowed_keys)}")
        row = db.get_by_slug(decision["post_slug"])
        if row is not None and choice:
            if choice in allowed_keys:
                # Only call retype if the choice is different from the current type
                if choice != row.get("media_type"):
                    ingest.retype(decision["post_slug"], choice, lambda f, *args: None)
                    applied.append(choice)
                else:
                    # Choice matches current type — just resolve without retying
                    applied.append(choice)
        db.resolve_pending_decision(decision_id, {
            "choice": choice or None,
            "kept": bool(row and choice and choice == row.get("media_type")),
        })

    else:
        raise UnknownDecisionKind(f"Unknown decision kind: {decision['kind']}")

    return {"ok": True, "applied": applied, "remaining": db.count_pending_decisions()}
