"""The MCP tools of one area. The Curator: needs, queue_item, revision, resolve_pending_decision,
run_type_action (+ aliases).

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server.shapes import _to_public
from mcp_server._common import RESERVED_ACTION, _needs_caption, _only_for, _retry_ocr

from core import policy, cards, curation_queue, curator_needs, db, decisions, errors, item_title, object_types
from core import revisions
from core.errors import InvalidInput, NotFound


# --- Curator: the queue, nudges, needs and questions ---

def _nudges(kind, limit):
    """Curator nudges (dismissed ones left out), filtered by kind, then capped."""
    needs = curator_needs.list_needs()
    if kind is not None:
        needs = [n for n in needs if n["kind"] == kind]
    if limit is not None:
        needs = needs[:limit]
    return needs


def _queue(card, include_deferred):
    """The Curator queue as the caller may see it (curation_queue.for_actor: the registered door;
    unchanged for an admin)."""
    q = curation_queue.for_actor(curation_queue.build_queue(card=card))
    if not include_deferred:
        q = {**q, "deferred": []}
    return q


@mcp.tool(selector="view")
def constructicon_needs(view: typing.Literal["queue", "nudges", "caption", "card_decisions", "pending"] = "queue",
                        card: str | int | None = None, include_deferred: bool = True, nudge_kind: str | None = None,
                        card_kind: str | None = None, need: str | None = None, hobby: str | None = None,
                        limit: int | None = None, include_failed: bool = True) -> dict | list:
    """What waits on curation, in one of five views:
    "queue" (default; card, include_deferred): every open question, nudge and need, grouped by
      card, as the owner's Curator tab shows it -> {groups, deferred, counts}. Groups: cards
      (active first, then most recently touched), then hobby / uploads / collection buckets;
      inside, questions first. Item: {type: question | nudge | need, key, label, kind, group,
      href, deferred, dismissible} (+ for a question: id, options, suggested, confidence, multi).
      counts.open is the tab's badge.
    "card_decisions" (card, card_kind, need, hobby, limit): cards waiting on a decision, each with
      a suggestion: [{need, card_slug, hobby_slug, title, detail, suggested, suggested_reason,
      confidence, decision_id, options, link?}]. Stored questions (card_status, card_built_for,
      card_kind, card_family_members: answer with constructicon_resolve_pending_decision or
      constructicon_resolve_decisions) and computed needs (hobby_inactive_with_active_work,
      hobby_active_untouched, untyped_link, missing_provenance, missing_provenance_credit,
      missing_whereabouts, status_conflict, blank_writeup_with_files: fix the field).
      card_kind "hobby" = hobby needs only.
    "pending" (no filters): every open stored question with its options: card questions,
      project_match (candidates), retype (options; the item's current type = keep it),
      item_supersedes (options, suggested).
    "nudges" (nudge_kind, limit): Curator nudges by priority: [{nudge_key, kind, target_type,
      target_id, target_slug, title, summary, priority, base_impact, status_weight, action,
      deferred}].
    "caption" (limit default 50, max 500; include_failed): items with no caption yet (never
      sensitive) -> {items: [{slug, name, type, filename, caption_status, projects, hobbies}],
      total}. Then constructicon_view and constructicon_set_caption.
    Act on an item with constructicon_queue_item(key, action).
    """
    defaults = {"card": (card, None), "include_deferred": (include_deferred, True), "nudge_kind": (nudge_kind, None),
                "card_kind": (card_kind, None), "need": (need, None), "hobby": (hobby, None), "limit": (limit, None),
                "include_failed": (include_failed, True)}
    applies = {"queue": ("card", "include_deferred"), "nudges": ("nudge_kind", "limit"),
               "caption": ("limit", "include_failed"), "card_decisions": ("card", "card_kind", "need", "hobby", "limit"),
               "pending": ()}
    if view not in applies:
        raise InvalidInput(f"Unknown view {view!r}: use {', '.join(applies)}.")
    _only_for(f'view="{view}"', **{k: v for k, v in defaults.items() if k not in applies[view]})
    if view == "queue":
        return _queue(card, include_deferred)
    if view == "nudges":
        return _nudges(nudge_kind, limit)
    if view == "caption":
        return _needs_caption(50 if limit is None else limit, include_failed)
    if view == "card_decisions":
        return cards.list_needs_decision(kind=card_kind, need=need, hobby=hobby, limit=limit, card=card)
    return _pending_decisions()


@mcp.tool(alias_of=("constructicon_needs", "nudges"))
def constructicon_list_needs(kind: str | None = None, limit: int | None = None) -> list[dict]:
    return _nudges(kind, limit)


@mcp.tool(alias_of=("constructicon_needs", "queue"))
def constructicon_curation_queue(card: str | None = None, include_deferred: bool = True) -> dict:
    return _queue(card, include_deferred)


@mcp.tool(selector="action")
def constructicon_queue_item(key: str, action: typing.Literal["defer", "bring_back", "dismiss"]) -> dict:
    """Act on one Curator queue item by its `key` (from constructicon_needs: "decision:<id>",
    "<kind>:project:<id>", "need:<need>:<slug>"):
    "defer": set it aside in the Deferred section, no timer, until answered or brought back.
    "bring_back": back into the main queue (a dismissed one is not).
    "dismiss": a nudge or need, for good. A question can't be dismissed: answer or defer it.
    Logged and undoable. -> {ok, changed}.
    """
    if action == "dismiss":
        return curation_queue.dismiss(key)  # a QueueError (400 bad_request) becomes the shared error shape
    if action == "defer":
        return curation_queue.defer(key)
    if action == "bring_back":
        return curation_queue.bring_back(key)
    raise InvalidInput(f"Unknown action {action!r}: use defer, bring_back or dismiss.")


@mcp.tool(alias_of=("constructicon_queue_item", "dismiss"))
def constructicon_dismiss_need(nudge_key: str) -> dict:
    return curation_queue.dismiss(nudge_key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool(alias_of=("constructicon_queue_item", "defer"))
def constructicon_defer(key: str) -> dict:
    return curation_queue.defer(key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool(alias_of=("constructicon_queue_item", "bring_back"))
def constructicon_bring_back(key: str) -> dict:
    return curation_queue.bring_back(key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool(alias_of=("constructicon_needs", "pending"))
def constructicon_list_pending_decisions() -> list[dict]:
    return _pending_decisions()


def _pending_decisions():
    """Every open stored question (decisions.list_open: leaves out stale ones and questions about
    items the caller can't see; resolves nothing), shaped per kind."""
    result = []
    for item in decisions.list_open():
        if cards.is_card_decision_slug(item["post_slug"]):
            # V2 card question: slug is "card:<project slug>"; carries the suggested answer.
            summary = cards.decision_summary({"id": item["id"], "kind": item["kind"],
                                              "post_slug": item["post_slug"], "payload": item["payload"]})
            result.append({
                "id": item["id"],
                "kind": item["kind"],
                "slug": item["post_slug"],
                "card_slug": summary["card_slug"],
                "title": summary["title"],
                "media_type": None,
                "created_at": item["created_at"],
                "question": summary["question"],
                "legacy_status": summary["legacy_status"],
                "provisional": summary["provisional"],
                "suggested": summary["suggested"],
                "suggested_reason": summary["suggested_reason"],
                "confidence": summary["confidence"],
                "options": summary["options"],
            })
            continue
        row = item["row"]
        title = item_title.title_of(row)

        entry = {
            "id": item["id"],
            "kind": item["kind"],
            "slug": item["post_slug"],
            "title": title,
            "media_type": row.get("media_type"),
            "created_at": item["created_at"],
        }

        if item["kind"] == "project_match":
            entry["candidates"] = item.get("candidates", [])
        elif item["kind"] == "retype":
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
        elif item["kind"] == revisions.KIND_ITEM_SUPERSEDES:
            # #477: options are the earlier files this upload might replace (key = their slug) + "none".
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
            entry["suggested"] = item.get("suggested")

        result.append(entry)

    return result


@mcp.tool(selector="action")
def constructicon_revision(slug: str, action: typing.Literal["supersede", "remove"], new: str | None = None,
                           dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Revision chains of files (linear: A -> B -> C; the CURRENT one has nothing newer). An older
    revision shows "Superseded" and leaves browse listings (search still finds it).
    "supersede" (slug = the older file, new = the file that replaces it): -> {ok, dry_run,
      batch_id, old, new, chain}. Errors: bad_revision (self-link, redacted), revision_cycle,
      revision_conflict (old already has a newer one, or new already replaces another).
    "remove": take `slug` out and close the gap (A -> B -> C minus B is A -> C). A no-op
      (removed: false) for a file in no chain. -> {ok, dry_run, batch_id, slug, removed, chain}.
    Read a chain with constructicon_get(slug, include=["revisions"]). Upload-time "does this
    replace ...?" questions are the owner's (constructicon_resolve_pending_decision).
    """
    if action == "supersede":
        if not new:
            raise InvalidInput('action="supersede" needs new (the item that replaces slug).')
        return revisions.mark_superseded(slug, new, batch_id=batch_id, dry_run=dry_run)
    if action == "remove":
        _only_for('action="remove"', new=(new, None))
        return revisions.remove_from_chain(slug, batch_id=batch_id, dry_run=dry_run)
    raise InvalidInput(f"Unknown action {action!r}: use supersede or remove.")


@mcp.tool(alias_of=("constructicon_revision", "supersede"))
def constructicon_mark_superseded(old: str, new: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    return revisions.mark_superseded(old, new, batch_id=batch_id, dry_run=dry_run)


@mcp.tool(alias_of=("constructicon_revision", "remove"))
def constructicon_remove_from_revisions(slug: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    return revisions.remove_from_chain(slug, batch_id=batch_id, dry_run=dry_run)


@mcp.tool()
def constructicon_resolve_pending_decision(decision_id: int, choice: str = "", project_ids: list[int] | None = None,
                                           choices: list[str] | None = None) -> dict:
    """Answer one stored question (see constructicon_needs(view="pending")), applied in one batch.
    project_match: project_ids = the cards to attach the file to ([] = none of these).
    retype: choice = the type to keep it as (the item's current type keeps it as is).
    item_supersedes: choice = the older file's slug (this one replaces it), "reverse:<slug>",
      "same:<slug>" (identical: this upload goes to the trash) or "none".
    card questions: choice = an option key, or choices = several for card_built_for /
      card_family_members (a family answer makes the card a family with the picked members).
    An answer that breaks a rule leaves the question open and returns that rule's error.
    -> {ok, applied, remaining}. Errors: already_resolved, unknown_decision_kind, invalid_choice.
    """
    if project_ids is None:
        project_ids = []

    return decisions.resolve(decision_id, choice=choice, project_ids=project_ids, choices=choices or ())


@mcp.tool()
def constructicon_run_type_action(slug: str, action: str, params: dict | None = None) -> dict:
    """Run an action on an item. action: one of the item's `actions` (from constructicon_get, e.g.
    "fetch_youtube_metadata"), or the built-in "retry_ocr": run text extraction / OCR again on any
    OCR-capable item (in the background; ocr_status goes "pending").
    params: for an action that takes them (else refused). Archive "unpack" (turn the archive into a
    card of its members): {"mode": "preview"} (default; lists members, sizes, what is skipped
    and why, what already exists byte-identical and will be linked, the cards it would make;
    nothing written), {"mode": "start"} (queues the conversion; the web app runs it in the
    background; -> job with id and batch_id), {"mode": "status", "job": <id>} (progress, then the
    result; partial failures name each member and why), plus "shape": "collection" (flat, default)
    | "project" (flat) | "nested" (a project, sub-folders as nested cards) and "depth": 1 or 2 (nested
    only, default 2). The whole conversion undoes with constructicon_undo(batch_id).
    -> {ok, action, item, ...what the action adds}. Errors: redacted, unknown_action,
    action_failed; retry_ocr: bad_request (redacted, or a type without OCR); unpack: archive_encrypted,
    archive_corrupt, archive_hostile_path, archive_too_big, archive_too_many, archive_ratio,
    archive_too_slow, archive_unsupported, bad_shape, unpack_busy, nothing_to_unpack.
    """
    if action == RESERVED_ACTION:
        return {"ok": True, "action": action, "item": _retry_ocr(slug)}
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #746: was missing (the old net never reached it)

    if row.get("redacted"):
        raise InvalidInput("object is redacted", code="redacted")

    spec = object_types.get_object_type(row.get("media_type"))
    action_obj = next((a for a in spec.actions if a.key == action), None)
    if action_obj is None:
        raise InvalidInput(f"{spec.label} has no action '{action}'", code="unknown_action")
    if not action_obj.applies_to(row):  # #446: e.g. Reclassify is .exe-only
        raise InvalidInput(f"{spec.label} has no action '{action}' for this item", code="unknown_action")

    try:
        result = action_obj.run(row, params)  # #605: an action that takes params gets them; others refuse them
    except errors.AppError:
        raise
    except Exception as e:
        print(f"Action '{action}' failed: {e!r}", flush=True)
        raise errors.AppError("action_failed", f"Action '{action}' failed: {e}", status=500) from e

    updated_row = db.get_by_slug(slug)
    return {
        "ok": True,
        "action": action,
        **(result or {}),
        "item": _to_public(updated_row)
    }
