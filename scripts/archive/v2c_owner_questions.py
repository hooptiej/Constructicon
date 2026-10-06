"""Archived one-time migrations about the OWNER'S OWN cards (#562; were core/db.py MIGRATIONS
`v2c_3_alienwhoop_family` and `v2c_4_canopy_built_for`, V2 cards spec 4.5, pieces 3 and 4).

They queue two owner questions keyed by card title, which only make sense on the owner's archive:
  v2c_3  the "AlienWhoop" (or "AlienWhoop and TinyWhoop") card holds separate builds by nesting:
         is it a family, and which cards belong in it? (card_family_members)
  v2c_4  which card was the "AW canopy..." card built for? (card_built_for; suggests "The Queen")

Both already ran (and are recorded in schema_migrations) on the owner's prod and test installs, so
taking them out of init_db changed nothing there; a fresh customer install never runs them, and a
new card titled "AlienWhoop" no longer gets a question queued at the next restart.

Kept runnable for the record. Run-once: a step whose name is already in schema_migrations is
skipped (pass --force to run it anyway; each step is idempotent on its own: queue_decision_once
never re-asks a question that exists, open or resolved). Dry run by default.

    python3 scripts/archive/v2c_owner_questions.py              # plan only
    python3 scripts/archive/v2c_owner_questions.py --execute    # queue the questions
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from core import card_rules, cards, changes, db  # noqa: E402
from core.card_migration import _envelope, _gather, rank_built_for_candidates  # noqa: E402

# --- v2c_3_alienwhoop: the fake family (4.5) ---------------------------------------------

# The family's own card. The spec says a card titled exactly "AlienWhoop"; the real
# archive calls it "AlienWhoop and TinyWhoop" (the owner: "that's really a family"),
# so both titles qualify. Matched by title, never by id.
FAMILY_CARD_TITLES = ("alienwhoop", "alienwhoop and tinywhoop")
# Named in #428 as candidates the owner should confirm; only offered if they exist.
FAMILY_SIBLING_TITLES = ("tinywhoop", "alienwhoop v2 f4", "alienwhoop zer0")


def plan_alienwhoop_family(all_cards=None):
    """Read-only. Returns None, or (family_card, payload) for the one
    card_family_members decision that should exist for the AlienWhoop family."""
    all_cards = all_cards if all_cards is not None else db.list_projects()
    fam = None
    for want in FAMILY_CARD_TITLES:  # prefer the exact "AlienWhoop" title
        fam = next((c for c in all_cards if (c["title"] or "").strip().lower() == want), None)
        if fam:
            break
    if fam is None:
        return None
    member_ids = {m["id"] for m in db.list_family_members(fam["id"])}
    seen = set()
    candidates = []

    def eligible(c):
        return (c["id"] != fam["id"] and c["id"] not in member_ids and c["id"] not in seen
                and (c.get("kind") or "project") not in card_rules.GROUP_KINDS)

    for c in sorted(db.list_child_projects(fam["id"]), key=lambda c: (c["title"] or "").lower()):
        if eligible(c):
            seen.add(c["id"])
            candidates.append((c, "currently nested under it", "medium", True))
    for c in all_cards:
        if (c["title"] or "").strip().lower() in FAMILY_SIBLING_TITLES and eligible(c):
            seen.add(c["id"])
            candidates.append((c, "named like a sibling; the archive suggests separate builds", "low", True))
    if not candidates:
        return None
    options = []
    for c, reason, conf, yes in candidates:
        patch = []
        if c.get("parent_id") == fam["id"]:
            patch.append({"op": "unnest", "card": c["slug"]})
        patch.append({"op": "add_to_family", "family": fam["slug"], "member": c["slug"]})
        options.append({"key": c["slug"], "label": c["title"], "reason": reason, "confidence": conf,
                        "suggested": yes, "patch": patch})
    options.append({"key": "none", "label": "None of these (leave it as it is)", "patch": []})
    nested = [c for c, _r, _cf, _y in candidates if c.get("parent_id") == fam["id"]]
    payload = _envelope(
        fam, "family_members",
        f"'{fam['title']}' holds separate builds by nesting them. Is it really a family, and which of these "
        "belong in it? (Members stay their own cards; the ones nested under it are taken out of the nesting.)",
        fam.get("status"), None,
        [c["slug"] for c, _r, _cf, yes in candidates if yes],
        "Currently nested children, plus any card named like a sibling"
        if len(candidates) > len(nested) else "Currently nested children",
        "medium" if len(candidates) == len(nested) else "low", options)
    return fam, payload


# --- v2c_4: the canopy's built_for question (4.5, deferred from piece 3) --------------------

# The card the spec names ("AW canopy*"), and the quad it was almost certainly built
# for. Matched by TITLE, never by id; the Queen is only ever a suggested candidate.
CANOPY_TITLE_PREFIX = "aw canopy"
CANOPY_LIKELY_TARGET_TITLES = ("the queen",)  # substring of the target's title


def plan_canopy_built_for(all_cards=None):
    """Read-only. Returns None, or (canopy_card, payload) for the one
    card_built_for decision the 'AW canopy' card should have."""
    all_cards = all_cards if all_cards is not None else db.list_projects()
    canopy = next((c for c in all_cards if (c["title"] or "").strip().lower().startswith(CANOPY_TITLE_PREFIX)), None)
    if canopy is None:
        return None
    children_by_parent = {}
    for c in all_cards:
        c["legacy_status_for_plan"] = c.get("status")
        if c.get("parent_id") is not None:
            children_by_parent.setdefault(c["parent_id"], []).append(c)
    hobby_ids_by_card = {c["id"]: {h["id"] for h in db.list_hobbies_for_project(c["id"])} for c in all_cards}
    facts = _gather(canopy, all_cards, hobby_ids_by_card, children_by_parent, time.time())
    cands, seen = [], {canopy["slug"]}
    for c in all_cards:  # the quad the spec names goes first
        t = (c["title"] or "").lower()
        if c["slug"] not in seen and any(w in t for w in CANOPY_LIKELY_TARGET_TITLES) and "alienwhoop" in t:
            seen.add(c["slug"])
            cands.append({"slug": c["slug"], "title": c["title"],
                          "reason": "named in the V2 spec as what the canopy was built for"})
    for c in rank_built_for_candidates(canopy, facts, all_cards):
        if c["slug"] not in seen:
            seen.add(c["slug"])
            cands.append(c)
    cands = cands[:8]
    options = [{"key": c["slug"], "label": c["title"], "reason": c["reason"],
                "patch": [{"op": "link", "a": canopy["slug"], "b": c["slug"], "type": "built_for"}]} for c in cands]
    options.append({"key": "none", "label": "None of these", "patch": []})
    if cands:
        sug, why = cands[0]["slug"], f"Top candidate: {cands[0]['reason']}"
    else:
        sug, why = "none", "No candidate cards found"
    payload = _envelope(
        canopy, "built_for", "Which card was this canopy built for? (Pick every quad it fits.)",
        canopy.get("status"), None, sug, why, "medium" if cands and "spec" in cands[0]["reason"] else "low", options)
    return canopy, payload


# --- running -------------------------------------------------------------------------------

STEPS = [
    # (schema_migrations name, planner, decision kind, change-log op)
    ("v2c_3_alienwhoop_family", plan_alienwhoop_family, cards.KIND_CARD_FAMILY_MEMBERS, "migration_v2c_3_alienwhoop"),
    ("v2c_4_canopy_built_for", plan_canopy_built_for, cards.KIND_CARD_BUILT_FOR, "migration_v2c_4_canopy"),
]


def run_step(name, planner, kind, op, execute):
    """Queues the step's one question (never moves or links anything: answering it does that
    through core.cards), and records `name` in schema_migrations. Returns a short summary."""
    plan = planner()
    if plan is None:
        summary = "nothing to ask (no matching card)"
        card = payload = None
    else:
        card, payload = plan
        summary = f"would ask about card:{card['slug']} ({len(payload['options']) - 1} candidate(s))"
    if not execute:
        return summary
    did = None
    with db.transaction() as tx:
        if card is not None:
            did = db.queue_decision_once(kind, f"card:{card['slug']}", payload, conn=tx.conn)
            if did is not None:
                db.insert_change_log(
                    tx.conn, op, changes.ACTOR_MIGRATION,
                    [changes.row_image("pending_decisions", {"id": did}, None,
                                       {"kind": kind, "post_slug": f"card:{card['slug']}"})],
                    batch_id=changes.new_batch_id(), affected_slugs=[card["slug"]])
        tx.conn.execute("INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)", (name, time.time()))
    return f"queued decision {did}" if did is not None else (summary + "; nothing new queued")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually queue the questions (default: plan only)")
    ap.add_argument("--force", action="store_true", help="run a step even if schema_migrations says it ran")
    args = ap.parse_args()
    db.init_db(migrate=False)
    done = db.applied_migrations()
    for name, planner, kind, op in STEPS:
        if name in done and not args.force:
            print(f"{name}: already applied, skipped")
            continue
        print(f"{name}: {run_step(name, planner, kind, op, args.execute)}")


if __name__ == "__main__":
    main()
