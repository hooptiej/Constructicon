"""Curator Stage 2: completeness/health scoring engine for projects.

This module provides a single source of truth for scoring project completeness
across multiple dimensions. Stage 3 will reuse these rules for automated nudges;
the scoring model is intentionally declarative and separated from policy.
"""

import json

from core import cards, db, timeline
from core.object_types import get_object_type

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
            {"name": "captions", "description": "≥60% of caption-capable objects (images, video) have a description or auto-caption"},
        ],
    },
    "timeline": {
        "weight": 25,
        "items": [
            {"name": "content_dates", "description": "≥60% of objects have a resolvable date"},
            {"name": "no_large_gap", "description": "no large unexplained gap"},
        ],
    },
    "connections": {
        "weight": 15,
        "items": [
            {"name": "related_projects", "description": "connected to a hobby, family, parent, child or another card"},
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



def _auto_caption(item):
    """#532: the item's moondream auto-caption, if any (type_metadata may be a dict or JSON text)."""
    tm = item.get("type_metadata") or {}
    if isinstance(tm, str):
        try:
            tm = json.loads(tm)
        except ValueError:
            return False
    return bool(((tm or {}).get("auto_caption") or "").strip())

def _normalize_status(status):
    """Map legacy statuses to current ones."""
    return _LEGACY_STATUS_MAP.get(status, status)


def _get_writeup(project):
    """Get the writeup object if writeup_slug is set."""
    if not project.get("writeup_slug"):
        return None
    return db.get_by_slug(project["writeup_slug"])


def content_items(project, items):
    """Project items EXCLUDING the write-up document (#404).

    The write-up is attached to a project (via writeup_slug) and also shows up
    in list_project_items, but it's a story-dimension concern (does a good
    narrative exist) — NOT project content. Counting it in per-object coverage
    (dates, captions, provenance) nags you to "date/caption the write-up", and
    a write-up saved today injects a today-dated point that fakes a fresh
    timeline / masks a stale WIP. So every date/coverage/timeline computation
    runs over this filtered set instead of raw items."""
    wu = project.get("writeup_slug")
    return [i for i in items if i.get("slug") != wu] if wu else items


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

    # #406: require BOTH a relative and an absolute gap, so the normal cadence
    # of a multi-year hobby project (a burst of activity then sparse later
    # additions) doesn't read as a "hole". Only a gap that's both unusually
    # large for this project AND long in absolute terms is worth surfacing.
    threshold = median_gap * 4
    MIN_ABS_GAP = 120 * 24 * 3600  # 120 days
    for gap in gaps:
        if gap > threshold and gap > MIN_ABS_GAP:
            return False  # genuine large gap found

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
    # V2 cards: the live status is `stage` (legacy projects.status is frozen), read
    # through the card_rules adapter so scoring keeps its v1 vocabulary. While a
    # card's status is still a provisional migration guess, it scores by what v1
    # said, so migrating can't change a score before the owner answers.
    effective_status = _normalize_status(cards.curator_status_for(project))

    # Determine if silent
    silent = effective_status in ("shelved", "abandoned", "failed", "reference-only", "idea")

    # Special case: means-to-an-end
    if effective_status == "means-to-an-end":
        return _score_means_to_an_end(project)

    # Get project items and tags
    items = db.list_project_items(project["id"])
    # #404: per-object coverage + timeline checks run over content only, never
    # the write-up doc (which is scored separately as story/writeup_body).
    content = content_items(project, items)
    project_tags = _get_project_tags(project, items)

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
    provenance_diversity_passed = len(set(item.get("provenance") for item in content if item.get("provenance"))) >= 2
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
    cover_passed = db.resolve_project_cover_slug(project) is not None
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

    # Check 2: captions (#532). Only caption-capable items (images, video: the
    # object-type registry's caption_capable) are in the check; a project with
    # none is excused. An item counts if it has a real `description` OR a
    # `content_description` (trimmed, non-empty), OR a moondream auto_caption
    # (type_metadata.auto_caption): owner decision on #532 (2026-10-03), since 556 of
    # 695 captionable items are auto-captioned only and the check should flag projects
    # with genuinely uncaptioned images. display_name never counts: it's often an
    # auto-populated filename.
    caption_threshold = 0.6
    cap_items = [i for i in content if get_object_type(i.get("media_type")).caption_capable]
    if cap_items:
        captions_present = sum(
            1 for item in cap_items
            if (item.get("description") or "").strip() or (item.get("content_description") or "").strip()
            or _auto_caption(item)
        )
        captions_passed = captions_present / len(cap_items) >= caption_threshold
    else:
        captions_passed = False
    excused = not dim_applicable[1] or not cap_items
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
    if content:
        items_with_dates = sum(1 for item in content if timeline.has_real_date(item))
        content_dates_passed = items_with_dates / len(content) >= date_threshold
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
    no_large_gap_passed = _has_large_gap(content)
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
    # Any real connection counts (#534): hobby, family/collection, nesting, or a typed link.
    related_passed = project["id"] in db.connected_project_ids()
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
    Only checks that the project is connected (hobby, family, nesting or a link).
    """
    # Check: any real connection (#534, owner: "relax means-to-an-end"): a hobby, a
    # family/collection, nesting, or a link, the same set as the connections check.
    linked = project["id"] in db.connected_project_ids()

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


def _get_project_tags(project, items=None):
    """Get all tags for a project: project-level tag_id plus tags on its items.
    `items` (db.list_project_items) can be passed in so the caller's list is reused, and the
    item tags come back in one query rather than one per item (#524)."""
    tags = set()

    # Project-level tag_id
    if project.get("tag_id"):
        tag = db.get_tag(project["tag_id"])
        if tag:
            tags.add(tag["id"])

    # Tags on project items
    if items is None:
        items = db.list_project_items(project["id"])
    tags.update(db.tag_ids_for_posts([item["slug"] for item in items]))

    return list(tags)


def _get_related_projects(project):
    """Get related projects: the parent/siblings via parent_id, plus explicit
    project-to-project relations (#408, project_relations table)."""
    related = []
    seen = set()

    # Parent project
    if project.get("parent_id"):
        parent = db.get_project(project["parent_id"])
        if parent and parent["id"] not in seen:
            related.append(parent); seen.add(parent["id"])

    # Sibling/child projects via parent
    if project.get("parent_id"):
        for s in db.list_child_projects(project["parent_id"]):
            if s["id"] != project["id"] and s["id"] not in seen:
                related.append(s); seen.add(s["id"])

    # Explicit peer links (#408)
    for rp in db.list_linked_projects(project["slug"]):  # any link type (V2 3.8)
        if rp["id"] != project["id"] and rp["id"] not in seen:
            related.append(rp); seen.add(rp["id"])

    return related


@db.in_read_session
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
    unfiled_count = db.count_unfiled_items()

    return {
        "projects_by_status": projects_by_status,
        "average_health": round(average_health, 1),
        "gap_buckets": gap_buckets,
        "unfiled_count": unfiled_count,
        "all_project_scores": scores,
    }
