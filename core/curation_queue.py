"""The Curator queue (#519): every question, nudge and need in one list, grouped by card.

Three kinds of item used to live in three places. They are one queue now:

  question  a stored decision the owner must answer (pending_decisions: card_status,
            card_kind, card_built_for, card_family_members, and the v1 file-level
            project_match / retype). Can be deferred, never dismissed.
  nudge     a derived suggestion from the Curator's scoring (core/curator_needs.py).
            Can be deferred or dismissed.
  need      a computed card/hobby need (core/cards.py list_needs_decision: missing
            provenance, blank write-up, untyped link, hobby flags ...). Same as a nudge.

Nothing here stores items: questions are stored by their own table, nudges and needs are
derived every time. The only thing this module's callers persist is the owner's Defer /
Dismiss choice (db.set_curator_state, table curator_dismissals), keyed by `item["key"]`.

Ordering (owner decisions on #519):
  * groups: one per card. Active cards first, then the most recently touched first
    (projects.updated_at); the title breaks ties. After the cards come the hobby groups
    (active hobbies first, then by name), then "Uploads" (file-level questions), then
    "Whole collection" (nudges that belong to no card).
  * inside a group: questions first (lowest confidence first, a question with no
    suggestion counts as lowest), then nudges (the Curator's own effort/impact order),
    then needs (by name).
  * deferred items leave the main list and form a trailing Deferred section, grouped and
    ordered the same way.
"""

import re
import threading
import time
from urllib.parse import quote

from . import actor as actor_ctx, cards, curator_needs, db, decisions, item_title, policy, roles
from .errors import InvalidInput

TYPE_QUESTION = "question"
TYPE_NUDGE = "nudge"
TYPE_NEED = "need"

# The details-panel group (templates/_details_group.html) whose Edit button fixes each kind
# of item. `None` = no single group applies, so Fix just opens the card.
GROUP_FOR_QUESTION = {
    cards.KIND_CARD_STATUS: "status",
    cards.KIND_CARD_KIND: "identity",
    cards.KIND_CARD_FAMILY_MEMBERS: "identity",
    cards.KIND_CARD_BUILT_FOR: "links",
}
GROUP_FOR_NUDGE = {
    "missing_dates": "dates",
    "timeline_gap": "dates",
    "weak_connections": "links",
    "no_highlight": "status",
    "stale_wip": "status",
}
GROUP_FOR_NEED = {
    cards.NEED_MISSING_PROVENANCE: "origin",
    cards.NEED_MISSING_PROVENANCE_CREDIT: "origin",
    cards.NEED_MISSING_WHEREABOUTS: "status",
    cards.NEED_UNTYPED_LINK: "links",
    cards.NEED_STATUS_CONFLICT: "status",
}
EDIT_GROUPS = ("identity", "status", "origin", "dates", "structure", "links")

_CONFIDENCE_RANK = {None: 0, "": 0, "low": 0, "medium": 1, "high": 2}

# Keys the Defer / Dismiss routes accept. Shape-checked rather than looked up so a click
# does not have to rebuild the whole queue.
_KEY_RE = re.compile(r"^(decision:\d{1,12}|[a-z_]{1,40}:(project:\d{1,12}|global)|need:[a-z_]{1,40}:[A-Za-z0-9_.:\-]{1,200})$")


def valid_key(key):
    return bool(key and _KEY_RE.match(key))


def decision_key(decision_id):
    return f"decision:{int(decision_id)}"


def need_key(row):
    link = row.get("link")
    target = f"{link['a']}:{link['b']}" if link else (row.get("card_slug") or row.get("hobby_slug") or "")
    return f"need:{row['need']}:{target}"


def fix_href(slug, group):
    """/project/<slug>?edit=<group>: the card page with that details group already open."""
    base = f"/project/{quote(slug or '', safe='')}"
    return f"{base}?edit={group}" if group in EDIT_GROUPS else base


# --- item builders -----------------------------------------------------------------

def _suggested_block(suggested, options, reason):
    keys = suggested if isinstance(suggested, list) else ([suggested] if suggested else [])
    labels = {o["key"]: o.get("label", o["key"]) for o in options}
    keys = [k for k in keys if k in labels]
    return {"picks": keys, "labels": [labels[k] for k in keys], "reason": reason}


def _card_question(decision_id, kind, payload, slug):
    """One stored card_* decision as a queue item."""
    options = [{"key": o["key"], "label": o.get("label", o["key"]), "reason": o.get("reason")}
               for o in payload.get("options", [])]
    suggested = payload.get("suggested")
    block = _suggested_block(suggested, options, payload.get("suggested_reason"))
    for o in options:
        o["suggested"] = o["key"] in block["picks"]
    group = GROUP_FOR_QUESTION.get(kind)
    return {
        "type": TYPE_QUESTION, "key": decision_key(decision_id), "id": decision_id, "kind": kind,
        "label": payload.get("question") or payload.get("field") or kind,
        "field": payload.get("field"),
        "suggested": block, "confidence": payload.get("confidence"),
        "options": options,
        "multi": isinstance(suggested, list) or kind in (cards.KIND_CARD_BUILT_FOR, cards.KIND_CARD_FAMILY_MEMBERS),
        "answer_param": "choice",
        "group": group, "href": fix_href(slug, group), "fix_label": "Open card",
        "deferred": False, "dismissible": False,
    }


def _file_question(entry):
    """One v1 file-level decision as a queue item, carrying `item_slug` (the file it is about) so
    for_actor can apply the item policy to the shared cached queue."""
    return {**_file_question_item(entry), "item_slug": entry["row"]["slug"]}


def _file_question_item(entry):
    """One v1 file-level decision (project_match / retype) as a queue item."""
    row = entry["row"]
    title = item_title.title_of(row)
    link = f"/object/{quote(row['slug'], safe='')}"
    if entry["kind"] == "project_match":
        options = [{"key": str(c["id"]), "label": c["title"], "reason": None, "suggested": False}
                   for c in entry.get("candidates", [])]
        return {
            "type": TYPE_QUESTION, "key": decision_key(entry["id"]), "id": entry["id"], "kind": "project_match",
            "label": f"Which project does “{title}” belong to?",
            "field": None, "suggested": {"picks": [], "labels": [], "reason": None}, "confidence": None,
            "options": options, "multi": True, "answer_param": "project_ids", "allow_none": True,
            "group": None, "href": link, "fix_label": "Open file", "file_title": title,
            "deferred": False, "dismissible": False,
        }
    if entry["kind"] == "item_supersedes":
        # #477: "does this new file replace an earlier one?" -- pick a candidate or "No, it's separate".
        options = [{"key": o["key"], "label": o.get("label", o["key"]), "reason": None,
                    "suggested": o["key"] == entry.get("suggested")} for o in entry.get("options", [])]
        picks = [o["key"] for o in options if o["suggested"]]
        return {
            "type": TYPE_QUESTION, "key": decision_key(entry["id"]), "id": entry["id"], "kind": "item_supersedes",
            "label": entry.get("question") or f"Does “{title}” replace an earlier file?",
            "field": None,
            "suggested": {"picks": picks, "labels": [o["label"] for o in options if o["suggested"]],
                          "reason": entry.get("suggested_reason")},
            "confidence": entry.get("confidence"),
            "options": options, "multi": False, "answer_param": "choice",
            "group": None, "href": link, "fix_label": "Open file", "file_title": title,
            "deferred": False, "dismissible": False,
        }
    current = entry.get("current_type")
    options = [{"key": o["key"], "label": o.get("label", o["key"]), "reason": None,
                "suggested": bool(o.get("suggested"))} for o in entry.get("options", [])]
    keys = [o["key"] for o in options if o["suggested"]]
    labels = [o["label"] for o in options if o["suggested"]]
    return {
        "type": TYPE_QUESTION, "key": decision_key(entry["id"]), "id": entry["id"], "kind": entry["kind"],
        "label": entry.get("question") or f"What type of file is “{title}”?",
        "field": None, "suggested": {"picks": keys, "labels": labels, "reason": None}, "confidence": None,
        "current": current, "options": options, "multi": False, "answer_param": "choice",
        "group": None, "href": link, "fix_label": "Open file", "file_title": title,
        "deferred": False, "dismissible": False,
    }


def _nudge_item(n):
    group = GROUP_FOR_NUDGE.get(n["kind"])
    slug = n.get("target_slug")
    if n["target_type"] == "project":
        href = fix_href(slug, group)
    elif n["kind"] == "unfiled_objects":
        href = "/unfiled"
    elif n["kind"] == "confirm_caption":
        href = "/captions/review"
    else:
        href = "/"
    return {
        "type": TYPE_NUDGE, "key": n["nudge_key"], "id": None, "kind": n["kind"],
        "label": n["title"], "detail": n.get("summary") or "",
        "priority": n.get("priority"),
        "group": group, "href": href, "fix_label": "Fix" if group else "Open",
        "deferred": bool(n.get("deferred")), "dismissible": True,
    }


def _need_item(r):
    slug = r.get("card_slug")
    group = GROUP_FOR_NEED.get(r["need"])
    if slug:
        href = fix_href(slug, group)
    else:
        # A hobby need: the hobby page opens its STATUS group (the Active/Inactive switch).
        group = "status"
        href = f"/hobby/{quote(r.get('hobby_slug') or '', safe='')}?edit=status"
    suggestion = None
    if r.get("suggested"):
        opts = {o["key"]: o.get("label", o["key"]) for o in r.get("options", [])}
        s = r["suggested"]
        suggestion = {"picks": [s], "labels": [opts.get(s, s)], "reason": r.get("suggested_reason")}
    return {
        "type": TYPE_NEED, "key": need_key(r), "id": None, "kind": r["need"],
        "label": r.get("detail") or r["need"].replace("_", " "), "detail": "",
        "suggested": suggestion, "confidence": r.get("confidence"),
        "group": group, "href": href, "fix_label": "Fix" if (group or not slug) else "Open card",
        "deferred": False, "dismissible": True,
    }


def _item_sort(item):
    if item["type"] == TYPE_QUESTION:
        return (0, _CONFIDENCE_RANK.get(item.get("confidence"), 0), item["id"] or 0, "")
    if item["type"] == TYPE_NUDGE:
        return (1, 0, 0, "")  # stable sort keeps curator_needs' own order
    return (2, 0, 0, item["kind"])


# --- assembling --------------------------------------------------------------------

def _card_group(card):
    return {
        "id": f"card:{card['slug']}", "type": "card", "slug": card["slug"], "title": card["title"],
        "href": f"/project/{quote(card['slug'], safe='')}",
        "card_kind": card.get("kind") or "project", "activity": card.get("activity"),
        "last_touched": card.get("updated_at") or card.get("created_at") or 0,
        "items": [],
    }


def _hobby_group(slug):
    h = db.get_hobby(slug) or {}
    return {
        "id": f"hobby:{slug}", "type": "hobby", "slug": slug, "title": h.get("name") or slug,
        "href": f"/hobby/{quote(slug, safe='')}", "card_kind": "hobby",
        "activity": h.get("hobby_status") or "active", "last_touched": 0, "items": [],
    }


def _plain_group(gid, gtype, title, href):
    return {"id": gid, "type": gtype, "slug": None, "title": title, "href": href,
            "card_kind": None, "activity": None, "last_touched": 0, "items": []}


def _group_order(g):
    """Sort key for the groups: cards, then hobbies, then uploads, then collection."""
    if g["type"] == "card":
        return (0, 0 if g["activity"] == "active" else 1, -float(g["last_touched"] or 0), g["title"].lower())
    if g["type"] == "hobby":
        return (1, 0 if g["activity"] == "active" else 1, 0, g["title"].lower())
    if g["type"] == "uploads":
        return (2, 0, 0, "")
    return (3, 0, 0, "")


def _arrange(items_by_group, groups):
    out = []
    for gid, items in items_by_group.items():
        if not items:
            continue
        g = dict(groups[gid])
        g["items"] = sorted(items, key=_item_sort)
        out.append(g)
    out.sort(key=_group_order)
    return out


# Overlapping items (#524). Where the Curator's scoring (a nudge) and a computed need say the
# same thing about the same card, the queue shows ONE item, not two:
#   nudge `missing_writeup`  +  need `blank_writeup_with_files`
# The nudge is kept (it already has the owner's Defer / Dismiss history under its key, and its
# place in the Curator's effort order); it takes on the need's more specific wording (how many
# files the card has). Defer or Dismiss on either of the two applies to the merged item, so an
# answer to one is never undone by the other reappearing.
def _dedupe_keys(card):
    """(nudge key, need key) of the overlapping pair for one card."""
    return (f"missing_writeup:project:{card['id']}",
            f"need:{cards.NEED_BLANK_WRITEUP_WITH_FILES}:{card['slug']}")


@db.in_read_session
def build_queue(card=None, hobby=None):
    """The unified queue. `card` (slug) limits it to that one card's slice (the project
    page's strip); `hobby` (slug) limits it to that hobby's own needs (the hobby page's
    strip: the two computed hobby flags). Returns {"groups": [...open...], "deferred": [...same shape...],
    "counts": {"open", "deferred", "questions", "nudges", "needs"}}. `counts.open` is what the
    Curator tab's badge shows: open, non-deferred items."""
    states = db.list_curator_states()
    dismissed = {k for k, v in states.items() if v == db.CURATOR_DISMISS}
    deferred_keys = {k for k, v in states.items() if v == db.CURATOR_DEFER}

    groups = {}      # group id -> group dict (no items)
    items = []       # (group id, item)

    def put(gid, group, item):
        groups.setdefault(gid, group)
        item["deferred"] = item["key"] in deferred_keys
        items.append((gid, item))

    card_row = db.get_project(card) if card else None
    if card and card_row is None:
        return {"groups": [], "deferred": [], "counts": _counts([], [])}
    hobby_row = db.get_hobby(hobby) if hobby else None
    if hobby and hobby_row is None:
        return {"groups": [], "deferred": [], "counts": _counts([], [])}

    # questions
    if hobby_row:
        pass  # card questions belong to cards, not to the hobby's own slice
    elif card_row:
        for d in cards.open_card_decisions(card_row["slug"]):
            put(f"card:{card_row['slug']}", _card_group(card_row),
                _card_question(d["id"], d["kind"], d["payload"], card_row["slug"]))
    else:
        for e in decisions.list_open():
            if cards.is_card_decision_slug(e["post_slug"]):
                c = e["card"]
                put(f"card:{c['slug']}", _card_group(c), _card_question(e["id"], e["kind"], e["payload"], c["slug"]))
            else:
                put("uploads", _plain_group("uploads", "uploads", "Uploads waiting for you", "/"), _file_question(e))

    # nudges
    if hobby_row:
        nudges = []
    elif card_row:
        nudges = curator_needs.sort_nudges(curator_needs.project_nudges(card_row, db.list_active_curator_dismissals()))
        nudges = [dict(n, deferred=n["nudge_key"] in deferred_keys) for n in nudges]
    else:
        nudges = curator_needs.list_needs()
    merged_nudges = {}  # nudge key -> its queue item, for the overlap dedupe below
    for n in nudges:
        if n["kind"] == "confirm_automatch":
            continue  # the project_match questions themselves are in the queue
        if n["target_type"] == "project":
            c = card_row or db.get_project(n["target_id"])
            if c is None:
                continue
            nkey, need_k = _dedupe_keys(c)
            if n["nudge_key"] == nkey and need_k in dismissed:
                continue  # the owner dismissed this same gap under its other name
            item = _nudge_item(n)
            put(f"card:{c['slug']}", _card_group(c), item)
            if n["nudge_key"] == nkey:
                merged_nudges[nkey] = item
                if need_k in deferred_keys:
                    item["deferred"] = True  # deferred under its other name
        elif not card_row:
            put("collection", _plain_group("collection", "collection", "Whole collection", "/"), _nudge_item(n))

    # needs (computed; stored questions are excluded, they are already above)
    if hobby_row:
        need_rows = [r for r in cards.list_needs_decision(kind="hobby", hobby=hobby_row["slug"])
                     if r.get("hobby_slug") == hobby_row["slug"]]
    else:
        need_rows = cards.list_needs_decision(card=card_row["slug"] if card_row else None)
    for r in need_rows:
        if r.get("decision_id") is not None:
            continue
        item = _need_item(r)
        if item["key"] in dismissed:
            continue
        if r.get("card_slug"):
            c = card_row or db.get_project(r["card_slug"])
            if c is None:
                continue
            if r["need"] == cards.NEED_BLANK_WRITEUP_WITH_FILES:
                nkey = _dedupe_keys(c)[0]
                if nkey in merged_nudges:
                    merged_nudges[nkey]["detail"] = item["label"]  # one item, with the file count
                    continue
                if nkey in dismissed:
                    continue
            put(f"card:{c['slug']}", _card_group(c), item)
        elif hobby_row or not card_row:
            hs = r.get("hobby_slug")
            put(f"hobby:{hs}", _hobby_group(hs), item)

    open_by, deferred_by = {}, {}
    for gid, item in items:
        (deferred_by if item["deferred"] else open_by).setdefault(gid, []).append(item)
    open_groups = _arrange(open_by, groups)
    deferred_groups = _arrange(deferred_by, groups)
    return {"groups": open_groups, "deferred": deferred_groups, "counts": _counts(open_groups, deferred_groups)}


def _counts(open_groups, deferred_groups):
    flat = [i for g in open_groups for i in g["items"]]
    return {
        "open": len(flat),
        "deferred": sum(len(g["items"]) for g in deferred_groups),
        "questions": sum(1 for i in flat if i["type"] == TYPE_QUESTION),
        "nudges": sum(1 for i in flat if i["type"] == TYPE_NUDGE),
        "needs": sum(1 for i in flat if i["type"] == TYPE_NEED),
    }


# --- the cached whole queue (#524) -------------------------------------------------
# Every page load asks for the badge count, and the full queue costs a lot to derive. So the
# unfiltered queue is built once and kept while NOTHING has been written to the database
# (db.change_fingerprint: file mtime/size of the DB and its WAL, so a write from the web
# process, the MCP sidecar or a script all count) and for at most CACHE_TTL seconds (the
# time-based nudges, e.g. a WIP going stale after 90 days, never wait long). The badge, the
# summary, the fragment's group headers and the JSON all read this one build, so the badge
# count is by construction the full queue's open count.
CACHE_TTL = 120.0
_cache = {"fp": None, "at": 0.0, "q": None}
_cache_lock = threading.Lock()   # guards the dict
_build_lock = threading.Lock()   # one build at a time: concurrent callers wait, then reuse it


def _cached():
    fp = db.change_fingerprint()
    with _cache_lock:
        if _cache["q"] is not None and fp is not None and _cache["fp"] == fp and time.time() - _cache["at"] < CACHE_TTL:
            return _cache["q"]
    return None


def _full_queue():
    """The WHOLE queue (every question, whoever asks), from the cache when nothing has been written
    since it was built. Built as `system` so the shared cache never depends on who happened to ask
    first; callers get `cached_queue()`, which applies the item policy for the actor asking."""
    hit = _cached()
    if hit is not None:
        return hit
    with _build_lock:
        hit = _cached()          # another caller may have built it while we waited
        if hit is not None:
            return hit
        fp = db.change_fingerprint()   # taken BEFORE the build: a write during it invalidates it
        with actor_ctx.acting_as(actor_ctx.ACTOR_SYSTEM):
            q = build_queue()
        with _cache_lock:
            _cache.update(fp=fp, at=time.time(), q=q)
        return q


def for_actor(q, actor=None):
    """The queue `q` as `actor` (default: the current context) may see it (#603, #467): a file
    question about an item the item policy hides (a sensitive item that isn't theirs) is left out,
    and the counts follow. An admin sees it unchanged. Returns `q` itself when nothing is hidden."""
    if roles.at_least(roles.role_of(actor_ctx.resolve(actor)), roles.ADMIN):
        return q
    hidden = set()
    for g in q["groups"] + q["deferred"]:
        for i in g["items"]:
            slug = i.get("item_slug")
            if slug and slug not in hidden and not policy.can_view(db.get_by_slug(slug), actor):
                hidden.add(slug)
    if not hidden:
        return q

    def keep(groups):
        out = []
        for g in groups:
            its = [i for i in g["items"] if i.get("item_slug") not in hidden]
            if its:
                out.append({**g, "items": its})
        return out
    open_groups, deferred_groups = keep(q["groups"]), keep(q["deferred"])
    return {**q, "groups": open_groups, "deferred": deferred_groups, "counts": _counts(open_groups, deferred_groups)}


def cached_queue():
    """The queue for the actor asking (for_actor over the shared, cached whole queue). Treat the
    result as read-only: it may be the shared object."""
    return for_actor(_full_queue())


def group_items(gid, section="open"):
    """One group's items (for expanding a collapsed group). `gid` is a group id from the queue
    (card:<slug>, hobby:<slug>, uploads, collection); `section` is "open" or "deferred".
    Returns (group dict or None, items). A card or hobby builds only its own slice, so
    expanding a group never rebuilds the whole queue."""
    key = "deferred" if section == "deferred" else "groups"
    if gid.startswith("card:"):
        q = build_queue(card=gid[5:])
    elif gid.startswith("hobby:"):
        q = build_queue(hobby=gid[6:])
    else:
        q = cached_queue()
    for g in q[key]:
        if g["id"] == gid:
            return g, g["items"]
    return None, []


# --- actions -----------------------------------------------------------------------

class QueueError(InvalidInput):
    """A Defer / Bring back / Dismiss the queue refuses (carries a message for the caller).
    #548: an AppError, 400 `bad_request` (the code the MCP tools already used)."""
    default_code = "bad_request"


def _slugs_for_key(key):
    if key.startswith("need:"):
        parts = key.split(":")
        return [parts[2]] if len(parts) > 2 and parts[2] else []
    return []


def defer(key, actor=None):
    """Move an item to the Deferred section. No timer; it stays until answered or brought back."""
    if not valid_key(key):
        raise QueueError(f"Not a queue item key: {key!r}")
    return {"ok": True, "changed": db.set_curator_state(key, db.CURATOR_DEFER, actor=actor,
                                                         affected_slugs=_slugs_for_key(key))}


def bring_back(key, actor=None):
    """Return a deferred item to the main queue."""
    if not valid_key(key):
        raise QueueError(f"Not a queue item key: {key!r}")
    return {"ok": True, "changed": db.clear_curator_defer(key, actor=actor, affected_slugs=_slugs_for_key(key))}


def dismiss(key, actor=None):
    """Dismiss a nudge or need for good. A question (decision:<id>) can only be answered or deferred."""
    if not valid_key(key):
        raise QueueError(f"Not a queue item key: {key!r}")
    if key.startswith("decision:"):
        raise QueueError("A question can't be dismissed. Answer it, or defer it.")
    return {"ok": True, "changed": db.set_curator_state(key, db.CURATOR_DISMISS, actor=actor,
                                                         affected_slugs=_slugs_for_key(key))}
