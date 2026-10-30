"""Card needs (spec 6): the stored card_* decisions plus every computed need (hobby flags, missing
provenance / whereabouts, untyped links, status conflicts, blank write-ups), merged by
list_needs_decision.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

import re

from .. import card_rules, db, policy
from .base import card_decision_slug, is_card_decision_slug, _status_warnings
from .ops import suggest_provenance
from .decisions import decision_summary


# Computed needs (never stored, always current) that list_needs_decision merges in.
NEED_HOBBY_INACTIVE_WITH_ACTIVE_WORK = "hobby_inactive_with_active_work"
NEED_HOBBY_ACTIVE_UNTOUCHED = "hobby_active_untouched"
NEED_UNTYPED_LINK = "untyped_link"
NEED_STATUS_CONFLICT = "status_conflict"
NEED_BLANK_WRITEUP_WITH_FILES = "blank_writeup_with_files"


NEED_MISSING_PROVENANCE = "missing_provenance"
NEED_MISSING_PROVENANCE_CREDIT = "missing_provenance_credit"
NEED_MISSING_WHEREABOUTS = "missing_whereabouts"
# Provenances where "who made it / where it came from" is worth a credit.
CREDIT_PROVENANCE = ("found", "collected")


def provenance_whereabouts_needs(kind=None, hobby_ids=None, only_id=None):
    """Computed needs (never stored): missing_provenance (every card with no provenance whose
    kind is nagged, card_rules.provenance_nag_applies: not a family, #554; carries the
    majority-file suggestion when there is one), missing_provenance_credit (found / collected
    card with no credit; not a family either) and
    missing_whereabouts (a Thing with no whereabouts). `only_id` limits it to one card."""
    from .. import provenance_options  # lazy, as elsewhere in this module
    prov_options = provenance_options.list_options("card")  # #529: the live, editable list
    rows = []
    for c in db.list_projects():
        if only_id is not None and c["id"] != only_id:
            continue
        ck = c.get("kind") or "project"
        if (kind and ck != kind) or (hobby_ids is not None and c["id"] not in hobby_ids):
            continue
        prov = c.get("provenance")
        nag = card_rules.provenance_nag_applies(ck)
        if not prov and nag:
            sug = suggest_provenance(c)
            rows.append({
                "need": NEED_MISSING_PROVENANCE, "card_slug": c["slug"], "title": c["title"],
                "detail": "No provenance recorded (" + ", ".join(o["label"].lower() for o in prov_options) + ").",
                "suggested": sug["value"] if sug else None,
                "suggested_reason": (f"{sug['files']} of {sug['counted']} files map to "
                                     f"{card_rules.card_provenance_label(sug['value'])}.") if sug else None,
                "confidence": ("medium" if sug and sug["share"] >= 0.8 else "low") if sug else None,
                "decision_id": None,
                "options": [{"key": o["key"], "label": o["label"]} for o in prov_options],
            })
        elif nag and prov in CREDIT_PROVENANCE and not (c.get("provenance_credit") or "").strip():
            rows.append({
                "need": NEED_MISSING_PROVENANCE_CREDIT, "card_slug": c["slug"], "title": c["title"],
                "detail": f"{card_rules.card_provenance_label(prov)} card with no credit (who designed it / where it came from).",
                "suggested": None, "suggested_reason": None, "confidence": None,
                "decision_id": None, "options": [],
            })
        if ck == "thing" and not c.get("whereabouts"):
            rows.append({
                "need": NEED_MISSING_WHEREABOUTS, "card_slug": c["slug"], "title": c["title"],
                "detail": "No whereabouts recorded (have it, partial, parted out, sold, gifted, lost, abandoned, never built).",
                "suggested": None, "suggested_reason": None, "confidence": None,
                "decision_id": None,
                "options": [{"key": k, "label": card_rules.WHEREABOUTS_LABELS[k]} for k in card_rules.WHEREABOUTS],
            })
    return rows


_BUILT_FOR_RE = r"built (?:for|to fit|to go on|to suit)\b[^.\n]{0,40}?"
_INSPIRED_RE = r"inspired by\b[^.\n]{0,40}?"
_SOFTWARE_RE = re.compile(r"\b(code|script|software|firmware|app|lua|library|design|tool)\b", re.I)


def untyped_link_needs(kind=None, hobby_ids=None):
    """Computed need `untyped_link` (never stored): v1 `related` pairs where a typed
    reading is plausible, with the suggested type and direction. Heuristics, all
    deterministic and never applied: a card's text says 'built for' / 'inspired by'
    followed by the other card's title; a software/design card nested under the
    other (applies_to). Row: {need, card_slug (the source), title, detail,
    suggested ('<type>'), link: {a, b, type}, decision_id: None, options}."""
    related = [r for r in db.list_project_link_rows() if r["type"] == "related" and r["slug_a"] < r["slug_b"]]
    if not related:
        return []
    text_cache = {}

    def text_of(c):
        if c["slug"] not in text_cache:
            body = ""
            if c.get("writeup_slug"):
                w = db.get_by_slug(c["writeup_slug"])
                if w:
                    body = (w.get("type_metadata") or {}).get("body", "") or ""
            text_cache[c["slug"]] = f"{c.get('description') or ''} {body}"
        return text_cache[c["slug"]]

    def reading(x, y):
        """A typed reading of 'x <type> y', or None."""
        t = text_of(x)
        title = re.escape((y["title"] or "").strip())
        if len(title) >= 4:
            if re.search(_BUILT_FOR_RE + title, t, re.I):
                return "built_for", f"'{x['title']}' says it was built for '{y['title']}'"
            if re.search(_INSPIRED_RE + title, t, re.I):
                return "inspired_by", f"'{x['title']}' says it was inspired by '{y['title']}'"
        if x.get("parent_id") == y["id"] and _SOFTWARE_RE.search(f"{x['title']} {x.get('description') or ''}"):
            return "applies_to", f"'{x['title']}' is nested under '{y['title']}' and reads like software or a design"
        return None

    rows = []
    for r in related:
        p, q = db.get_project(r["slug_a"]), db.get_project(r["slug_b"])
        if p is None or q is None:
            continue
        for x, y in ((p, q), (q, p)):
            found = reading(x, y)
            if not found:
                continue
            if kind and (x.get("kind") or "project") != kind:
                break
            if hobby_ids is not None and x["id"] not in hobby_ids:
                break
            t, why = found
            rows.append({
                "need": NEED_UNTYPED_LINK,
                "card_slug": x["slug"],
                "title": x["title"],
                "detail": f"'{x['title']}' is just 'related' to '{y['title']}'. Reads like: {t.replace('_', ' ')}.",
                "suggested": t,
                "suggested_reason": why,
                "confidence": "low",
                "link": {"a": x["slug"], "b": y["slug"], "type": t},
                "decision_id": None,
                "options": [{"key": k, "label": card_rules.LINK_LABELS[k][0]} for k in card_rules.DIRECTED_LINK_TYPES],
            })
            break
    return rows


def hobby_needs():
    """Computed hobby needs for list_needs_decision (never stored, always current):
    hobby_inactive_with_active_work and hobby_active_untouched."""
    need_of = {
        card_rules.HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK: NEED_HOBBY_INACTIVE_WITH_ACTIVE_WORK,
        card_rules.HOBBY_FLAG_ACTIVE_UNTOUCHED: NEED_HOBBY_ACTIVE_UNTOUCHED,
    }
    rows = []
    for h in db.list_hobbies():
        for flag in db.hobby_flags(h["id"]):
            if flag["code"] == card_rules.HOBBY_FLAG_INACTIVE_WITH_ACTIVE_WORK:
                suggested = "active"
                reason = "It has active work in it; the switch probably should be Active."
            else:
                suggested = "inactive"
                reason = "Nothing in it has been touched for about 2 years."
            rows.append({
                "need": need_of[flag["code"]],
                "card_slug": None,
                "hobby_slug": h["slug"],
                "title": h["name"],
                "detail": f"{flag['label']}. {flag['detail']}",
                "suggested": suggested,
                "suggested_reason": reason,
                "confidence": "low",
                "decision_id": None,
                "options": [{"key": "active", "label": "Active"}, {"key": "inactive", "label": "Inactive"}],
            })
    return rows


@db.in_read_session
def list_needs_decision(kind=None, need=None, hobby=None, limit=None, card=None):
    """Open card questions for the owner (spec 6): the STORED card_* decisions plus
    the computed needs from the pieces that exist so far (piece 2: the two hobby
    flags; missing provenance, untyped links, ... join in with their pieces).

    Row: {need, card_slug, title, detail, suggested|None, decision_id|None, ...}.
    Filters: `kind` (the card's kind), `need` (the decision kind), `hobby`
    (hobby slug or id), `limit`, `card` (one card's slug or id). Sorted by card title.
    """
    hobby_ids = None
    if hobby not in (None, ""):
        h = db.get_hobby(hobby)
        hobby_ids = {p["id"] for p in db.list_projects_for_hobby(h["id"])} if h else set()
    # #524: one card asked for -> compute only that card's needs, not every card's then filter.
    picked_card = db.get_project(card) if card not in (None, "") else None
    if card not in (None, "") and picked_card is None:
        return []
    only_id = picked_card["id"] if picked_card else None
    rows = []
    stored = (db.list_all_pending_decisions(kind_prefix="card_", post_slug=card_decision_slug(picked_card["slug"]))
              if picked_card else db.list_all_pending_decisions(kind_prefix="card_"))
    for d in stored:
        if d["resolved_at"] is not None or not is_card_decision_slug(d["post_slug"]):
            continue
        if need and d["kind"] != need:
            continue
        s = decision_summary(d)
        card_row = db.get_project(s["card_slug"])
        if card_row is None:
            continue
        if kind and (card_row.get("kind") or "project") != kind:
            continue
        if hobby_ids is not None and card_row["id"] not in hobby_ids:
            continue
        rows.append({
            "need": s["need"],
            "card_slug": s["card_slug"],
            "title": card_row["title"],
            "detail": s["question"],
            "suggested": s["suggested"],
            "suggested_reason": s["suggested_reason"],
            "confidence": s["confidence"],
            "decision_id": s["decision_id"],
            "options": s["options"],
        })
    # Computed hobby needs (3.3). They belong to a hobby, not a project card, so a
    # `kind` filter other than 'hobby' leaves them out; `hobby` narrows to one hobby.
    if kind in (None, "", "hobby") and picked_card is None:
        picked = db.get_hobby(hobby) if hobby not in (None, "") else None
        for r in hobby_needs():
            if need and r["need"] != need:
                continue
            if hobby not in (None, "") and (picked is None or r["hobby_slug"] != picked["slug"]):
                continue
            rows.append(r)
    # Computed untyped_link (3.8): v1 'related' pairs with a plausible typed reading.
    if not need or need == NEED_UNTYPED_LINK:
        rows += untyped_link_needs(kind=kind if kind not in (None, "", "hobby") else None, hobby_ids=hobby_ids)             if kind != "hobby" else []
    # Computed provenance / whereabouts needs (3.4, 3.5; piece 5).
    if kind != "hobby" and (not need or need in (NEED_MISSING_PROVENANCE, NEED_MISSING_PROVENANCE_CREDIT,
                                                  NEED_MISSING_WHEREABOUTS)):
        rows += [r for r in provenance_whereabouts_needs(kind=kind or None, hobby_ids=hobby_ids, only_id=only_id)
                 if not need or r["need"] == need]
    # Computed status_conflict / blank_writeup_with_files (3.2 rule 6, 3.11; piece 6).
    if kind != "hobby" and (not need or need in (NEED_STATUS_CONFLICT, NEED_BLANK_WRITEUP_WITH_FILES)):
        rows += [r for r in status_and_writeup_needs(kind=kind or None, hobby_ids=hobby_ids, only_id=only_id)
                 if not need or r["need"] == need]
    if picked_card is not None:
        rows = [r for r in rows if r.get("card_slug") == picked_card["slug"]]
    acc = policy.card_access()  # #604 step 3: never a need about (or naming) a private card the actor isn't on
    if not acc.empty:
        def _shown(r):
            slugs = [r.get("card_slug")] + ([r["link"].get("a"), r["link"].get("b")] if r.get("link") else [])
            for sl in filter(None, slugs):
                c = db.get_project(sl)
                if c is not None and acc.card_hidden(c["id"]):
                    return False
            h = db.get_hobby(r["hobby_slug"]) if r.get("hobby_slug") else None
            return h is None or not acc.hobby_hidden(h["id"])
        rows = [r for r in rows if _shown(r)]
    rows.sort(key=lambda r: (r["title"].lower(), r["need"]))
    return rows[:limit] if limit else rows


# --- Computed needs added in piece 6 -------------------------------------------------

def status_and_writeup_needs(kind=None, hobby_ids=None, only_id=None):
    """Computed needs (never stored): status_conflict (a 3.2 rule 6 warning that holds right
    now: active/inactive disagrees with the parent, or Done with nested work still in
    progress) and blank_writeup_with_files (the auto-made write-up is still empty although
    the card has files). `only_id` limits it to one card."""
    rows = []
    for c in db.list_projects():
        if only_id is not None and c["id"] != only_id:
            continue
        ck = c.get("kind") or "project"
        if (kind and ck != kind) or (hobby_ids is not None and c["id"] not in hobby_ids):
            continue
        if c.get("stage"):
            warns = _status_warnings(c, c.get("activity"), c.get("stage"))
            if warns:
                rows.append({"need": NEED_STATUS_CONFLICT, "card_slug": c["slug"], "title": c["title"],
                             "detail": " ".join(warns), "suggested": None, "suggested_reason": None,
                             "confidence": None, "decision_id": None, "options": []})
        if ck not in card_rules.GROUP_KINDS and c.get("writeup_slug") and db.blank_document_body(c["writeup_slug"]):
            n = len([r for r in db.list_project_item_rows(c["id"]) if r["post_slug"] != c["writeup_slug"]])
            if n:
                rows.append({"need": NEED_BLANK_WRITEUP_WITH_FILES, "card_slug": c["slug"], "title": c["title"],
                             "detail": f"The write-up is still blank, but the card has {n} file(s).",
                             "suggested": None, "suggested_reason": None, "confidence": None,
                             "decision_id": None, "options": []})
    return rows
