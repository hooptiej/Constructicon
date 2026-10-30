"""Curator Stage 3a: nudge engine for deriving actionable needs from project scores.

Nudges are DERIVED on-demand, never stored. Only dismissals/deferrals are persisted (#519:
Defer replaced the timed snooze).
This module is the single source of truth for what nudges exist and how to rank them.
"""

import time
from core import curator, db, policy, timeline


# Mapping of (dimension, item) from scoring.py's unmet checks to nudge kinds and base impacts
_UNMET_TO_NUDGE = {
    ("presentation", "cover_image"): ("missing_cover", 10),
    ("story", "writeup_body"): ("missing_writeup", 8),
    ("presentation", "captions"): ("thin_captions", 5),
    ("timeline", "content_dates"): ("missing_dates", 5),
    ("timeline", "no_large_gap"): ("timeline_gap", 4),
    ("connections", "related_projects"): ("weak_connections", 3),
    ("connections", "tags"): ("weak_connections", 3),  # Emit ONE per project even if both unmet
    ("oddball_bonus", "highlighted"): ("no_highlight", 3),  # only fires on done / in use (in progress excuses it)
    # ("story", "provenance_diversity") is not in this table: it is the #354 nudge
    # `no_provenance_diversity`, built by _provenance_diversity_nudge (it only fires once the card
    # has SOME file provenance, so a never-classified card isn't nagged about diversity).
}

# #354: the provenance-diversity nudge turns on once file provenance exists (the gross rules in
# core/provenance_rules.py are what start filling it in). A card is nudged when it has at least
# DIVERSITY_MIN_FILES content files, at least one of them has a provenance, and fewer than two
# distinct values are used (the scoring check `provenance_diversity`).
DIVERSITY_MIN_FILES = 2


# Effort rank per nudge kind: lower = quicker/easier to resolve, so it floats
# to the top of the queue (momentum — knock out the one-click wins first).
# Impact (priority) breaks ties within a tier, so within "easy" the most
# important quick win still leads.
_EFFORT_RANK = {
    "confirm_caption": 1,     # accept a suggestion
    "confirm_automatch": 1,   # confirm a match
    "no_highlight": 1,        # one-click flag
    "missing_cover": 2,       # pick a cover image
    "stale_wip": 2,           # one stage decision
    "weak_connections": 2,    # add a tag / link
    "missing_dates": 3,       # date items
    "no_provenance_diversity": 3,  # #354: mark a few files' origin
    "timeline_gap": 3,        # acknowledge / add footage
    "thin_captions": 4,       # caption many items
    "missing_writeup": 5,     # write a narrative
    "unfiled_objects": 5,     # file a pile of objects
}
_DEFAULT_EFFORT = 3


def _stage_weight(stage):
    """The weight a card's stage gives its nudges in priority ranking (the nudge's
    `status_weight` field keeps its name). `stage` is curator.score_project()'s: a finished
    card (done, in use) is about to be shown off, so its gaps rank higher (#545: keyed on the
    stage, no longer the v1 status word)."""
    return 3 if stage in ("done", "in_use") else 1


def _nudge_key_for_project(kind, project_id):
    """Generate a stable nudge_key for a per-project nudge."""
    return f"{kind}:project:{project_id}"


def _nudge_key_for_global(kind):
    """Generate a stable nudge_key for a global/aggregate nudge."""
    return f"{kind}:global"


def project_nudges(project, dismissed=None):
    """The derived nudges for ONE project (the per-project half of list_needs), unsorted.
    `dismissed` is db.list_active_curator_dismissals() (fetched if not given); dismissed
    nudges are left out, deferred ones are kept."""
    if dismissed is None:
        dismissed = db.list_active_curator_dismissals()
    nudges = []
    score = curator.score_project(project)

    # Skip silent projects (they emit nothing)
    if score["silent"]:
        return nudges

    project_id = project["id"]
    stage = score["stage"]
    weight = _stage_weight(stage)
    # #545: the shared checks carry the same sentence the pips show ("Write-up has 120
    # characters. Needs 200."); it becomes the nudge's summary, so both say the same thing.
    reasons = {(c["dimension"], c["item"]): c["reason"] for c in score["checklist"] if c.get("reason")}

    # Emit nudges from unmet checks
    # Track which kinds we've emitted for this project to avoid duplicates
    # (e.g., both "related_projects" and "tags" map to "weak_connections")
    emitted_kinds = set()

    for unmet in score["unmet"]:
        dimension = unmet["dimension"]
        item = unmet["item"]

        # provenance_diversity: its own nudge (#354), only once the card has some file provenance
        if dimension == "story" and item == "provenance_diversity":
            n = _provenance_diversity_nudge(project, weight, dismissed)
            if n is not None:
                nudges.append(n)
            continue

        key = (dimension, item)
        if key not in _UNMET_TO_NUDGE:
            # Unknown check; skip silently (shouldn't happen)
            continue

        kind, base_impact = _UNMET_TO_NUDGE[key]

        # Emit ONE nudge per project per kind (avoid duplicates)
        if kind in emitted_kinds:
            continue
        emitted_kinds.add(kind)

        nudge_key = _nudge_key_for_project(kind, project_id)
        if nudge_key in dismissed:
            continue  # Suppressed by dismissal

        # Compute action descriptor
        action = _action_for_kind(kind, project)

        priority = base_impact * weight

        nudges.append({
            "nudge_key": nudge_key,
            "kind": kind,
            "target_type": "project",
            "target_id": project_id,
            "target_slug": project["slug"],
            "title": _title_for_nudge(kind, project),
            "summary": reasons.get(key) or f"{kind}: {project['title']}",
            "priority": priority,
            "base_impact": base_impact,
            "status_weight": weight,
            "action": action,
        })

    # Stale detector: an in-progress card nothing has happened on in 90 days (kind stale_wip)
    if stage == "in_progress":
        # #404: exclude the write-up doc — a today-saved write-up must not
        # make a stale WIP look freshly worked-on.
        items = curator.content_items(project, db.list_project_items(project_id))
        if items:  # Only check if there are items
            real = timeline.real_dates(items)  # #725: an undated item says nothing about recency
            most_recent_date = max(real) if real else None
            days_since = (time.time() - most_recent_date) / (24 * 3600) if real else 0
            if days_since > 90:
                kind = "stale_wip"
                nudge_key = _nudge_key_for_project(kind, project_id)
                if nudge_key not in dismissed:
                    base_impact = 6
                    priority = base_impact * weight
                    nudges.append({
                        "nudge_key": nudge_key,
                        "kind": kind,
                        "target_type": "project",
                        "target_id": project_id,
                        "target_slug": project["slug"],
                        "title": f"{project['title']} is stale (in progress)",
                        "summary": f"No activity in {int(days_since)} days",
                        "priority": priority,
                        "base_impact": base_impact,
                        "status_weight": weight,
                        "action": {
                            "type": "set_status",
                            "project_slug": project["slug"],
                        },
                    })
    return nudges


def _provenance_diversity_nudge(project, weight, dismissed):
    """#354: "all of this card's files with a provenance say the same thing" (or most have none), as a
    nudge, or None. Fires only when the card has >= DIVERSITY_MIN_FILES content files, at least one has
    a file provenance, and fewer than two distinct values are in use; a family is never nagged
    (card_rules.provenance_nag_applies). The summary names the one value used and how many files have
    none: the gap to fill."""
    from core import card_rules, provenance_options  # lazy: keep this module's import light
    if not card_rules.provenance_nag_applies(project.get("kind")):
        return None
    content = curator.content_items(project, db.list_project_items(project["id"]))
    if len(content) < DIVERSITY_MIN_FILES:
        return None
    values = [i.get("provenance") for i in content if i.get("provenance")]
    distinct = sorted(set(values))
    if not values or len(distinct) >= 2:
        return None
    nudge_key = _nudge_key_for_project("no_provenance_diversity", project["id"])
    if nudge_key in dismissed:
        return None
    label = provenance_options.label("file", distinct[0])
    missing = len(content) - len(values)
    summary = (f"{len(values)} of {len(content)} files say where they came from, all “{label}”"
               + (f"; {missing} say nothing yet" if missing else "")
               + ". Mark the others (made by you, a result, a design, found ...) so the story shows more than one side.")
    base_impact = 2
    return {
        "nudge_key": nudge_key,
        "kind": "no_provenance_diversity",
        "target_type": "project",
        "target_id": project["id"],
        "target_slug": project["slug"],
        "title": f"{project['title']}: every file has the same origin",
        "summary": summary,
        "priority": base_impact * weight,
        "base_impact": base_impact,
        "status_weight": weight,
        "action": {"type": "set_file_provenance", "project_slug": project["slug"]},
    }


def _sort_key(nudge, recency_cache=None):
    # Easy-first: lower effort floats to the top (quick wins first).
    effort = _EFFORT_RANK.get(nudge["kind"], _DEFAULT_EFFORT)
    # Within an effort tier, higher impact leads (priority DESC).
    priority = -nudge["priority"]

    # Target recency (newest first = smallest epoch last, so negate)
    # For global nudges, use current time (most recent)
    if nudge["target_type"] == "global":
        recency = -time.time()
    elif recency_cache is not None and nudge["target_id"] in recency_cache:
        recency = recency_cache[nudge["target_id"]]
    else:
        # Find the most recent project item date
        project = db.get_project(nudge["target_id"])
        items = curator.content_items(project, db.list_project_items(nudge["target_id"]))
        real = timeline.real_dates(items)  # #725: real dates only
        if real:
            recency = -max(real)
        else:
            recency = -project["created_at"]
        if recency_cache is not None:
            recency_cache[nudge["target_id"]] = recency  # one project has up to ~6 nudges (#524)

    # Nudge key as final tiebreaker (lexicographic)
    return (effort, priority, recency, nudge["nudge_key"])


def sort_nudges(nudges):
    """Rank nudges the way list_needs does: easy wins first, then impact, then recency (#519: the
    per-card slice on the project page uses this too, so it matches the queue)."""
    cache = {}
    return sorted(nudges, key=lambda n: _sort_key(n, cache))


@db.in_read_session
def list_needs():
    """Build the full set of currently-detected nudges, filter by dismissals,
    rank by priority, and return sorted list.

    Returns list[dict] of nudges, sorted by priority DESC (tie-break: target
    recency, then id).
    """
    nudges = []
    dismissed = db.list_active_curator_dismissals()

    # --- Per-project detectors ---
    # #604 step 3: not the private cards the actor isn't on (the cached queue runs as system: all).
    all_projects = policy.filter_visible_cards(db.list_projects())

    for project in all_projects:
        nudges.extend(project_nudges(project, dismissed))

    # --- Global/aggregate detectors ---
    global_weight = 1  # Global nudges always use weight 1

    # Unfiled objects detector
    unfiled_count = db.count_unfiled_items()  # (#524: a count, not 1,700 row dicts)
    if unfiled_count > 0:
        kind = "unfiled_objects"
        nudge_key = _nudge_key_for_global(kind)
        if nudge_key not in dismissed:
            base_impact = 5
            priority = base_impact * global_weight
            nudges.append({
                "nudge_key": nudge_key,
                "kind": kind,
                "target_type": "global",
                "target_id": None,
                "target_slug": None,
                "title": "Unfiled objects",
                "summary": f"{unfiled_count} objects not in any project",
                "priority": priority,
                "base_impact": base_impact,
                "status_weight": global_weight,
                "action": {"type": "file_unfiled"},
            })

    # Confirm automatch detector
    # #551 item 3: the answerable ones only (decisions.list_open leaves out stale-but-unswept
    # questions, as the old resolve-on-read did). Lazy: decisions imports half of core.
    from core import decisions
    unresolved_count = sum(1 for d in db.list_pending_decisions(kind="project_match") if not decisions.stale_reason(d))
    if unresolved_count > 0:
        kind = "confirm_automatch"
        nudge_key = _nudge_key_for_global(kind)
        if nudge_key not in dismissed:
            base_impact = 7
            priority = base_impact * global_weight
            nudges.append({
                "nudge_key": nudge_key,
                "kind": kind,
                "target_type": "global",
                "target_id": None,
                "target_slug": None,
                "title": "Confirm auto-matched projects",
                "summary": f"{unresolved_count} uploads need project assignment",
                "priority": priority,
                "base_impact": base_impact,
                "status_weight": global_weight,
                "action": {"type": "review_automatch"},
            })

    # Confirm caption detector
    # Count capture_events with auto_caption that haven't been accepted into content_description
    auto_caption_count = _count_unaccepted_captions()
    if auto_caption_count > 0:
        kind = "confirm_caption"
        nudge_key = _nudge_key_for_global(kind)
        if nudge_key not in dismissed:
            base_impact = 7
            priority = base_impact * global_weight
            nudges.append({
                "nudge_key": nudge_key,
                "kind": kind,
                "target_type": "global",
                "target_id": None,
                "target_slug": None,
                "title": "Review auto-generated captions",
                "summary": f"{auto_caption_count} objects with suggested captions",
                "priority": priority,
                "base_impact": base_impact,
                "status_weight": global_weight,
                "action": {"type": "review_captions"},
            })

    # --- Stage 4 stubs ---
    # Project correlations (Stage 4, #344) are a Curator NEED, not a nudge: core/correlations.py.

    # --- Sort and return ---
    nudges = sort_nudges(nudges)
    states = db.list_curator_states()
    for n in nudges:
        n["deferred"] = states.get(n["nudge_key"]) == db.CURATOR_DEFER
    return nudges


def _count_unaccepted_captions():
    """Count capture_events rows with auto_caption in type_metadata that do NOT
    have a content_description (i.e., the caption was never accepted).

    Auto-captions are suggestions stored in type_metadata.auto_caption.
    When accepted, they're manually moved to content_description.
    This counts rows where the suggestion exists but hasn't been accepted yet."""
    conn = db.get_conn()
    try:
        # Query: type_metadata JSON contains 'auto_caption' key (non-empty string)
        # AND content_description is NULL or empty
        # Use JSON_EXTRACT to safely check the JSON column
        rows = conn.execute("""
            SELECT COUNT(*) AS count FROM capture_events
            WHERE
                json_extract(type_metadata, '$.auto_caption') IS NOT NULL
                AND json_extract(type_metadata, '$.auto_caption') != ''
                AND (content_description IS NULL OR content_description = '')
                AND json_extract(type_metadata, '$.auto_caption_dismissed') IS NULL
        """).fetchone()
        return rows["count"] if rows else 0
    finally:
        conn.close()


def _action_for_kind(kind, project):
    """Generate an action descriptor dict for a nudge kind."""
    if kind == "missing_cover":
        return {"type": "set_cover", "project_slug": project["slug"]}
    elif kind == "missing_writeup":
        return {"type": "draft_writeup", "project_slug": project["slug"]}
    elif kind == "thin_captions":
        return {"type": "add_captions", "project_slug": project["slug"]}
    elif kind == "missing_dates":
        return {"type": "add_dates", "project_slug": project["slug"]}
    elif kind == "timeline_gap":
        return {"type": "acknowledge", "project_slug": project["slug"]}
    elif kind == "weak_connections":
        return {"type": "add_related_or_tags", "project_slug": project["slug"]}
    elif kind == "no_highlight":
        return {"type": "add_highlight", "project_slug": project["slug"]}
    elif kind == "stale_wip":
        return {"type": "set_status", "project_slug": project["slug"]}
    else:
        # Fallback for unknown kinds
        return {"type": "acknowledge", "project_slug": project["slug"]}


def _title_for_nudge(kind, project):
    """Generate a human-friendly title for a nudge."""
    titles = {
        "missing_cover": f"{project['title']} needs a cover image",
        "missing_writeup": f"{project['title']} needs a write-up",
        "thin_captions": f"{project['title']} has thin captions",
        "missing_dates": f"{project['title']} is missing content dates",
        "timeline_gap": f"{project['title']} has a timeline gap",
        "weak_connections": f"{project['title']} needs more connections",
        "no_highlight": f"{project['title']} has no highlights",
    }
    return titles.get(kind, f"{project['title']}: {kind}")
