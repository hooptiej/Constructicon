"""V2 card migrations (docs/design/v2-cards.md section 4).

Piece 1: `v2c_1` -- maps v1 `projects.status` onto kind / activity / stage /
stop_reason (4.2), and queues owner decisions for the judgment calls.

Run from core.db.init_db(). Idempotent by construction, no marker needed:
  * a card is migrated only while its `stage IS NULL`, and cards created after
    the migration always get a stage, so a second run finds nothing to do;
  * decisions are queued with db.queue_decision_once (skips any existing row,
    open OR resolved), so an answered question is never re-asked.

Two phases so the slow, read-only heuristics don't hold a write lock: (1) plan,
reading through the normal db helpers; (2) one write transaction that applies the
plan and logs every write as an actor='migration' change-log row (not undoable --
restore from a snapshot instead, 3.13).

Provisional values (4.1): a card with a queued judgment call is never left blank;
it gets the LEAST-CLAIMING valid value now, the payload records exactly what was
applied, and the suggestion is separate and may differ.
"""

import re

from . import card_rules, changes, db, timeline

SCHEMA_VERSION = 1

_OWNED_WORDS = re.compile(r"\b(own|owned|bought|using|use|carry|daily)\b", re.I)
_SOFTWARE_WORDS = re.compile(
    r"\b(code|script|software|firmware|app|site|system|design|tool|lua|library)\b", re.I)
_STALE_SECONDS = 2 * 365 * 24 * 3600

STATUS_OPTION_LABELS = {
    "done": "Done",
    "in_use": "In use",
    "paused": "Paused",
    "collection": "Collection (a set kept together)",
    "in_progress": "In progress",
    "idea": "Idea",
    "stopped_failed": "Stopped - failed",
    "stopped_abandoned": "Stopped - abandoned",
}


def _set_status_patch(stage, stop_reason=None):
    p = {"op": "set_status", "stage": stage}
    if stop_reason:
        p["stop_reason"] = stop_reason
    return p


def _envelope(card, field, question, legacy_status, provisional, suggested, reason, confidence, options):
    return {
        "schema": SCHEMA_VERSION,
        "card_id": card["id"],
        "card_slug": card["slug"],
        "title": card["title"],
        "field": field,
        "question": question,
        "legacy_status": legacy_status,
        "provisional": provisional,
        "suggested": suggested,
        "suggested_reason": reason,
        "confidence": confidence,
        "options": options,
    }


# --- Suggestions (deterministic, never applied) ------------------------------------

def suggest_status_for_complete(card, facts):
    """complete / archived: done vs in use (spec 4.3)."""
    signals = []
    if facts["owned_files"]:
        signals.append(f"{facts['owned_files']} file(s) marked found/collected")
    if _OWNED_WORDS.search(f"{card['title']} {card.get('description') or ''}"):
        signals.append("title/description mentions owning or using it")
    if any("collect" in h.lower() for h in facts["hobby_names"]):
        signals.append("sits in a collecting hobby")
    if signals:
        return "in_use", "; ".join(signals), "medium" if len(signals) > 1 else "low"
    return "done", "No owned-object signals found", "low"


def suggest_status_for_shelved(card, facts):
    """shelved: paused build vs a collection (spec 4.3). Always low confidence."""
    newest = facts["newest_file_date"]
    if (newest is not None and (facts["now"] - newest) > _STALE_SECONDS and not facts["children_in_progress"]):
        return "collection", "Newest file is over 2 years old and nothing nested is in progress", "low"
    return "paused", "Recent activity or work in progress nested under it", "low"


def suggest_kind(card, facts):
    """card_kind: Thing for a leaf with no software/system words, else Project."""
    if _SOFTWARE_WORDS.search(f"{card['title']} {card.get('description') or ''}"):
        return "project", "Title/description reads like software, a system or a design", "low"
    return "thing", "Leaf card with no software/system words (reads like one physical object)", "low"


def rank_built_for_candidates(card, facts, all_cards):
    """means-to-an-end: which card was this built for. Ranked list of
    {slug, title, reason}: related-link neighbours, then shared hobby, then cards
    whose title appears in this card's description/write-up."""
    ranked, seen = [], {card["slug"]}

    def add(c, reason):
        if c["slug"] not in seen:
            seen.add(c["slug"])
            ranked.append({"slug": c["slug"], "title": c["title"], "reason": reason})

    for n in facts["related"]:
        add(n, "linked as related")
    for c in all_cards:
        if c["id"] != card["id"] and facts["hobby_ids"] & facts["hobby_ids_by_card"].get(c["id"], set()):
            add(c, "shares a hobby")
    haystack = f"{card.get('description') or ''} {facts['writeup_body']}".lower()
    for c in all_cards:
        if c["id"] != card["id"] and len(c["title"]) >= 4 and c["title"].lower() in haystack:
            add(c, "named in its description or write-up")
    return ranked[:8]


# --- Planning -----------------------------------------------------------------------

def _gather(card, all_cards, hobby_ids_by_card, children_by_parent, now):
    items = db.list_project_items(card["id"])
    writeup = card.get("writeup_slug")
    content = [i for i in items if i.get("slug") != writeup]
    dates = [timeline.resolve_item_date(i) for i in content]
    hobbies = db.list_hobbies_for_project(card["id"])
    writeup_body = ""
    if writeup:
        w = db.get_by_slug(writeup)
        if w:
            writeup_body = (w.get("type_metadata") or {}).get("body", "") or ""
    return {
        "now": now,
        "owned_files": sum(1 for i in content if i.get("provenance") in ("found", "collected")),
        "hobby_names": [h["name"] for h in hobbies],
        "hobby_ids": {h["id"] for h in hobbies},
        "hobby_ids_by_card": hobby_ids_by_card,
        "newest_file_date": max(dates) if dates else None,
        "children_in_progress": any(
            c.get("legacy_status_for_plan") in ("wip", "active")
            for c in children_by_parent.get(card["id"], [])),
        "related": db.list_related_projects(card["slug"]),
        "writeup_body": writeup_body,
    }


def build_plan(now=None):
    """Read-only. Returns a list of per-card plan entries for every card whose
    `stage IS NULL`:
        {card, target: {kind, activity, stage, stop_reason}, decisions: [(kind, payload)]}
    """
    import time as _time
    now = now if now is not None else _time.time()
    all_cards = db.list_projects()
    todo = [c for c in all_cards if not c.get("stage")]
    if not todo:
        return []
    children_by_parent = {}
    for c in all_cards:
        c["legacy_status_for_plan"] = c.get("status")
        if c.get("parent_id") is not None:
            children_by_parent.setdefault(c["parent_id"], []).append(c)
    hobby_ids_by_card = {c["id"]: {h["id"] for h in db.list_hobbies_for_project(c["id"])} for c in all_cards}

    plan = []
    for card in todo:
        legacy = card.get("status")
        tgt = card_rules.migration_target(legacy)
        target = {
            "kind": tgt["kind"],
            "activity": card_rules.ACTIVITY_OF[tgt["stage"]],
            "stage": tgt["stage"],
            "stop_reason": tgt["stop_reason"],
        }
        facts = _gather(card, all_cards, hobby_ids_by_card, children_by_parent, now)
        decisions = []
        provisional = _set_status_patch(tgt["stage"], tgt["stop_reason"])

        if tgt["question"] == "card_status":
            if legacy == "shelved":
                sug, why, conf = suggest_status_for_shelved(card, facts)
                question = "This was shelved. Is it a paused build, or a collection (a set kept together)?"
                options = [
                    {"key": "paused", "label": STATUS_OPTION_LABELS["paused"], "patch": [_set_status_patch("paused")]},
                    {"key": "collection", "label": STATUS_OPTION_LABELS["collection"],
                     "patch": [{"op": "set_kind", "kind": "collection"}, _set_status_patch("in_use")]},
                ]
            elif legacy in ("complete", "archived"):
                sug, why, conf = suggest_status_for_complete(card, facts)
                question = "Is this finished and still in use, or finished and done?"
                options = [
                    {"key": "in_use", "label": STATUS_OPTION_LABELS["in_use"], "patch": [_set_status_patch("in_use")]},
                    {"key": "done", "label": STATUS_OPTION_LABELS["done"], "patch": [_set_status_patch("done")]},
                ]
            else:
                sug, why, conf = "paused", f"Unrecognised legacy status {legacy!r}", "low"
                question = f"This card's old status was {legacy!r}, which doesn't map to anything. Pick a stage."
                options = [
                    {"key": "in_progress", "label": STATUS_OPTION_LABELS["in_progress"], "patch": [_set_status_patch("in_progress")]},
                    {"key": "in_use", "label": STATUS_OPTION_LABELS["in_use"], "patch": [_set_status_patch("in_use")]},
                    {"key": "idea", "label": STATUS_OPTION_LABELS["idea"], "patch": [_set_status_patch("idea")]},
                    {"key": "paused", "label": STATUS_OPTION_LABELS["paused"], "patch": [_set_status_patch("paused")]},
                    {"key": "done", "label": STATUS_OPTION_LABELS["done"], "patch": [_set_status_patch("done")]},
                    {"key": "stopped_failed", "label": STATUS_OPTION_LABELS["stopped_failed"],
                     "patch": [_set_status_patch("stopped", "failed")]},
                    {"key": "stopped_abandoned", "label": STATUS_OPTION_LABELS["stopped_abandoned"],
                     "patch": [_set_status_patch("stopped", "abandoned")]},
                ]
            decisions.append(("card_status", _envelope(
                card, "stage", question, legacy, provisional, sug, why, conf, options)))

        elif tgt["question"] == "card_built_for":
            cands = rank_built_for_candidates(card, facts, all_cards)
            options = [
                {"key": c["slug"], "label": c["title"], "reason": c["reason"],
                 "patch": [{"op": "link", "a": card["slug"], "b": c["slug"], "type": "built_for"}]}
                for c in cands
            ]
            options.append({"key": "is_event", "label": "It's an event, not a build",
                            "patch": [{"op": "set_kind", "kind": "event"}]})
            options.append({"key": "none", "label": "None of these", "patch": []})
            if cands:
                sug, why = cands[0]["slug"], f"Top candidate: {cands[0]['reason']}"
            else:
                sug, why = "none", "No candidate cards found"
            decisions.append(("card_built_for", _envelope(
                card, "built_for",
                "This was a means to an end. Which card was it built for (or is it really an event)?",
                legacy, provisional, sug, why, "low", options)))

        # card_kind: one decision per LEAF card that is still a plain project
        # (4.3). Non-leaf cards are containers and stay 'project' silently.
        is_leaf = not children_by_parent.get(card["id"])
        if is_leaf and target["kind"] == "project":
            sug, why, conf = suggest_kind(card, facts)
            options = [{"key": k, "label": card_rules.KIND_LABELS[k], "patch": [{"op": "set_kind", "kind": k}]}
                       for k in card_rules.KINDS]
            decisions.append(("card_kind", _envelope(
                card, "kind", "Is this one physical object (a Thing) or a Project?",
                legacy, {"op": "set_kind", "kind": "project"}, sug, why, conf, options)))

        plan.append({"card": card, "target": target, "decisions": decisions})
    return plan


# --- v2c_2: hobbies (4.6) ------------------------------------------------------------

def run_v2c_2():
    """Hobby activity + group codes (spec 3.3, 3.9, 4.6). Automatic, no decisions.

    * hobby_status 'dormant' / 'abandoned' (or any other non-two-value word) ->
      'inactive'; NULL / blank -> 'active'. A hobby already on active/inactive is
      untouched, which is the idempotency guard.
    * group_code derived for every is_hobby=1 row where it is NULL, unique across
      hobbies (card_rules.derive_group_code), in id order so re-runs are stable.

    The v1 value is kept in the actor='migration' change-log row (not a column), so
    the log shows exactly what changed. Nothing is flipped on the owner's behalf
    beyond the vocabulary mapping: 'definitely not FPV'ing anymore' is a manual
    switch, and the computed flags (db.hobby_flags) point at the obvious mismatches.
    Returns {"hobbies_changed": n, "batch_id": ...}.
    """
    conn = db.get_conn()
    changed = 0
    batch_id = changes.new_batch_id()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, slug, name, hobby_status, group_code FROM blog_tags WHERE is_hobby = 1 ORDER BY id"
        ).fetchall()
        taken = [r["group_code"] for r in rows if r["group_code"]]
        for r in rows:
            old_status, old_code = r["hobby_status"], r["group_code"]
            new_status = old_status
            if old_status not in card_rules.HOBBY_ACTIVITIES:
                new_status = "active" if old_status in (None, "") else "inactive"
            new_code = old_code
            if not old_code:
                new_code = card_rules.derive_group_code(r["name"], taken)
                taken.append(new_code)
            if (new_status, new_code) == (old_status, old_code):
                continue
            conn.execute("UPDATE blog_tags SET hobby_status = ?, group_code = ? WHERE id = ?",
                         (new_status, new_code, r["id"]))
            db.insert_change_log(
                conn, "migration_v2c_2", changes.ACTOR_MIGRATION,
                [changes.row_image("blog_tags", {"id": r["id"]},
                                   {"hobby_status": old_status, "group_code": old_code},
                                   {"hobby_status": new_status, "group_code": new_code})],
                batch_id=batch_id, affected_slugs=[r["slug"]])
            changed += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"hobbies_changed": changed, "batch_id": batch_id}


# --- Applying -----------------------------------------------------------------------

def run_v2c_1():
    """Plans (read-only), then applies in one write transaction. Returns a small
    summary dict; {"migrated": 0, ...} on a re-run."""
    plan = build_plan()
    if not plan:
        return {"migrated": 0, "decisions_queued": 0}
    batch_id = changes.new_batch_id()
    queued = 0
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for entry in plan:
            card, target = entry["card"], entry["target"]
            # Re-check under the write lock (a concurrent init_db could have won).
            cur = conn.execute(
                "UPDATE projects SET kind = ?, activity = ?, stage = ?, stop_reason = ? WHERE id = ? AND stage IS NULL",
                (target["kind"], target["activity"], target["stage"], target["stop_reason"], card["id"]),
            )
            if cur.rowcount == 0:
                continue
            mutations = [changes.row_image(
                "projects", {"id": card["id"]},
                {"kind": card.get("kind"), "activity": None, "stage": None, "stop_reason": None,
                 "legacy_status": card.get("status")},
                target)]
            for kind, payload in entry["decisions"]:
                did = db.queue_decision_once(kind, f"card:{card['slug']}", payload, conn=conn)
                if did is not None:
                    queued += 1
                    mutations.append(changes.row_image(
                        "pending_decisions", {"id": did}, None,
                        {"kind": kind, "post_slug": f"card:{card['slug']}"}))
            db.insert_change_log(conn, "migration_v2c_1", changes.ACTOR_MIGRATION, mutations,
                                 batch_id=batch_id, affected_slugs=[card["slug"]])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"migrated": len(plan), "decisions_queued": queued, "batch_id": batch_id}
