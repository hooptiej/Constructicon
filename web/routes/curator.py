"""Curator routes (#547): /api/curator/* and /api/pending-decisions*."""

from fastapi import Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from core import automatch, cards, curation_queue, curator, curator_needs, db, decisions, revisions
from web.common import templates
from web.shapes import _friendly_datetime, _project_effective_cover_url, _to_card_face, _to_content_public
from core import roles
from web.roles import RoleRouter, requires

router = RoleRouter(default_role=roles.VIEWER)  # #557: routes without their own label are viewer


@router.get("/api/pending-decisions")
def api_list_pending_decisions():
    """#240/#446/#448: every open "ask, don't guess" question for the admin
    page's queue — kind="project_match" (an upload matched multiple projects)
    or "retype" (a file's type was deferred for owner input). Each entry
    carries the object it's about (slim card shape) and candidate/option info.
    Decisions whose object or candidates have since vanished are resolved
    as "stale" rather than shown as unanswerable."""
    items = []
    for item in decisions.list_open():
        if cards.is_card_decision_slug(item["post_slug"]):
            # V2 card question: "post" is the card, shaped like a file post so the
            # admin queue can render it (title, link, thumbnail).
            card_row = item["card"]
            cover = _project_effective_cover_url(card_row)
            items.append({
                "id": item["id"],
                "kind": item["kind"],
                "created_at": item["created_at"],
                "created_at_display": _friendly_datetime(item["created_at"]),
                "post": {
                    "slug": card_row["slug"],
                    "title": card_row["title"],
                    "link": f"/project/{card_row['slug']}",
                    "thumb_url": cover,
                    "type_icon": "",
                },
                "payload": item["payload"],
                "question": item["question"],
                "options": [
                    {"key": o["key"], "label": o.get("label", o["key"]), "reason": o.get("reason"),
                     # `suggested` is one key, or a list of keys for a multi-pick question
                     # (card_family_members pre-ticks every suggested-yes candidate).
                     "suggested": (o["key"] in item["suggested"]) if isinstance(item["suggested"], list)
                     else o["key"] == item["suggested"]}
                    for o in item["options"]
                ],
                "suggested": item["suggested"],
                "suggested_reason": item["suggested_reason"],
                "confidence": item["confidence"],
                "multi": item["kind"] in (cards.KIND_CARD_BUILT_FOR, cards.KIND_CARD_FAMILY_MEMBERS),
            })
            continue
        entry = {
            "id": item["id"],
            "kind": item["kind"],
            "created_at": item["created_at"],
            "created_at_display": _friendly_datetime(item["created_at"]),
            "post": _to_content_public(item["row"]),
            "payload": item["payload"],
        }
        if item["kind"] == automatch.KIND_PROJECT_MATCH:
            entry["candidates"] = item.get("candidates", [])
        elif item["kind"] == "retype":
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
            entry["current_type"] = item.get("current_type")
        elif item["kind"] == revisions.KIND_ITEM_SUPERSEDES:
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
            entry["suggested"] = item.get("suggested")
        items.append(entry)
    return JSONResponse({"count": len(items), "items": items})


@router.post("/api/pending-decisions/{decision_id}/resolve", dependencies=requires(roles.EDITOR))
def api_resolve_pending_decision(decision_id: int, project_ids: list[str] = Form([]), choice: str = Form(""),
                                 choices: list[str] = Form([])):
    """#240/#446/#448: resolve a pending decision with the owner's choice.

    For project_match, `project_ids` is whichever candidates were ticked — zero ("none of these"),
    one, or several, since an item can belong to multiple projects. Attaches via
    ingest.attach_to_project so the linked tag / cover behavior matches a drawer pick.

    For retype, `choice` is the key of the chosen option from the decision's options list.

    For the V2 card_* kinds, `choice` (or several `choices`) is an option key; the
    option's patch runs through core.cards, and a rule violation comes back as the
    shared CardError response (422/409) with the decision left open.

    #548: the decisions.* refusals are AppErrors (404 not_found, 409 already_resolved,
    400 unknown_decision_kind / invalid_choice), turned into the shared error shape by
    the app-wide handler; `detail` is the same text as before."""
    return JSONResponse(decisions.resolve(decision_id, choice=choice, project_ids=project_ids, choices=choices))


# --- Curator (Stage 2) ---

@router.get("/api/curator/dashboard")
def api_curator_dashboard(request: Request):
    """Curator Stage 2 dashboard: aggregate project health scoring.

    Returns:
        - projects_by_status: count per effective status
        - average_health: average score of LIVE projects (wip/complete/published)
        - gap_buckets: counts of projects failing each specific check
        - unfiled_count: count of capture_events not in any project
        - all_project_scores: detailed scoring for every project
    """
    return JSONResponse(curator.score_all_projects())


# --- Curator Stage 3a: Nudges ---

@router.get("/api/curator/needs")
def api_curator_list_needs(request: Request, kind: str | None = None, limit: int | None = None):
    """Curator Stage 3a: list current nudges (actionable needs).

    Returns a list of nudge dicts, sorted by priority DESC. Each nudge has:
        - nudge_key: stable id for dismissal
        - kind: nudge type (missing_cover, unfiled_objects, etc.)
        - target_type: 'project' or 'global'
        - target_id, target_slug: project id/slug or None for global nudges
        - title, summary: human-readable text
        - priority: computed score (base_impact * status_weight)
        - base_impact, status_weight: components of priority
        - action: descriptor dict for UI/agent to interpret

    Optional query params:
        - kind: filter by nudge kind (e.g., 'missing_cover')
        - limit: cap the result count (default: all)
    """
    needs = curator_needs.list_needs()

    # Filter by kind if requested
    if kind is not None:
        needs = [n for n in needs if n["kind"] == kind]

    # Limit if requested
    if limit is not None:
        needs = needs[:limit]

    return JSONResponse(needs)


@router.post("/api/curator/needs/dismiss", dependencies=requires(roles.EDITOR))
def api_curator_dismiss_need(nudge_key: str = Form(...), snooze_until: str | None = Form(None)):
    """Dismiss a nudge or need for good (#519: the timed snooze is gone; use Defer).

    nudge_key: the item's key (from the queue or the nudges list). A question
    ("decision:<id>") can't be dismissed: answer it or defer it.

    Returns {ok: true} on success."""
    if snooze_until:
        raise HTTPException(status_code=400, detail="Snooze was replaced by Defer: POST /api/curator/queue/defer")
    # A refusal is a curation_queue.QueueError (#548: an AppError, 400 bad_request).
    return JSONResponse(curation_queue.dismiss(nudge_key))


# --- Curator queue (#519): questions + nudges + needs, grouped by card ---

@router.get("/api/curator/queue")
def api_curator_queue(card: str | None = None, summary: bool = False):
    """The unified Curator queue, grouped by card: {groups, deferred, counts}. `card`
    limits it to one card's slice; `summary=1` returns only the counts (the nav badge).
    (#524) The whole-queue and summary forms come from curation_queue's cache, which is
    rebuilt only after something has been written to the database."""
    if card:
        q = curation_queue.build_queue(card=card)
    else:
        q = curation_queue.cached_queue()
    if summary:
        return JSONResponse({"counts": q["counts"]})
    return JSONResponse(q)


@router.get("/api/curator/queue/html", response_class=HTMLResponse)
def api_curator_queue_html(request: Request, group: str | None = None, card: str | None = None,
                           section: str = "open"):
    """The queue as a server-rendered fragment (autoescaped Jinja). Since #524 it is lazy:
    with no parameters it is just the shell, one collapsed header per group with its item
    count. `group` (a group id: card:<slug>, hobby:<slug>, uploads, collection; or `card`, a
    bare card slug) with `section` (open | deferred) returns that one group's items and, for a
    card, its mini card face from _card.html. The Curator drawer and /curator both use it."""
    if card and not group:
        group = f"card:{card}"
    if not group:
        return templates.TemplateResponse(request, "_curation_queue.html", {"q": curation_queue.cached_queue()})
    section = "deferred" if section == "deferred" else "open"
    g, items = curation_queue.group_items(group, section)
    face = None
    if g is not None and g["type"] == "card":
        p = db.get_project(g["slug"])
        if p is not None:
            face = _to_card_face(p)
    return templates.TemplateResponse(request, "_curation_group.html", {"items": items, "face": face})


def _queue_action(fn, key):
    # A refusal is a curation_queue.QueueError (#548: an AppError, 400 bad_request).
    return JSONResponse(fn(key))


@router.post("/api/curator/queue/defer", dependencies=requires(roles.EDITOR))
def api_curator_queue_defer(key: str = Form(...)):
    """Defer an item (any question, nudge or need): it moves to the Deferred section, with no
    timer, until it is answered or brought back."""
    return _queue_action(curation_queue.defer, key)


@router.post("/api/curator/queue/bring-back", dependencies=requires(roles.EDITOR))
def api_curator_queue_bring_back(key: str = Form(...)):
    """Bring a deferred item back into the main queue."""
    return _queue_action(curation_queue.bring_back, key)
