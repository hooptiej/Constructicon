#!/usr/bin/env python3
"""Live regression check for bulk-accepting card decisions (#506).

`cards.resolve_decisions(accept_suggested=True)` used to crash with
`TypeError: unhashable type: 'list'` whenever a card_family_members decision was in
the batch, because that decision's `suggested` is a LIST of member keys. This builds
its own throwaway cards and decisions (unique per run), exercises the bulk path in
dry-run and real mode, and removes everything it made.

It calls core directly (no HTTP), so run it inside the test container, never production:

    docker exec constructicon-test python3 scripts/test_bulk_decisions.py

Exits 1 if any check fails.
"""

import hashlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core import card_migration, cards, db  # noqa: E402

TAG = "zqbulk" + uuid.uuid4().hex[:6]
FAILS = []
MADE_SLUGS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def fingerprint():
    """Hash of every row that a card/decision change can touch."""
    conn = db.get_conn()
    try:
        h = hashlib.sha256()
        for table in ("projects", "family_members", "project_items", "pending_decisions"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            h.update(table.encode())
            for r in rows:
                h.update(json.dumps(list(tuple(r)), default=str).encode())
        return h.hexdigest()
    finally:
        conn.close()


def card(title, **kw):
    c = db.create_project(f"{TAG} {title}", stage="in_progress", **kw)
    MADE_SLUGS.append(c["slug"])
    return c


def decide(kind, owner, field, suggested, options):
    payload = {"schema": 1, "card_id": owner["id"], "card_slug": owner["slug"], "title": owner["title"],
               "field": field, "question": "test question", "legacy_status": None, "provisional": None,
               "suggested": suggested, "suggested_reason": "test", "confidence": "high", "options": options}
    return db.queue_decision_once(kind, f"card:{owner['slug']}", payload)


def family_decision(fam, children, suggest_slugs):
    options = []
    for c in children:
        options.append({"key": c["slug"], "label": c["title"], "suggested": c["slug"] in suggest_slugs,
                        "patch": [{"op": "unnest", "card": c["slug"]},
                                  {"op": "add_to_family", "family": fam["slug"], "member": c["slug"]}]})
    options.append({"key": "none", "label": "None of these", "patch": []})
    return decide(cards.KIND_CARD_FAMILY_MEMBERS, fam, "family_members", list(suggest_slugs), options)


def status_decision(c):
    opts = [{"key": "done", "label": "Done", "patch": [card_migration._set_status_patch("done")]},
            {"key": "paused", "label": "Paused", "patch": [card_migration._set_status_patch("paused")]}]
    return decide(cards.KIND_CARD_STATUS, c, "stage", "done", opts)


def kind_decision(c):
    opts = [{"key": k, "label": k, "patch": [{"op": "set_kind", "kind": k}]} for k in ("project", "thing")]
    return decide(cards.KIND_CARD_KIND, c, "kind", "thing", opts)


def row(c):
    return db.get_project(c["slug"])


def is_open(did):
    return db.get_pending_decision(did)["resolved_at"] is None


def main():
    try:
        # Fixture 1: a family-to-be with two nested children, both suggested.
        f1 = card("Family One")
        a1, b1 = card("Alpha", parent_id=f1["id"]), card("Bravo", parent_id=f1["id"])
        d_fam1 = family_decision(f1, [a1, b1], [a1["slug"], b1["slug"]])
        s = card("Stage Card")
        d_stat = status_decision(s)
        k = card("Kind Card")
        d_kind = kind_decision(k)
        before = fingerprint()
        batch = [d_fam1, d_stat, d_kind]

        # 1. Dry run of a mixed batch including the family decision.
        r = cards.resolve_decisions(batch, accept_suggested=True, dry_run=True)
        check("dry-run mixed batch ok", r["ok"] and r["failed"] == 0 and r["would_apply"] == 3, str(r.get("items")))
        fam_item = next(i for i in r["items"] if i["decision_id"] == d_fam1)
        check("dry-run reports the family choice as the list", fam_item["choice"] == [a1["slug"], b1["slug"]],
              str(fam_item["choice"]))
        check("dry-run wrote nothing", fingerprint() == before)

        # 2. Real run.
        r = cards.resolve_decisions(batch, accept_suggested=True, dry_run=False)
        check("real mixed batch ok", r["ok"] and r["applied"] == 3 and r["failed"] == 0, str(r.get("items")))
        check("family card is now a family", row(f1)["kind"] == "family")
        check("family members added and un-nested",
              {m["slug"] for m in db.list_family_members(f1["id"])} == {a1["slug"], b1["slug"]}
              and row(a1)["parent_id"] is None and row(b1)["parent_id"] is None)
        check("status applied", row(s)["stage"] == "done", str(row(s).get("stage")))
        check("kind applied", row(k)["kind"] == "thing")
        check("all three decisions resolved", not any(is_open(d) for d in batch))

        # 3. Undo restores the original state exactly.
        cards.undo(r["batch_id"])
        check("undo restores the fingerprint", fingerprint() == before)
        check("decisions open again after undo", all(is_open(d) for d in batch))

        # 4. A family decision that can't be applied: suggests Charlie only, Delta stays nested.
        f2 = card("Family Two")
        c2, d2 = card("Charlie", parent_id=f2["id"]), card("Delta", parent_id=f2["id"])
        d_fam2 = family_decision(f2, [c2, d2], [c2["slug"]])
        before2 = fingerprint()
        mixed = [d_fam1, d_fam2, d_stat, d_kind]

        r = cards.resolve_decisions(mixed, accept_suggested=True, dry_run=True)
        bad = next(i for i in r["items"] if i["decision_id"] == d_fam2)
        check("dry-run: unappliable family fails cleanly",
              bad["status"] == "failed" and bad["error"]["code"] == "nest_group_kind" and not r["ok"], str(bad))
        check("dry-run strict: nothing written", fingerprint() == before2)

        r = cards.resolve_decisions(mixed, accept_suggested=True, dry_run=False)
        check("strict real: refuses the whole batch, writes nothing",
              not r["ok"] and r["failed"] == 1 and fingerprint() == before2, str(r.get("warnings")))

        r = cards.resolve_decisions(mixed, accept_suggested=True, dry_run=False, partial_ok=True)
        check("partial_ok: valid items applied, bad one failed", r["ok"] and r["applied"] == 3 and r["failed"] == 1,
              str([(i["decision_id"], i["status"]) for i in r["items"]]))
        check("partial_ok: refused family left untouched",
              is_open(d_fam2) and row(f2)["kind"] != "family" and row(c2)["parent_id"] == f2["id"]
              and row(d2)["parent_id"] == f2["id"])
        check("partial_ok: the others landed", row(f1)["kind"] == "family" and row(s)["stage"] == "done"
              and row(k)["kind"] == "thing")
        cards.undo(r["batch_id"])
        check("undo of the partial batch restores the fingerprint", fingerprint() == before2)

        # 5. Odd list suggestions skip instead of crashing.
        s2 = card("Odd One")
        d_odd = decide(cards.KIND_CARD_FAMILY_MEMBERS, s2, "family_members", ["not-an-option"],
                       [{"key": "none", "label": "None", "patch": []}])
        d_empty = decide(cards.KIND_CARD_KIND, card("Empty One"), "kind", [], [{"key": "thing", "label": "t", "patch": []}])
        r = cards.resolve_decisions([d_odd, d_empty], accept_suggested=True, dry_run=True)
        check("unknown / empty list suggestions are skipped, not crashes",
              r["ok"] and r["skipped"] == 2 and all(i["status"] == "skipped" for i in r["items"]), str(r.get("items")))
    finally:
        conn = db.get_conn()
        try:
            for slug in MADE_SLUGS:
                conn.execute("DELETE FROM pending_decisions WHERE post_slug = ?", (f"card:{slug}",))
            conn.commit()
        finally:
            conn.close()
        for slug in MADE_SLUGS:
            try:
                if db.get_project(slug):
                    cards.delete_card(slug)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup of {slug} failed: {e}")
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
