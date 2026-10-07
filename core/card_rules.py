"""V2 card rules (docs/design/v2-cards.md, sections 1, 3, 5): enums, labels and
validation for cards. Pure -- no DB, no I/O -- so the web UI, the MCP server and
the migration all enforce the same rules by calling the same functions, and the
rules can be unit-tested with plain dicts.

Piece 1 scope: kind, activity/stage/stop_reason, the legacy-status adapters.
Whereabouts / provenance / membership / link validators arrive with the pieces
that add those fields (spec section 9).
"""

import logging
import re

from . import besteffort
from .errors import AppError

log = logging.getLogger("constructicon.card_rules")

# --- Kinds (3.1) -------------------------------------------------------------
KINDS = ("project", "thing", "action", "family", "collection", "event")
GROUP_KINDS = ("family", "collection")
KIND_LABELS = {
    "project": "Project",
    "thing": "Thing",
    "action": "Action",
    "family": "Family",
    "collection": "Collection",
    "event": "Event",
}

# --- Status (3.2) ------------------------------------------------------------
STAGES = ("in_progress", "in_use", "idea", "paused", "done", "stopped")
ACTIVITIES = ("active", "inactive")
STOP_REASONS = ("failed", "abandoned")
ACTIVITY_OF = {
    "in_progress": "active",
    "in_use": "active",
    "idea": "inactive",
    "paused": "inactive",
    "done": "inactive",
    "stopped": "inactive",
}
STAGE_LABELS = {
    "in_progress": "In progress",
    "in_use": "In use",
    "idea": "Idea",
    "paused": "Paused",
    "done": "Done",
    "stopped": "Stopped",
}
STOP_REASON_LABELS = {"failed": "Failed", "abandoned": "Abandoned"}
# Event cards only allow these stages (3.2 rule 4).
EVENT_STAGES = ("idea", "in_progress", "done", "stopped")

# --- Whereabouts (3.4) -------------------------------------------------------
# Where the physical thing is now. NULL = not recorded / not applicable.
WHEREABOUTS = ("have_it", "partial", "parted_out", "sold", "gifted", "lost", "never_built")
WHEREABOUTS_LABELS = {
    "have_it": "Have it",
    "partial": "Partial",
    "parted_out": "Parted out",
    "sold": "Sold",
    "gifted": "Gifted",
    "lost": "Lost",
    "never_built": "Never built",
}
# Kinds a whereabouts value applies to (an action, event or family has no physical whereabouts).
WHEREABOUTS_KINDS = ("thing", "project", "collection")

# --- Card-level provenance (3.5) ---------------------------------------------
# #529: these constants are the SEED for the editable lists (core/provenance_options.py,
# table provenance_options, managed in /admin) and the fallback if that table is
# unreadable. Validation, pickers and labels read the table, not these.
CARD_PROVENANCE = ("created", "found", "collected", "referenced", "client_owned")
CARD_PROVENANCE_LABELS = {
    "created": "Created",
    "found": "Found",
    "collected": "Collected",
    "referenced": "Referenced",
    "client_owned": "Client-owned",
}
# File-level (capture_events.provenance) value -> the card-vocabulary reading shown
# on an asset card. `documented` is deliberately file-only (a record OF something).
FILE_PROVENANCE_LABELS = {
    "found": "Found",
    "created": "Created",
    "reference": "Referenced",
    "result": "Created",
    "design": "Created",
    "documented": "Documented",
}
# File value -> the card provenance it counts toward in a majority suggestion.
# `documented` is evidence, not an origin, so it never votes.
FILE_PROVENANCE_TO_CARD = {
    "found": "found",
    "created": "created",
    "reference": "referenced",
    "result": "created",
    "design": "created",
    "purchased": "purchased",
}

# --- Hobby activity (3.3) ------------------------------------------------------
# A hobby's two-value manual switch (blog_tags.hobby_status). Never computed; the
# mismatch flags in core/db.hobby_flags only SURFACE disagreement with the cards.
HOBBY_ACTIVITIES = ("active", "inactive")
HOBBY_ACTIVITY_LABELS = {"active": "Active", "inactive": "Inactive"}
# The v1 words, accepted for one release and mapped to 'inactive' with a warning.
HOBBY_DEPRECATED_ALIASES = {"dormant": "inactive", "abandoned": "inactive"}
# Computed flag codes (3.3) and the staleness threshold for active_untouched.
HOBBY_STALE_DAYS = 730
HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK = "inactive_with_active_work"
HOBBY_FLAG_ACTIVE_UNTOUCHED = "active_untouched"
HOBBY_FLAG_LABELS = {
    HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK: "Inactive, but has active work",
    HOBBY_FLAG_ACTIVE_UNTOUCHED: "Active, but untouched for about 2 years",
}

# New-card defaults (3.2): matches v1's default status='active'.
DEFAULT_KIND = "project"
DEFAULT_STAGE = "in_progress"


class CardError(AppError):
    """A card rule violation (section 5). Since #548 it is the card flavour of the shared
    core.errors.AppError: `code` is a stable machine string shared by HTTP and MCP callers,
    and the status is derived from the code."""

    def __init__(self, code, message, details=None):
        super().__init__(code, message, details=details)

    @property
    def http_status(self):
        """409 for conflicts, 404 for a missing card, 422 for everything else."""
        if self.code == "not_found":
            return 404
        if self.code.endswith("_conflict") or self.code.startswith("nest_"):
            return 409
        return 422


def stage_label(stage):
    return STAGE_LABELS.get(stage, stage or "")


def kind_label(kind):
    return KIND_LABELS.get(kind, kind or "")


def validate_kind(kind):
    """Returns the kind unchanged or raises CardError('bad_kind')."""
    if kind not in KINDS:
        raise CardError("bad_kind", f"Unknown kind {kind!r}. Choose one of: {', '.join(KINDS)}.")
    return kind


def whereabouts_label(value):
    return WHEREABOUTS_LABELS.get(value, value or "")


def _po():
    from . import provenance_options  # lazy: it imports db, and this module stays pure at import time
    return provenance_options


def card_provenance_label(value):
    """Current label for a card provenance key, retired keys included (#529)."""
    if not value:
        return ""
    try:
        return _po().label("card", value, CARD_PROVENANCE_LABELS.get(value, value))
    except Exception as e:  # no DB / table yet: the seed labels
        besteffort.warn(log, "card_rules: card provenance label lookup, using the seed label", e, value=value)
        return CARD_PROVENANCE_LABELS.get(value, value)


def file_provenance_label(value, card_provenance=None):
    """Display label for a per-file provenance value on an asset card (3.5 table).
    A NULL file value inherits the owning card's provenance FOR DISPLAY ONLY (pass
    `card_provenance`); returns None when neither is set. The six original file keys keep
    their card-vocabulary reading (FILE_PROVENANCE_LABELS); a key added later reads its
    label from the editable list, and unknown custom values are shown as-is."""
    if value in (None, ""):
        return card_provenance_label(card_provenance) if card_provenance else None
    if value in FILE_PROVENANCE_LABELS:
        return FILE_PROVENANCE_LABELS[value]
    try:
        return _po().label("file", value)
    except Exception as e:
        besteffort.warn(log, "card_rules: file provenance label lookup, showing the raw key", e, value=value)
        return value


def validate_provenance(value, current=None):
    """Returns the card provenance unchanged (None / '' clear it) or raises
    CardError('bad_provenance'). #529: accepts an ACTIVE key of the editable card list,
    or `current` (the value the card already holds, even if since retired)."""
    return _po().validate("card", value, current=current)


def validate_whereabouts(kind, value, stage=None):
    """Whereabouts (3.4) for a card of `kind` currently at `stage`. None clears and
    always passes. Raises CardError('bad_whereabouts'): unknown value; a kind it
    doesn't apply to (action / event / family); the 3.2 rule 5 cross-field rules
    (in_use needs have_it or partial; never_built excludes in_progress / in_use)."""
    if value in (None, ""):
        return None
    if value not in WHEREABOUTS:
        raise CardError("bad_whereabouts",
                        f"Unknown whereabouts {value!r}. Choose one of: {', '.join(WHEREABOUTS)}.")
    kind = kind or DEFAULT_KIND
    if kind not in WHEREABOUTS_KINDS:
        raise CardError("bad_whereabouts",
                        f"Whereabouts doesn't apply to a {kind_label(kind)} (only to: "
                        f"{', '.join(kind_label(k) for k in WHEREABOUTS_KINDS)}).")
    if stage == "in_use" and value not in ("have_it", "partial"):
        raise CardError("bad_whereabouts",
                        f"A card that is in use can't be {value!r} (only 'have_it' or 'partial'); "
                        "change its stage first.")
    if value == "never_built" and stage in ("in_progress", "in_use"):
        raise CardError("bad_whereabouts", f"A card that is {stage!r} can't be 'never_built'; change its stage first.")
    return value


def validate_status(kind, stage, stop_reason=None, activity=None, whereabouts=None):
    """Validates a status triple and returns the normalized
    {"stage", "activity", "stop_reason"} to store. Raises CardError('bad_status').

    Rules (spec 3.2): stage is one of the six; activity, if supplied, must equal
    the stage's own activity (a mismatch is an error, never a silent fix);
    stop_reason is required iff stage == 'stopped'; an event allows only
    idea / in_progress / done / stopped; whereabouts cross-rules (in_use needs a
    thing you still have; never_built excludes in_progress and in_use).
    """
    if stage in (None, ""):
        if activity:
            raise CardError("bad_status", "Pick a stage: an activity alone isn't enough "
                            f"(choose one of: {', '.join(STAGES)}).")
        raise CardError("bad_status", f"A stage is required (choose one of: {', '.join(STAGES)}).")
    if stage not in STAGES:
        raise CardError("bad_status", f"Unknown stage {stage!r}. Choose one of: {', '.join(STAGES)}.")
    expected_activity = ACTIVITY_OF[stage]
    if activity not in (None, "") and activity != expected_activity:
        raise CardError(
            "bad_status",
            f"Stage {stage!r} is {expected_activity}, so activity {activity!r} doesn't fit "
            "(an idea can never be active).",
        )
    if stage == "stopped":
        if stop_reason not in STOP_REASONS:
            raise CardError("bad_status", "A stopped card needs a stop reason: failed or abandoned.")
    elif stop_reason not in (None, ""):
        raise CardError("bad_status", f"A stop reason only applies to stopped cards, not {stage!r}.")
    if kind == "event" and stage not in EVENT_STAGES:
        raise CardError("bad_status", f"An event can't be {stage!r}; use one of: {', '.join(EVENT_STAGES)}.")
    if whereabouts:
        if stage == "in_use" and whereabouts not in ("have_it", "partial"):
            raise CardError(
                "bad_status",
                f"A card can't be in use when whereabouts is {whereabouts!r} (only 'have_it' or 'partial').",
            )
        if whereabouts == "never_built" and stage in ("in_progress", "in_use"):
            raise CardError("bad_status", f"A never-built card can't be {stage!r}.")
    return {
        "stage": stage,
        "activity": expected_activity,
        "stop_reason": stop_reason if stage == "stopped" else None,
    }


# --- Legacy v1 status adapters (4.1, 4.2) ------------------------------------
# projects.status is frozen: the static export still reads it. Live code reads
# stage instead, through these.

LEGACY_STATUSES = ("wip", "complete", "shelved", "means-to-an-end", "abandoned", "failed",
                   "idea", "published", "reference-only", "active", "archived")


def migration_target(legacy_status):
    """Exact v1 -> v2 mapping from spec 4.2. Returns a dict:
        kind, stage, stop_reason  -- what to store now
        definite (bool)           -- True = automatic, False = provisional + a queued decision
        question                  -- None | 'card_status' | 'card_built_for'
    Unknown values map to a provisional 'paused' with a card_status question.
    """
    s = legacy_status
    if s in ("wip", "active"):
        return {"kind": "project", "stage": "in_progress", "stop_reason": None, "definite": True, "question": None}
    if s == "abandoned":
        return {"kind": "project", "stage": "stopped", "stop_reason": "abandoned", "definite": True, "question": None}
    if s == "failed":
        return {"kind": "project", "stage": "stopped", "stop_reason": "failed", "definite": True, "question": None}
    if s == "idea":
        return {"kind": "project", "stage": "idea", "stop_reason": None, "definite": True, "question": None}
    if s == "published":
        return {"kind": "project", "stage": "done", "stop_reason": None, "definite": True, "question": None}
    if s == "reference-only":
        return {"kind": "collection", "stage": "in_use", "stop_reason": None, "definite": True, "question": None}
    if s in ("complete", "archived"):
        return {"kind": "project", "stage": "done", "stop_reason": None, "definite": False, "question": "card_status"}
    if s == "shelved":
        return {"kind": "project", "stage": "paused", "stop_reason": None, "definite": False, "question": "card_status"}
    if s == "means-to-an-end":
        return {"kind": "project", "stage": "done", "stop_reason": None, "definite": False, "question": "card_built_for"}
    return {"kind": "project", "stage": "paused", "stop_reason": None, "definite": False, "question": "card_status"}


def legacy_to_status(word):
    """Translate a v1 status word (as accepted by the old set_project_status
    tool and the old `status` form field) into a status change.

    Returns {"stage", "stop_reason", "kind": None | 'collection', "warnings": [..]}.
    Raises CardError('bad_status') for a word that is neither legacy nor a stage.
    A bare v2 stage name is accepted too, so callers can pass either vocabulary.
    """
    if word in STAGES:
        return {"stage": word, "stop_reason": None, "kind": None, "warnings": []}
    if word not in LEGACY_STATUSES:
        raise CardError(
            "bad_status",
            f"Unknown status {word!r}. Use a stage ({', '.join(STAGES)}) or a legacy word "
            f"({', '.join(LEGACY_STATUSES)}).",
        )
    tgt = migration_target(word)
    warnings = [f"Legacy status {word!r} translated to stage {tgt['stage']!r}"
                + (f" ({tgt['stop_reason']})" if tgt["stop_reason"] else "")
                + "; prefer constructicon_set_status with a stage."]
    if word in ("complete", "archived", "means-to-an-end"):
        warnings.append("Legacy 'complete' is ambiguous: done (finished) or in_use (finished and still used)? "
                        "Applied 'done'; pass stage='in_use' if it is still in use.")
    if word == "shelved":
        warnings.append("Legacy 'shelved' applied as 'paused'.")
    return {
        "stage": tgt["stage"],
        "stop_reason": tgt["stop_reason"],
        "kind": "collection" if word == "reference-only" else None,
        "warnings": warnings,
    }


def curator_status(card, provisional_legacy=None):
    """Adapter for the Curator (spec 4.1): the v1-vocabulary status the scoring
    rules understand, derived from the live stage.

    in_progress->wip, in_use/done->complete, paused->shelved, idea->idea,
    stopped+failed->failed, stopped+abandoned->abandoned, collection->reference-only.

    `provisional_legacy` is the legacy word recorded on an *open* migration
    decision for this card. While a card's status is still a provisional guess
    (e.g. a means-to-an-end card the migration parked on 'done'), the Curator
    keeps scoring it by what v1 said, so migrating doesn't change a score
    before the owner has answered. A row that has no stage yet (pre-migration
    data) falls back to its legacy status column.
    """
    if provisional_legacy:
        return provisional_legacy
    stage = card.get("stage")
    if not stage:
        return card.get("status") or "wip"
    if card.get("kind") == "collection" and card.get("provenance") in (None, "referenced"):
        # Piece 1: the only collections are migrated v1 reference-only cards.
        return "reference-only"
    if stage == "in_progress":
        return "wip"
    if stage in ("in_use", "done"):
        return "complete"
    if stage == "paused":
        return "shelved"
    if stage == "idea":
        return "idea"
    if stage == "stopped":
        return "failed" if card.get("stop_reason") == "failed" else "abandoned"
    return "wip"


def export_status(card):
    """The v1-vocabulary status the STATIC EXPORT should use for a card (#512).

    projects.status is frozen at the V2 migration; the live status is
    kind/stage/stop_reason. The export keeps speaking v1, so this returns the
    legacy-equivalent word:

    * Unchanged card: if (kind, stage, stop_reason) is still exactly what
      migration_target() mapped the frozen status to (including the provisional
      values for queued ones), return the frozen `status` untouched. So a
      `complete` card still provisional `done` stays `complete`, `archived`
      stays `archived`, and an unchanged export is byte-identical.
    * Changed or new card: map from the live stage.

        kind collection (any stage)       -> reference-only
        stage in_progress                 -> wip
        stage done                        -> complete   (event done too)
        stage in_use                      -> complete   (finished, still used)
        stage paused                      -> shelved
        stage stopped + stop_reason failed     -> failed
        stage stopped + stop_reason abandoned  -> abandoned
        stage idea                        -> idea
        no stage at all (pre-migration row)    -> frozen status, else wip
    """
    frozen = card.get("status")
    stage = card.get("stage")
    if not stage:
        return frozen or "wip"
    tgt = migration_target(frozen)
    if (tgt["kind"], tgt["stage"], tgt["stop_reason"]) == (
            card.get("kind") or "project", stage, card.get("stop_reason") or None):
        return frozen
    if card.get("kind") == "collection":
        return "reference-only"
    if stage == "in_progress":
        return "wip"
    if stage in ("done", "in_use"):
        return "complete"
    if stage == "paused":
        return "shelved"
    if stage == "idea":
        return "idea"
    if stage == "stopped":
        return "failed" if card.get("stop_reason") == "failed" else "abandoned"
    return "wip"


def export_included_by_default(card):
    """Whether the export-everything default includes this card (#512). v1 used
    db.list_projects(status="active"), i.e. only cards whose status was the literal
    word 'active' (the other in-progress word, 'wip', never matched; that quirk is
    kept so unchanged output stays byte-identical). Unchanged cards therefore keep
    that rule on their frozen status; a card whose stage has changed is included
    exactly when it is now in progress."""
    status = export_status(card)
    if status == card.get("status"):
        return status == "active"
    return status in ("wip", "active")


# --- Families, collections and nesting (3.6, 3.7) ---------------------------------

def validate_membership(family, member):
    """Family / collection membership (3.6). `family` and `member` are project
    dicts (needs id, kind, title). The family must be a group kind; the member
    must differ from it and must not be a group kind itself (flat, one level).
    Raises CardError('bad_membership')."""
    fkind = family.get("kind") or DEFAULT_KIND
    if fkind not in GROUP_KINDS:
        raise CardError("bad_membership",
                        f"'{family['title']}' is a {kind_label(fkind)}, not a family or collection, so it can't have members.")
    if family["id"] == member["id"]:
        raise CardError("bad_membership", "A family can't be a member of itself.")
    mkind = member.get("kind") or DEFAULT_KIND
    if mkind in GROUP_KINDS:
        raise CardError("bad_membership",
                        f"'{member['title']}' is a {kind_label(mkind)}; families and collections can't contain each other.")
    return True


def validate_nest(child, parent, descendants_of_child, replace=False):
    """Nesting means "part of" only (3.7). `child` and `parent` are project dicts
    (`child` may be a not-yet-created card: {"id": None, "kind": ..., "parent_id": None});
    `descendants_of_child` is the set of ids at or below the child.

    Raises CardError: nest_self, nest_group_kind (a family/collection can't be
    nested or be a nesting parent; use membership), nest_cycle, nest_second_parent
    (a card already part of something else; pass replace=True to move it).
    """
    if child.get("id") is not None and child["id"] == parent["id"]:
        raise CardError("nest_self", "A card can't be part of itself.")
    for role, card in (("child", child), ("parent", parent)):
        k = card.get("kind") or DEFAULT_KIND
        if k in GROUP_KINDS:
            who = f"'{card['title']}' is a {kind_label(k)}"
            if role == "child":
                raise CardError("nest_group_kind", f"{who}, and a {k} can't be part of another card; use membership instead.")
            raise CardError("nest_group_kind", f"{who}, and a {k} can't be a nesting parent; use membership instead.")
    if parent["id"] in set(descendants_of_child or ()):
        raise CardError("nest_cycle", f"'{parent['title']}' is already nested under '{child['title']}'; that would be a loop.")
    current = child.get("parent_id")
    if current is not None and current != parent["id"] and not replace:
        raise CardError("nest_second_parent",
                        f"'{child['title']}' is already part of another card; a card has one parent. "
                        "Unnest it first, or pass replace=true to move it.",
                        {"current_parent_id": current})
    return True


# --- Typed links (3.8) -------------------------------------------------------------
# A row (a, b, type) reads "a <type> b". Directed types are stored as ONE row;
# `related` is symmetric and stored as TWO rows ((a,b) and (b,a)), exactly as v1.
LINK_TYPES = ("built_for", "applies_to", "used_in", "inspired_by", "related")
DIRECTED_LINK_TYPES = ("built_for", "applies_to", "used_in", "inspired_by")
# Types whose SOURCE must not be a group kind (a family/collection isn't built for
# or applied to anything; it holds things that are).
GROUP_SOURCE_BLOCKED_LINK_TYPES = ("built_for", "applies_to", "used_in")
# (forward label, reverse label): shown from the source's side / the target's side.
LINK_LABELS = {
    "built_for": ("Built for", "Made for this"),
    "applies_to": ("Applies to", "Applied here"),
    "used_in": ("Used in", "Uses"),
    "inspired_by": ("Inspired by", "Inspired"),
    "related": ("Related", "Related"),
}


def link_label(link_type, direction="out"):
    """Display label for a link type from one side. direction: 'out' (this card is
    the source), 'in' (this card is the target), 'both' (symmetric)."""
    fwd, rev = LINK_LABELS.get(link_type, (link_type, link_type))
    return rev if direction == "in" else fwd


def validate_link_type(link_type):
    """Returns the type unchanged or raises CardError('bad_link')."""
    if link_type not in LINK_TYPES:
        raise CardError("bad_link", f"Unknown link type {link_type!r}. Choose one of: {', '.join(LINK_TYPES)}.")
    return link_type


def validate_link(a, b, link_type, existing):
    """Validates adding the link "a <link_type> b" (3.8). `a` / `b` are project
    dicts (slug, title, kind); `existing` is the list of link rows already on
    this pair in EITHER direction ({slug_a, slug_b, type}), after removing any
    rows the caller is about to replace (retype).

    Raises CardError: bad_link (unknown type, self-link, a family/collection as the
    source of built_for/applies_to/used_in) or link_conflict (the link already
    exists; `related` over a typed pair; details['reason'] says which).
    Returns {"drop_related": bool}: True when a typed link is being added over a
    `related` pair, which upgrades it (the related rows are removed)."""
    validate_link_type(link_type)
    if a["slug"] == b["slug"]:
        raise CardError("bad_link", "A card can't be linked to itself.")
    akind = a.get("kind") or DEFAULT_KIND
    if akind in GROUP_KINDS and link_type in GROUP_SOURCE_BLOCKED_LINK_TYPES:
        raise CardError("bad_link",
                        f"'{a['title']}' is a {kind_label(akind)}, which can't be the source of a "
                        f"'{link_type}' link; link its members instead.")
    pair = {a["slug"], b["slug"]}
    rows = [r for r in existing if {r["slug_a"], r["slug_b"]} == pair]
    if link_type == "related":
        if any(r["type"] == "related" for r in rows):
            raise CardError("link_conflict", f"'{a['title']}' and '{b['title']}' are already related.",
                            {"reason": "duplicate"})
        typed = sorted({r["type"] for r in rows if r["type"] != "related"})
        if typed:
            raise CardError("link_conflict",
                            f"'{a['title']}' and '{b['title']}' already have a typed link ({', '.join(typed)}); "
                            "a pair can't be both typed and related. Unlink or retype that first.",
                            {"reason": "related_over_typed", "existing_types": typed})
        return {"drop_related": False}
    if any(r["type"] == link_type and r["slug_a"] == a["slug"] and r["slug_b"] == b["slug"] for r in rows):
        raise CardError("link_conflict", f"'{a['title']}' already {link_type.replace('_', ' ')} '{b['title']}'.",
                        {"reason": "duplicate"})
    return {"drop_related": any(r["type"] == "related" for r in rows)}


def validate_hobby_activity(value, allow_aliases=True):
    """Returns (activity, warnings) for a hobby activity word, or raises
    CardError('bad_hobby_activity'). With allow_aliases the v1 words 'dormant' and
    'abandoned' map to 'inactive' and the warnings say so (3.3, one release)."""
    v = value.strip().lower() if isinstance(value, str) else value
    if v in HOBBY_ACTIVITIES:
        return v, []
    if allow_aliases and v in HOBBY_DEPRECATED_ALIASES:
        return HOBBY_DEPRECATED_ALIASES[v], [
            f"'{v}' is deprecated; hobbies are now just active or inactive. Stored as 'inactive'."]
    raise CardError("bad_hobby_activity",
                    f"Unknown hobby status {value!r}. Choose one of: {', '.join(HOBBY_ACTIVITIES)}.")


def _alnum(text):
    return "".join(ch for ch in text if ch.isalnum())


def derive_group_code(name, taken=()):
    """The default 2-4 char hobby code (3.9), unique against `taken` (compared
    case-insensitively). Multi-word names take initials, where a short (<=2 char)
    word contributes all its characters ("R/C Adventures" -> RCA, "3D Printing" ->
    3DP), max 4; a single word takes its first 3 letters (Collecting -> COL).
    Collisions try the next letters of the first word, then a digit."""
    taken_up = {t.upper() for t in taken if t}
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w]
    if not words:
        words = ["X"]
    if len(words) > 1:
        base = "".join(w if len(w) <= 2 else w[0] for w in words).upper()[:4]
    else:
        base = words[0][:3].upper()
    if len(base) < 2:
        base = (base + _alnum(words[0]).upper() + "XX")[:2]
    candidates = [base]
    first = _alnum(words[0]).upper()
    for i in range(1, len(first)):
        cand = (base[:-1] + first[i])[:4] if len(base) > 1 else base + first[i]
        candidates.append(cand)
    for cand in candidates:
        if cand not in taken_up:
            return cand
    stem = base[:3]
    for n in range(2, 100):
        cand = f"{stem}{n}"[:4]
        if cand not in taken_up:
            return cand
    raise CardError("bad_group_code", f"No free group code left for {name!r}.")


def validate_group_code(value):
    """Normalized 2-4 char alphanumeric uppercase code, or CardError('bad_group_code')."""
    v = (value or "").strip().upper()
    if not (2 <= len(v) <= 4) or not v.isalnum():
        raise CardError("bad_group_code", "A group code is 2-4 letters or digits.")
    return v


# --- Piece 6: home, bulk, split (3.10, section 6) ----------------------------------
HOME_KINDS = ("card", "hobby")
HOME_SOURCES = ("override", "parent", "family", "hobby", "none")

# Operations constructicon_bulk_edit may run (spec section 6, bulk). Allow-list: anything
# else is refused with bad_bulk_op.
BULK_OPS = ("set_status", "set_kind", "set_whereabouts", "set_card_provenance", "set_home", "set_card_highlight",
            "add_to_hobby", "remove_from_hobby", "add_to_family", "remove_from_family", "nest", "unnest",
            "link", "unlink", "retype_link", "set_hobby_activity")

SPLIT_RELATIONS = ("child", "sibling")
# Keys a split part may carry (anything else is refused so a typo can't silently do nothing).
SPLIT_PART_KEYS = ("title", "kind", "description", "move_description", "provenance", "provenance_credit", "stage",
                   "stop_reason", "whereabouts", "whereabouts_note", "relation", "file_slugs", "link_to_source",
                   "hobbies", "families", "highlight")


def validate_home_kind(value):
    if value not in HOME_KINDS:
        raise CardError("bad_home", f"A home is a card or a hobby, not {value!r}.")
    return value


def validate_bulk_op(op):
    if op not in BULK_OPS:
        raise CardError("bad_bulk_op", f"{op!r} can't be run in bulk. Choose one of: {', '.join(BULK_OPS)}.")
    return op


def validate_split_part(part, index=0):
    """Shape check for one split_card part (the field values themselves are validated by the
    same validators a manual edit uses, when the part is built). Returns the relation."""
    if not isinstance(part, dict):
        raise CardError("bad_split", f"Part {index + 1} must be an object.")
    unknown = sorted(set(part) - set(SPLIT_PART_KEYS))
    if unknown:
        raise CardError("bad_split", f"Part {index + 1} has unknown field(s): {', '.join(unknown)}.",
                        {"allowed": list(SPLIT_PART_KEYS)})
    if not (part.get("title") or "").strip():
        raise CardError("bad_split", f"Part {index + 1} needs a title.")
    relation = part.get("relation") or "sibling"
    if relation not in SPLIT_RELATIONS:
        raise CardError("bad_split", f"Part {index + 1}: relation must be 'child' or 'sibling'.")
    return relation


# --- Card face text (#596) -----------------------------------------------------------
# The text box of a card face: an optional hand-written synopsis (a few sentences) and an
# optional one-line italic flavor. Shared by cards (projects.synopsis / flavor) and hobbies
# (hobby_settings.synopsis / flavor). The write-up lead and the description are not set here.
SYNOPSIS_MAX = 600
FLAVOR_MAX = 140
CARD_TEXT_LIMITS = {"synopsis": SYNOPSIS_MAX, "flavor": FLAVOR_MAX}  # the edit form's maxlength


def validate_card_text(synopsis=..., flavor=...):
    """The text fields to write, from what the caller passed: `...` = leave alone (absent from the
    result), None or blank = clear (None), else the cleaned text. A synopsis keeps its line breaks
    (one paragraph per line, blank lines dropped); a flavor is one line. Raises
    CardError('bad_card_text') when nothing was passed or a value is too long."""
    out = {}
    if synopsis is not ...:
        lines = [" ".join(line.split()) for line in str(synopsis or "").splitlines()]
        text = "\n".join(line for line in lines if line)
        if len(text) > SYNOPSIS_MAX:
            raise CardError("bad_card_text", f"The synopsis is {len(text)} characters; keep it under {SYNOPSIS_MAX} "
                            "(a few sentences: the full story belongs in the write-up).")
        out["synopsis"] = text or None
    if flavor is not ...:
        text = " ".join(str(flavor or "").split())
        if len(text) > FLAVOR_MAX:
            raise CardError("bad_card_text", f"The flavor line is {len(text)} characters; keep it under {FLAVOR_MAX}.")
        out["flavor"] = text or None
    if not out:
        raise CardError("bad_card_text", "Nothing to set: pass a synopsis and/or a flavor line (blank clears).")
    return out
