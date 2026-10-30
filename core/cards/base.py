"""What every card op shares: the card decision kinds and patch ops, Result, get_card, the change-row
and status-warning helpers, and _changes_from_log (change-log rows -> Result.changes).

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

import json
from dataclasses import dataclass, field

from .. import db, policy
from ..card_rules import CardError


CARD_DECISION_PREFIX = "card:"
KIND_CARD_STATUS = "card_status"
KIND_CARD_BUILT_FOR = "card_built_for"
KIND_CARD_KIND = "card_kind"
KIND_CARD_FAMILY_MEMBERS = "card_family_members"
CARD_DECISION_KINDS = (KIND_CARD_STATUS, KIND_CARD_BUILT_FOR, KIND_CARD_KIND, KIND_CARD_FAMILY_MEMBERS)

# Patch ops the single-card decision resolver can apply (piece 4 added `link`).
# card_family_members decisions use their own multi-card path
# (_apply_family_patches) with FAMILY_PATCH_OPS.
SUPPORTED_PATCH_OPS = ("set_status", "set_kind", "link")
FAMILY_PATCH_OPS = ("unnest", "add_to_family")


@dataclass
class Result:
    ok: bool = True
    changes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    batch_id: str | None = None
    dry_run: bool = False
    data: dict = field(default_factory=dict)  # extra keys an operation returns (e.g. split's new cards)

    def to_dict(self):
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "changes": self.changes,
            "warnings": self.warnings,
            "batch_id": self.batch_id,
            **self.data,
        }


def card_decision_slug(slug):
    """pending_decisions.post_slug for a card decision (4.3)."""
    return f"{CARD_DECISION_PREFIX}{slug}"


def is_card_decision_slug(post_slug):
    return isinstance(post_slug, str) and post_slug.startswith(CARD_DECISION_PREFIX)


def get_card(card):
    """Resolves a project id or slug to its row, or raises CardError('not_found'). #604 step 3: a
    private card the actor can't see answers exactly like a missing one (every card op goes
    through here, so none can reach it)."""
    row = db.get_project(card)
    if row is None or not policy.can_view_card(row):
        raise CardError("not_found", f"No such card: {card!r}")
    return row


def _change_rows(slug, before, after):
    return [
        {"card": slug, "field": f, "before": before.get(f), "after": after[f]}
        for f in after
        if before.get(f) != after[f]
    ]


def _status_warnings(card, new_activity, new_stage):
    """Spec 3.2 rule 6: warnings, never errors."""
    warnings = []
    parent = db.get_project(card["parent_id"]) if card.get("parent_id") else None
    if parent is not None and parent.get("activity"):
        if new_activity == "inactive" and parent["activity"] == "active":
            warnings.append(f"'{card['title']}' is now inactive but is part of '{parent['title']}', which is active.")
        elif new_activity == "active" and parent["activity"] == "inactive":
            warnings.append(f"'{card['title']}' is now active but is part of '{parent['title']}', which is inactive.")
    if new_stage == "done":
        busy = [c["title"] for c in db.list_child_projects(card["id"]) if c.get("stage") == "in_progress"]
        if busy:
            warnings.append(f"Marked done while nested card(s) are still in progress: {', '.join(busy[:5])}.")
    return warnings


def _changes_from_log(rows):
    """Flattens change-log rows into [{op, table, key, field, before, after, card}]."""
    out = []
    for r in rows:
        card = r["affected_slugs"][0] if len(r.get("affected_slugs") or []) == 1 else None
        for m in r["mutations"]:
            b, a = m.get("before"), m.get("after")
            if m["table"] == "pending_decisions":
                # Summarize: the payload column is the whole question, far too big to echo.
                def _state(img):
                    if not img:
                        return None
                    if img.get("resolved_at") is None:
                        return "open"
                    try:
                        res = (json.loads(img["payload"]).get("resolution") or {}) if img.get("payload") else {}
                    except ValueError:  # silent-ok: display summary only; an unreadable payload shows as "?"
                        res = {}
                    return f"resolved ({res.get('choice') or res.get('stale') or '?'})"
                slug = None
                dec = db.get_pending_decision(m["key"]["id"])
                if dec and is_card_decision_slug(dec["post_slug"]):
                    slug = dec["post_slug"][len(CARD_DECISION_PREFIX):]
                out.append({"op": r["op"], "card": slug or card, "table": m["table"], "key": m["key"],
                            "field": "decision", "before": _state(b) if b and "resolved_at" in b else "open",
                            "after": _state(a) if a and "resolved_at" in a else None})
                continue
            if m["table"] == "projects":  # #745: a card's agent notes never ride in a change summary
                b = {k: v for k, v in b.items() if k not in db.PRIVATE_CARD_COLUMNS} if b else b
                a = {k: v for k, v in a.items() if k not in db.PRIVATE_CARD_COLUMNS} if a else a
            if b is None or a is None:
                if m["table"] == "capture_events":  # #541: an item row can carry a megabyte of OCR text
                    from .. import items as _items
                    b, a = _items._slim("capture_events", b), _items._slim("capture_events", a)
                out.append({"op": r["op"], "card": card, "table": m["table"], "key": m["key"],
                            "field": "(row)", "before": b, "after": a})
                continue
            for col in a:
                if col == "updated_at" or b.get(col) == a[col]:
                    continue
                out.append({"op": r["op"], "card": card, "table": m["table"], "key": m["key"], "field": col,
                            "before": b.get(col), "after": a[col]})
    return out
