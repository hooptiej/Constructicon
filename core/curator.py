"""Curator Stage 2: completeness/health scoring engine for projects.

This module provides a single source of truth for scoring project completeness
across multiple dimensions. Stage 3 will reuse these rules for automated nudges;
the scoring model is intentionally declarative and separated from policy.
"""

from core import db, timeline

# Status mapping: legacy active/archived -> wip/complete
_LEGACY_STATUS_MAP = {
    "active": "wip",
    "archived": "complete",
}

# Weights as fractions of 100
SCORING_RULES = {
    "story": {
        "weight": 25,
        "items": [
            {"name": "writeup_body", "description": "write-up body present & non-empty"},
            {"name": "provenance_diversity", "description": "≥2 distinct provenance values"},
        ],
    },
    "presentation": {
        "weight": 25,
        "items": [
            {"name": "cover_image", "description": "cover image set (cover_slug non-null)"},
            {"name": "captions", "description": "≥60% of objects have a non-empty caption"},
        ],
    },
    "timeline": {
        "weight": 25,
        "items": [
            {"name": "content_dates", "description": "≥60% of objects have a content date"},
            {"name": "no_large_gap", "description": "no large unexplained gap"},
        ],
    },
    "connections": {
        "weight": 15,
        "items": [
            {"name": "related_projects", "description": "≥1 related-project link"},
            {"name": "tags", "description": "has ≥1 tag (project or item level)"},
        ],
    },
    "oddball_bonus": {
        "weight": 10,
        "items": [
            {"name": "highlighted", "description": "≥1 object with highlight = 1"},
        ],
    },
}

# Status-based check applicability (True = applicable, False = excused)
STATUS_APPLICABILITY = {
    "complete": {"story": [True, True], "presentation": [True, True], "timeline": [True, True], "connections": [True, True], "oddball_bonus": [True]},
    "published": {"story": [True, True], "presentation": [True, True], "timeline": [True, True], "connections": [True, True], "oddball_bonus": [True]},
    "wip": {"story": [True, True], "presentation": [True, True], "timeline": [True, False], "connections": [True, True], "oddball_bonus": [False]},
    "shelved": None,  # Silent
    "abandoned": None,  # Silent
    "failed": None,  # Silent
    "reference-only": None,  # Silent
    "idea": None,  # Silent
    "means-to-an-end": "parent_or_related_only",  # Only check parent_id or related links
}


def _normalize_status(status):
    """Map legacy statuses to current ones."""
    return _LEGACY_STATUS_MAP.get(status, status)


def _get_writeup(project):
    """Get the writeup object if writeup_slug is set."""
    if not project.get("writeup_slug"):
        return None
    return db.get_by_slug(project["writeup_slug"])


def _has_large_gap(items):
    """Check if a project has an unexplained large gap in its timeline.

    Returns True if no large gap is found (check passes), False if a gap exists.

    A "large gap" is defined heuristically: sort items by effective date,
    look at gaps between consecutive items. If any gap is significantly larger
    than the median gap (e.g., 3x the median), it's considered "large".
    Edge cases: <= 2 items pass trivially (need 3+ items for a meaningful pattern).
    """
    if len(items) <= 2:
        return True  # No gap to speak of, check passes

    # Compute effective dates using timeline resolution
    dates = sorted([timeline.resolve_item_date(item) for item in items])

    # Compute gaps between consecutive dates
    gaps = []
    for i in range(1, len(dates)):
        gap = dates[i] - dates[i-1]
        if gap > 0:  # Only count positive gaps
            gaps.append(gap)

    if len(gaps) < 2:
        return True  # Not enough gaps to identify a pattern

    # Heuristic: find median gap, flag if any gap > 3x median
    sorted_gaps = sorted(gaps)
    median_gap = sorted_gaps[len(sorted_gaps) // 2]

    if median_gap == 0:
        return True  # All items on same date, no gap pattern

    threshold = median_gap * 3
    for gap in gaps:
        if gap > threshold:
            return False  # Large gap found

    return True  # No large gap, check passes


def score_project(project_id_or_dict):
    """Score one project and return a structured result.

    Args:
        project_id_or_dict: numeric project id or a project dict from db.get_project.

    Returns a dict with keys:
        - project_id, slug, title, status (effective after legacy mapping)
        - silent: bool, True if this status should be greyed out / not nudged
        - score: int (0-100, rounded)
        - band: "green"|"yellow"|"red" (>=80, 50-79, <50)
        - earned: float, applicable: float (precise, before rounding)
        - checklist: list of {"dimension", "item", "passed", "excused", "weight"}
        - unmet: list of {"dimension", "item"} for applicable-and-failed checks
    """
    if isinstance(project_id_or_dict, dict):
        project = project_id_or_dict
    else:
        project = db.get_project(project_id_or_dict)

    if project is None:
        return None

    # Normalize status
    effective_status = _normalize_status(project.get("status", "wip"))

    # Determine if silent
    silent = effective_status in ("shelved", "abandoned", "failed", "reference-only", "idea")

    # Special case: means-to-an-end
    if effective_status == "means-to-an-end":
        return _score_means_to_an_end(project)

    # Get project items and tags
    items = db.list_project_items(project["id"])
    project_tags = _get_project_tags(project)

    # Build checklist
    checklist = []
    unmet = []
    earned = 0.0
    applicable = 0.0

    # Get applicability for this status
    applicability = STATUS_APPLICABILITY.get(effective_status, STATUS_APPLICABILITY["wip"])
    if applicability is None:
        applicability = {
            "story": [False, False],
            "presentation": [False, False],
            "timeline": [False, False],
            "connections": [False, False],
            "oddball_bonus": [False],
        }

    # Story checks
    dimension = "story"
    dim_rules = SCORING_RULES[dimension]
    dim_applicable = applicability.get(dimension, [False, False])
    weight_per_item = dim_rules["weight"] / len(dim_rules["items"])

    # Check 1: writeup_body
    # The write-up narrative lives in the writeup document's type_metadata.body
    # (the one place site_export.py and app.py both read it from), NOT in
    # content_description/description. Projects auto-get an empty writeup doc on
    # creation (#156), so presence of writeup_slug is not enough — check the body.
    writeup = _get_writeup(project)
    _writeup_body = ""
    if writeup is not None:
        _writeup_body = (writeup.get("type_metadata") or {}).get("body", "") or ""
    writeup_body_passed = bool(_writeup_body.strip())
    excused = not dim_applicable[0]
    if not excused:
        applicable += weight_per_item
        if writeup_body_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "writeup_body"})
    checklist.append({
        "dimension": dimension,
        "item": "writeup_body",
        "passed": writeup_body_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Check 2: provenance_diversity
    provenance_diversity_passed = len(set(item.get("provenance") for item in items if item.get("provenance"))) >= 2
    excused = not dim_applicable[1]
    if not excused:
        applicable += weight_per_item
        if provenance_diversity_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "provenance_diversity"})
    checklist.append({
        "dimension": dimension,
        "item": "provenance_diversity",
        "passed": provenance_diversity_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Presentation checks
    dimension = "presentation"
    dim_rules = SCORING_RULES[dimension]
    dim_applicable = applicability.get(dimension, [False, False])
    weight_per_item = dim_rules["weight"] / len(dim_rules["items"])

    # Check 1: cover_image
    cover_passed = project.get("cover_slug") is not None
    excused = not dim_applicable[0]
    if not excused:
        applicable += weight_per_item
        if cover_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "cover_image"})
    checklist.append({
        "dimension": dimension,
        "item": "cover_image",
        "passed": cover_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Check 2: captions — the content's OWN description (content_description).
    # NOT display_name: that's an optional filename/title override that's
    # frequently auto-populated, so counting it would make nearly every object
    # look "captioned" and render this check meaningless.
    caption_threshold = 0.6
    if items:
        captions_present = sum(1 for item in items if (item.get("content_description") or "").strip())
        captions_passed = captions_present / len(items) >= caption_threshold
    else:
        captions_passed = False
    excused = not dim_applicable[1]
    if not excused:
        applicable += weight_per_item
        if captions_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "captions"})
    checklist.append({
        "dimension": dimension,
        "item": "captions",
        "passed": captions_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Timeline checks
    dimension = "timeline"
    dim_rules = SCORING_RULES[dimension]
    dim_applicable = applicability.get(dimension, [False, False])
    weight_per_item = dim_rules["weight"] / len(dim_rules["items"])

    # Check 1: content_dates
    date_threshold = 0.6
    if items:
        items_with_dates = sum(1 for item in items if item.get("content_date") is not None)
        content_dates_passed = items_with_dates / len(items) >= date_threshold
    else:
        content_dates_passed = False
    excused = not dim_applicable[0]
    if not excused:
        applicable += weight_per_item
        if content_dates_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "content_dates"})
    checklist.append({
        "dimension": dimension,
        "item": "content_dates",
        "passed": content_dates_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Check 2: no_large_gap
    no_large_gap_passed = _has_large_gap(items)
    excused = not dim_applicable[1]
    if not excused:
        applicable += weight_per_item
        if no_large_gap_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "no_large_gap"})
    checklist.append({
        "dimension": dimension,
        "item": "no_large_gap",
        "passed": no_large_gap_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Connections checks
    dimension = "connections"
    dim_rules = SCORING_RULES[dimension]
    dim_applicable = applicability.get(dimension, [False, False])
    weight_per_item = dim_rules["weight"] / len(dim_rules["items"])

    # Check 1: related_projects
    related_projects = _get_related_projects(project)
    related_passed = len(related_projects) >= 1
    excused = not dim_applicable[0]
    if not excused:
        applicable += weight_per_item
        if related_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "related_projects"})
    checklist.append({
        "dimension": dimension,
        "item": "related_projects",
        "passed": related_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Check 2: tags
    tags_passed = len(project_tags) >= 1
    excused = not dim_applicable[1]
    if not excused:
        applicable += weight_per_item
        if tags_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "tags"})
    checklist.append({
        "dimension": dimension,
        "item": "tags",
        "passed": tags_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Oddball bonus
    dimension = "oddball_bonus"
    dim_rules = SCORING_RULES[dimension]
    dim_applicable = applicability.get(dimension, [False])
    weight_per_item = dim_rules["weight"] / len(dim_rules["items"])

    # Check: highlighted
    highlighted_passed = any(item.get("highlight") == 1 for item in items)
    excused = not dim_applicable[0]
    if not excused:
        applicable += weight_per_item
        if highlighted_passed:
            earned += weight_per_item
        else:
            unmet.append({"dimension": dimension, "item": "highlighted"})
    checklist.append({
        "dimension": dimension,
        "item": "highlighted",
        "passed": highlighted_passed,
        "excused": excused,
        "weight": weight_per_item,
    })

    # Compute score
    if applicable == 0:
        # Fully excused / silent project
        score = 100
    else:
        score = int(round(100 * earned / applicable))

    # Determine band
    if score >= 80:
        band = "green"
    elif score >= 50:
        band = "yellow"
    else:
        band = "red"

    return {
        "project_id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "status": effective_status,
        "silent": silent,
        "score": score,
        "band": band,
        "earned": earned,
        "applicable": applicable,
        "checklist": checklist,
        "unmet": unmet,
    }


def _score_means_to_an_end(project):
    """Special scoring for means-to-an-end status.
    Only checks if project is linked to a parent or has related projects.
    """
    # Check: parent_id non-null OR ≥1 related-project link
    has_parent = project.get("parent_id") is not None
    related = _get_related_projects(project)
    has_related = len(related) >= 1

    linked = has_parent or has_related

    checklist = [
        {
            "dimension": "means-to-an-end",
            "item": "linked",
            "passed": linked,
            "excused": False,
            "weight": 100.0,
        }
    ]

    return {
        "project_id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "status": "means-to-an-end",
        "silent": False,
        "score": 100 if linked else 0,
        "band": "green" if linked else "red",
        "earned": 100.0 if linked else 0.0,
        "applicable": 100.0,
        "checklist": checklist,
        "unmet": [] if linked else [{"dimension": "means-to-an-end", "item": "linked"}],
    }


def _get_project_tags(project):
    """Get all tags for a project: project-level tag_id plus tags on its items."""
    tags = set()

    # Project-level tag_id
    if project.get("tag_id"):
        tag = db.get_tag(project["tag_id"])
        if tag:
            tags.add(tag["id"])

    # Tags on project items
    items = db.list_project_items(project["id"])
    for item in items:
        item_tags = db.list_tags_for_post(item["slug"])
        for tag in item_tags:
            tags.add(tag["id"])

    return list(tags)


def _get_related_projects(project):
    """Get related projects: parent or any manual project-to-project links.

    Since the schema doesn't have project_relations, we check parent_id only.
    Future enhancement: if project relations are added, include them here.
    """
    related = []

    # Parent project
    if project.get("parent_id"):
        parent = db.get_project(project["parent_id"])
        if parent:
            related.append(parent)

    # Sibling/child projects via parent
    if project.get("parent_id"):
        siblings = db.list_child_projects(project["parent_id"])
        related.extend([s for s in siblings if s["id"] != project["id"]])

    return related


def score_all_projects():
    """Score every project and return aggregate dashboard data.

    Returns a dict with:
        - projects_by_status: count of projects per effective status
        - average_health: average score of LIVE projects (wip+complete+published, excluding silent)
        - gap_buckets: counts of projects failing each specific check
        - unfiled_count: count of capture_events not in any project
    """
    all_projects = db.list_projects()
    scores = [score_project(p) for p in all_projects]

    # Projects by status
    projects_by_status = {}
    for score in scores:
        status = score["status"]
        projects_by_status[status] = projects_by_status.get(status, 0) + 1

    # Average health (only live, non-silent projects)
    live_scores = [s for s in scores if s["status"] in ("wip", "complete", "published")]
    average_health = sum(s["score"] for s in live_scores) / len(live_scores) if live_scores else 0

    # Gap buckets: count per failed check
    gap_buckets = {}
    for score in scores:
        for unmet_check in score["unmet"]:
            key = f"{unmet_check['dimension']}:{unmet_check['item']}"
            gap_buckets[key] = gap_buckets.get(key, 0) + 1

    # Unfiled count
    unfiled = db.list_unfiled_items(limit=1000000)
    unfiled_count = len(unfiled)

    return {
        "projects_by_status": projects_by_status,
        "average_health": round(average_health, 1),
        "gap_buckets": gap_buckets,
        "unfiled_count": unfiled_count,
        "all_project_scores": scores,
    }
