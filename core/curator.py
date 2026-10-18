"""Curator Stage 2: completeness/health scoring engine for cards.

Scores a card's completeness across several dimensions; curator_needs.py turns the
unmet checks into nudges. The model is declarative and separated from policy.

One report card (#545): the checks this score shares with the card pips (cover,
write-up, dates) are NOT defined here. They call core/card_level.py's check_cover /
check_writeup / check_dates, so the two can never grade the same card differently
(write-up >= 200 characters, >= 80% of files with a real date). And the card's
STAGE is the only status it reads: the frozen v1 `projects.status` word is not an
input any more (STAGE_APPLICABILITY below is keyed on card_rules.STAGES).
"""

import json
import logging

from core import besteffort, card_level, card_rules, db, timeline
from core.object_types import get_object_type

log = logging.getLogger("constructicon.curator")

# Weights as fractions of 100
SCORING_RULES = {
    "story": {
        "weight": 25,
        "items": [
            {"name": "writeup_body", "description": f"write-up has >= {card_level.WRITEUP_MIN_CHARS} characters"},
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
            {"name": "content_dates",
             "description": f">= {int(card_level.DATES_SHARE * 100)}% of files have a real date, or start and end dates are set"},
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

# Which checks apply at each stage (True = applicable, False = excused). None = the stage is
# silent: not scored, never nudged. Keyed on the live stage (card_rules.STAGES), #545.
_ALL_CHECKS = {"story": [True, True], "presentation": [True, True], "timeline": [True, True],
               "connections": [True, True], "oddball_bonus": [True]}
STAGE_APPLICABILITY = {
    "done": _ALL_CHECKS,
    "in_use": _ALL_CHECKS,
    # Still being worked on: a gap in the timeline is expected and no highlight is due yet.
    "in_progress": {"story": [True, True], "presentation": [True, True], "timeline": [True, False],
                    "connections": [True, True], "oddball_bonus": [False]},
    "paused": None,
    "idea": None,
    "stopped": None,
}
assert set(STAGE_APPLICABILITY) == set(card_rules.STAGES), "every stage needs a Curator rule"
LIVE_STAGES = ("in_progress", "done", "in_use")  # the stages the dashboard's average health counts


def scoring_stage(card):
    """The stage the Curator scores a card by: its live stage. A row with no stage yet
    (pre-migration data, migrated at the next boot) scores as in progress."""
    return card.get("stage") or "in_progress"


def is_silent(card):
    """True when the Curator doesn't score or nudge this card: a silent stage (paused, idea,
    stopped), or a reference collection (kind collection with no provenance of its own, or
    'referenced': what v1 called reference-only)."""
    if card.get("kind") == "collection" and card.get("provenance") in (None, "referenced"):
        return True
    return STAGE_APPLICABILITY.get(scoring_stage(card)) is None


def _auto_caption(item):
    """#532: the item's moondream auto-caption, if any (type_metadata may be a dict or JSON text)."""
    tm = item.get("type_metadata") or {}
    if isinstance(tm, str):
        try:
            tm = json.loads(tm)
        except ValueError as e:
            besteffort.warn(log, "curator: unreadable type_metadata JSON (treated as no caption)", e,
                            slug=item.get("slug"))
            return False
    return bool(((tm or {}).get("auto_caption") or "").strip())

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
    dates = sorted(timeline.real_dates(items))  # #725: undated items have no place on the line

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
        - project_id, slug, title, stage + stage_label (the live stage it was scored by)
        - silent: bool, True if this card is greyed out / not nudged (see is_silent)
        - score: int (0-100, rounded)
        - band: "green"|"yellow"|"red" (>=80, 50-79, <50)
        - earned: float, applicable: float (precise, before rounding)
        - checklist: list of {"dimension", "item", "passed", "excused", "weight"}; the checks
          shared with the pips (cover_image, writeup_body, content_dates) also carry `reason`,
          the same sentence the pips show
        - unmet: list of {"dimension", "item"} for applicable-and-failed checks
    """
    if isinstance(project_id_or_dict, dict):
        project = project_id_or_dict
    else:
        project = db.get_project(project_id_or_dict)

    if project is None:
        return None

    stage = scoring_stage(project)
    silent = is_silent(project)

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

    # Which checks apply at this stage (a silent card: none)
    applicability = None if silent else STAGE_APPLICABILITY[stage]
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

    # Check 1: writeup_body. #545: the pips' rule (card_level.check_writeup), so the score and
    # the pips agree. The narrative lives in the write-up document's type_metadata.body (#156:
    # every card gets an empty one on creation, so having a writeup_slug proves nothing).
    writeup_check = card_level.check_writeup(project)
    writeup_body_passed = writeup_check["passed"]
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
        "reason": writeup_check["reason"],
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

    # Check 1: cover_image (card_level.check_cover, the pips' rule)
    cover_check = card_level.check_cover(project)
    cover_passed = cover_check["passed"]
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
        "reason": cover_check["reason"],
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

    # Check 1: content_dates. #545: the pips' rule (card_level.check_dates): >= 80% of the
    # files have a real date (never the upload time), or the card's start and end are both set.
    dates_check = card_level.check_dates(project, content)
    content_dates_passed = dates_check["passed"]
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
        "reason": dates_check["reason"],
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
        "stage": stage,
        "stage_label": card_rules.stage_label(stage),
        "silent": silent,
        "score": score,
        "band": band,
        "earned": earned,
        "applicable": applicable,
        "checklist": checklist,
        "unmet": unmet,
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


@db.in_read_session
def score_all_projects():
    """Score every project and return aggregate dashboard data.

    Returns a dict with:
        - projects_by_stage: count of cards per stage label (#545: the stage is the only status)
        - average_health: average score of LIVE cards (in progress, done, in use; not silent ones)
        - gap_buckets: counts of projects failing each specific check
        - unfiled_count: count of capture_events not in any project
    """
    all_projects = db.list_projects()
    scores = [score_project(p) for p in all_projects]

    # Cards by stage
    projects_by_stage = {}
    for score in scores:
        label = score["stage_label"]
        projects_by_stage[label] = projects_by_stage.get(label, 0) + 1

    # Average health (only live, non-silent cards)
    live_scores = [s for s in scores if s["stage"] in LIVE_STAGES and not s["silent"]]
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
        "projects_by_stage": projects_by_stage,
        "average_health": round(average_health, 1),
        "gap_buckets": gap_buckets,
        "unfiled_count": unfiled_count,
        "all_project_scores": scores,
    }
