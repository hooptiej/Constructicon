"""explain_card (6): everything about one card, with the reasons.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import changes, db, policy, timeline
from .base import get_card, _status_warnings
from .text import text_fields
from .ops import suggest_provenance, whereabouts_fields
from .families import family_fields
from .links import list_links
from .hobbies import get_hobby, hobby_fields
from .decisions import decision_summary, open_card_decisions, provisional_legacy_status, status_fields
from .needs import list_needs_decision, NEED_UNTYPED_LINK
from .reshape import home_chain, _home_text, resolve_home, _slim


# --- explain_card (6) ---------------------------------------------------------------

def _file_summary(card, items):
    files = [i for i in items if i["slug"] != card.get("writeup_slug")]
    by_type = {}
    for f in files:
        by_type[f.get("media_type") or "unknown"] = by_type.get(f.get("media_type") or "unknown", 0) + 1
    dates = timeline.real_dates(files)  # #725: undated files have no say in the span
    return {"total": len(files), "by_type": by_type, "undated": len(files) - len(dates),
            "earliest": min(dates) if dates else None, "latest": max(dates) if dates else None}


def explain_card(card):
    """Everything about a card in one call (spec 6) -- what to read before proposing a
    change. Returns a dict: identity, status (kind/activity/stage/stop_reason, `provisional`),
    whereabouts, provenance, highlight, face text (synopsis / flavor / writeup_lead), hobbies, families, members, parent, children, links,
    home (+ `home_chain`), files (counts per type, date span), level (pips + reasons),
    caption_check (#556: the Curator's caption check, with the files it excuses and why),
    open_decisions, needs (computed + stored for this card), warnings, suggestions,
    recent_changes. Read-only."""
    from .. import card_level  # lazy: card_level imports db/timeline only, but keeps cards.py's import graph flat
    row = get_card(card)
    items = policy.filter_visible(db.list_project_items(row["id"]))  # #557 / #604 step 3
    parent = db.get_project(row["parent_id"]) if row.get("parent_id") else None
    home = resolve_home(row["id"])
    needs = list_needs_decision(card=row["slug"])
    warnings = _status_warnings(row, row.get("activity"), row.get("stage")) if row.get("stage") else []
    if home.get("dangling_override"):
        warnings.append("The manual home points at something that no longer exists; the automatic home is used.")
    suggestions = {"provenance": suggest_provenance(row),
                   "links": [n["link"] for n in needs if n["need"] == NEED_UNTYPED_LINK and n.get("link")]}
    recent = []
    for r in db.list_change_log(card_slug=row["slug"], limit=5):
        recent.append({"id": r["id"], "op": r["op"], "actor": r["actor"], "via": changes.via(r), "batch_id": r["batch_id"],
                       "timestamp": r["timestamp"], "undone": r.get("undone_by") is not None})
    out = {
        "id": row["id"], "slug": row["slug"], "title": row["title"], "description": row.get("description") or "",
        **status_fields(row),
        "provisional": provisional_legacy_status(row) is not None,
        **whereabouts_fields(row),
        **text_fields(row),
        "hobbies": [hobby_fields(get_hobby(r["hobby_tag_id"]), with_flags=False)
                    for r in db.list_hobby_rows(row["id"]) if db.get_hobby(r["hobby_tag_id"])],
        **family_fields(row),
        "parent": _slim(parent) if parent else None,
        "children": [_slim(c) for c in policy.filter_visible_cards(db.list_child_projects(row["id"]))],
        "links": list_links(row["id"]),
        "home": home,
        "home_chain": home_chain(row["id"]),
        "home_override": ({"kind": row["home_kind"], "ref": row["home_ref"],
                           "text": _home_text(row["home_kind"], row["home_ref"])} if row.get("home_kind") else None),
        "files": _file_summary(row, items),
        "level": card_level.card_level(row, items),
        "open_decisions": [decision_summary(d) for d in open_card_decisions(row["slug"])],
        "needs": [n for n in needs if n["decision_id"] is None],
        "warnings": warnings,
        "suggestions": suggestions,
        "recent_changes": recent,
    }
    from .. import curator  # lazy, like card_level above
    # #556: the Curator's caption check, so an agent sees which files it counts and which it excuses
    # (and why: "Word documents aren't captioned; their text is extracted instead").
    out["caption_check"] = next(({k: c.get(k) for k in ("passed", "excused", "reason", "excused_files")}
                                 for c in (curator.score_project(row) or {}).get("checklist", [])
                                 if c["item"] == "captions"), None)
    from .. import sharing  # lazy: sharing imports this module
    out["sharing"] = sharing.state("card", row["id"])  # #604 step 3
    if out["home_override"] and row["home_kind"] in ("card", "hobby"):
        t = db.get_project(row["home_ref"]) if row["home_kind"] == "card" else db.get_hobby(row["home_ref"])
        if t is not None and not (policy.can_view_card(t) if row["home_kind"] == "card" else policy.can_view_hobby(t)):
            out["home_override"] = {**out["home_override"], "text": None}  # #604 step 3: not spelled out
    return out
