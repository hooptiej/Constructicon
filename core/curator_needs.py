"""Curator Stage 3a: nudge engine for deriving actionable needs from project scores.

Nudges are DERIVED on-demand, never stored. Only dismissals/snoozes are persisted.
This module is the single source of truth for what nudges exist and how to rank them.
"""

import time
from core import curator, db, timeline


# Mapping of (dimension, item) from scoring.py's unmet checks to nudge kinds and base impacts
_UNMET_TO_NUDGE = {
    ("presentation", "cover_image"): ("missing_cover", 10),
    ("story", "writeup_body"): ("missing_writeup", 8),
    ("presentation", "captions"): ("thin_captions", 5),
    ("timeline", "content_dates"): ("missing_dates", 5),
    ("timeline", "no_large_gap"): ("timeline_gap", 4),
    ("connections", "related_projects"): ("weak_connections", 3),
    ("connections", "tags"): ("weak_connections", 3),  # Emit ONE per project even if both unmet
    ("means-to-an-end", "linked"): ("weak_connections", 3),
    ("oddball_bonus", "highlighted"): ("no_highlight", 3),  # only fires on complete/published (wip excuses it)
    # ("story", "provenance_diversity") is EXCLUDED — no nudge for it
}


# Effort rank per nudge kind: lower = quicker/easier to resolve, so it floats
# to the top of the queue (momentum — knock out the one-click wins first).
# Impact (priority) breaks ties within a tier, so within "easy" the most
# important quick win still leads.
_EFFORT_RANK = {
    "confirm_caption": 1,     # accept a suggestion
    "confirm_automatch": 1,   # confirm a match
    "no_highlight": 1,        # one-click flag
    "missing_cover": 2,       # pick a cover image
    "stale_wip": 2,           # one status decision
    "weak_connections": 2,    # add a tag / link
    "missing_dates": 3,       # date items
    "timeline_gap": 3,        # acknowledge / add footage
    "thin_captions": 4,       # caption many items
    "missing_writeup": 5,     # write a narrative
    "unfiled_objects": 5,     # file a pile of objects
}
_DEFAULT_EFFORT = 3


def _status_weight(status):
    """Compute the status_weight used in priority ranking.
    status is the normalized status from curator.score_project()."""
    if status in ("complete", "published"):
        return 3
    elif status == "wip":
        return 1
    elif status == "means-to-an-end":
        return 0.5
    else:
        return 1  # Fallback for unknown statuses


def _nudge_key_for_project(kind, project_id):
    """Generate a stable nudge_key for a per-project nudge."""
    return f"{kind}:project:{project_id}"


def _nudge_key_for_global(kind):
    """Generate a stable nudge_key for a global/aggregate nudge."""
    return f"{kind}:global"


def list_needs():
    """Build the full set of currently-detected nudges, filter by dismissals,
    rank by priority, and return sorted list.

    Returns list[dict] of nudges, sorted by priority DESC (tie-break: target
    recency, then id).
    """
    nudges = []
    dismissed = db.list_active_curator_dismissals()

    # --- Per-project detectors ---
    all_projects = db.list_projects()

    for project in all_projects:
        score = curator.score_project(project)

        # Skip silent projects (they emit nothing)
        if score["silent"]:
            continue

        project_id = project["id"]
        project_status = score["status"]
        weight = _status_weight(project_status)

        # Emit nudges from unmet checks
        # Track which kinds we've emitted for this project to avoid duplicates
        # (e.g., both "related_projects" and "tags" map to "weak_connections")
        emitted_kinds = set()

        for unmet in score["unmet"]:
            dimension = unmet["dimension"]
            item = unmet["item"]

            # Skip provenance_diversity entirely
            if dimension == "story" and item == "provenance_diversity":
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
                "summary": f"{kind}: {project['title']}",
                "priority": priority,
                "base_impact": base_impact,
                "status_weight": weight,
                "action": action,
            })

        # Stale WIP detector
        if project_status == "wip":
            # #404: exclude the write-up doc — a today-saved write-up must not
            # make a stale WIP look freshly worked-on.
            items = curator.content_items(project, db.list_project_items(project_id))
            if items:  # Only check if there are items
                most_recent_date = max(
                    timeline.resolve_item_date(item) for item in items
                )
                days_since = (time.time() - most_recent_date) / (24 * 3600)
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
                            "title": f"{project['title']} is stale (WIP)",
                            "summary": f"No activity in {int(days_since)} days",
                            "priority": priority,
                            "base_impact": base_impact,
                            "status_weight": weight,
                            "action": {
                                "type": "set_status",
                                "project_slug": project["slug"],
                            },
                        })

    # --- Global/aggregate detectors ---
    global_weight = 1  # Global nudges always use weight 1

    # Unfiled objects detector
    unfiled = db.list_unfiled_items(limit=100000)
    if len(unfiled) > 0:
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
                "summary": f"{len(unfiled)} objects not in any project",
                "priority": priority,
                "base_impact": base_impact,
                "status_weight": global_weight,
                "action": {"type": "file_unfiled"},
            })

    # Confirm automatch detector
    pending_matches = db.list_pending_decisions(kind="project_match")
    unresolved_count = len(pending_matches)  # Already filtered for unresolved by db.list_pending_decisions
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
    # possible_correlation: STUB — do not implement (Stage 4)

    # --- Sort and return ---
    # Sort by priority DESC, tie-break by target recency (created_at of project/item),
    # then by id if still tied
    def sort_key(nudge):
        # Easy-first: lower effort floats to the top (quick wins first).
        effort = _EFFORT_RANK.get(nudge["kind"], _DEFAULT_EFFORT)
        # Within an effort tier, higher impact leads (priority DESC).
        priority = -nudge["priority"]

        # Target recency (newest first = smallest epoch last, so negate)
        # For global nudges, use current time (most recent)
        if nudge["target_type"] == "global":
            recency = -time.time()
        else:
            # Find the most recent project item date
            project = db.get_project(nudge["target_id"])
            items = curator.content_items(project, db.list_project_items(nudge["target_id"]))
            if items:
                recency = -max(timeline.resolve_item_date(item) for item in items)
            else:
                recency = -project["created_at"]

        # Nudge key as final tiebreaker (lexicographic)
        return (effort, priority, recency, nudge["nudge_key"])

    nudges.sort(key=sort_key)
    return nudges


def count_active_needs():
    """Return the count of active needs (filtered by dismissals)."""
    return len(list_needs())


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
