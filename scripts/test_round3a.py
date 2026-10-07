#!/usr/bin/env python3
"""Round 3A checks: #592 (captions persisted + self-heal), #586 (replace question direction,
reverse answer, same file), #584 (Thing-or-Project from what the card holds).

Throwaway DB + storage, no server, no GPU:

    python scripts/test_round3a.py

Exits 1 if any check fails.
"""

import os
import sys
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("round3a-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine: core imports every object type, svg included
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import card_migration, captions, cards, db, decisions, revisions, storage  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
DAY = 86400.0
NOW = time.time()


def mk(slug, filename, media_type="document", data=None, **kw):
    """An item with a real stored file (so same-file comparison has bytes to compare)."""
    stored = f"stored-{slug}"
    storage.path_for(stored).write_bytes(data if data is not None else slug.encode())
    db.insert_upload(slug, filename, stored, "tester", media_type=media_type,
                     file_size=len(data if data is not None else slug.encode()), **kw)
    return slug


def set_ts(slug, ts):
    conn = db.get_conn()
    conn.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (ts, slug))
    conn.commit()
    conn.close()


def decision_for(slug):
    for d in db.list_pending_decisions(revisions.KIND_ITEM_SUPERSEDES):
        if d["post_slug"] == slug and d["resolved_at"] is None:
            return d
    return None


def keys(d):
    return [o["key"] for o in d["payload"]["options"]]


# ============================ #592 captions ============================
calls = []
captions.caption_once = lambda *a, **k: calls.append(a) or {"caption": "a cat", "elapsed_seconds": 0.0, "restarted": False,
                                                          "restart_seconds": 0.0, "restart_reason": None, "error": None}
captions._caption_source_path = lambda row, spec: "/dev/null"
for n in range(3):
    mk(f"cap{n}", f"pic{n}.png", media_type="image")
captions._wake.clear()
captions.run_caption("cap0")   # what an upload / regenerate click / retype now does, in the web role
check("#592: web run_caption does not caption inline", calls == [])
check("#592: ...it persists a caption_queue row", (db.peek_caption_queue() or {}).get("slug") == "cap0")
check("#592: ...marks the item pending", db.get_by_slug("cap0")["type_metadata"].get(captions.STATUS_KEY) == "pending")
check("#592: ...and wakes the worker", captions._wake.is_set())
captions.run_caption("cap1")
# "restart": nothing in memory survives, the rows do. A fresh drain resumes them in order.
check("#592: after a restart the queued items resume", captions._drain_one() is True and captions._drain_one() is True
      and len(calls) == 2)
check("#592: both came out done", all(db.get_by_slug(s)["type_metadata"].get(captions.STATUS_KEY) == "done" for s in ("cap0", "cap1")))
check("#592: queue is empty again", db.peek_caption_queue() is None)

# self-heal: pending, no queue row, old -> re-queued; recent / queued / disabled -> left alone
mk("orph_old", "old.png", media_type="image")
mk("orph_new", "new.png", media_type="image")
mk("orph_queued", "queued.png", media_type="image")
mk("orph_done", "done.png", media_type="image")
mk("orph_stl", "m.stl", media_type="stl")
for s in ("orph_old", "orph_new", "orph_queued", "orph_stl"):
    db._update_content_metadata(s, type_metadata={captions.STATUS_KEY: "pending"})
db._update_content_metadata("orph_done", type_metadata={captions.STATUS_KEY: "done"})
set_ts("orph_old", NOW - 3600)
set_ts("orph_queued", NOW - 3600)
set_ts("orph_done", NOW - 3600)
set_ts("orph_stl", NOW - 3600)
db.enqueue_caption("orph_queued")
n = captions.requeue_orphans()
q = db.peek_caption_queue()
rows = set()
conn = db.get_conn()
rows = {r["slug"] for r in conn.execute("SELECT slug FROM caption_queue")}
conn.close()
check("#592 self-heal: re-queues the old pending item with no row", "orph_old" in rows and n == 1, f"{n} {rows}")
check("#592 self-heal: leaves a recent pending item", "orph_new" not in rows)
check("#592 self-heal: leaves an item that is already queued (no double row)", n == 1)
check("#592 self-heal: leaves a done item and a type that can't be captioned", "orph_done" not in rows and "orph_stl" not in rows)
captions.DISABLED = True
db.dequeue_caption("orph_old")
check("#592 self-heal: does nothing when captions are off", captions.requeue_orphans() == 0 and db.peek_caption_queue() is not None)
captions.DISABLED = False

# ============================ #586 replace question ============================
check("#586: copy_number reads ' (n)'", revisions.copy_number("X (1).msi") == 1 and revisions.copy_number("X (12).msi") == 12
      and revisions.copy_number("X.msi") == 0 and revisions.copy_number("X (1) final.msi") == 0)

# (a) identical bytes, X.msi then "X (1).msi": suggest "same file", whichever upload came first
BIG = b"MSI-ish harmless bytes " * 50
mk("msi_a", "NinjaAgent-Setup.msi", media_type="installer", data=BIG, source_modified_at=NOW - 10 * DAY)
mk("msi_b", "NinjaAgent-Setup (1).msi", media_type="installer", data=BIG, source_modified_at=NOW - 5 * DAY)
revisions.queue_replace_question("msi_b")
d = decision_for("msi_b")
check("#586 same: question queued", d is not None)
p = d["payload"]
check("#586 same: 'same file' is offered and suggested", p["suggested"] == "same:msi_a", str(p["suggested"]))
check("#586 same: the reverse answer is offered too", "reverse:msi_a" in keys(d) and "msi_a" in keys(d) and "none" in keys(d))
check("#586 same: reason names the evidence", "identical contents" in (p["suggested_reason"] or ""), str(p["suggested_reason"]))
res = decisions.resolve(d["id"], choice="same:msi_a")
check("#586 same: choosing it trashes this upload, keeps the other",
      db.get_by_slug("msi_b") is None and db.get_by_slug("msi_a") is not None and res.get("trashed") == "msi_b", str(res))
check("#586 same: it is in the trash for the 7-day undo", any(t["slug"] == "msi_b" and t["expires_at"] for t in db.list_trash(holds=False)))
undo = cards.undo(res["batch_id"])
check("#586 same: undo restores the file and the open question",
      db.get_by_slug("msi_b") is not None and decision_for("msi_b") is not None
      and storage.path_for("stored-msi_b").exists(), str(undo))

# (b) different files, suffix decides, regardless of upload order
mk("rep_plain", "report.pdf", media_type="pdf", data=b"older body", source_modified_at=NOW - 30 * DAY)
mk("rep_one", "report (1).pdf", media_type="pdf", data=b"newer body!", source_modified_at=NOW - 20 * DAY)
# upload order: the (1) copy first, then the plain one
set_ts("rep_one", NOW - 2000)
set_ts("rep_plain", NOW - 1000)
revisions.queue_replace_question("rep_plain")
d = decision_for("rep_plain")
p = d["payload"]
check("#586 suffix: plain file asked about, (1) copy is the candidate", p["candidate_slugs"] == ["rep_one"])
check("#586 suffix: suggests the REVERSE (the (1) copy replaces this)", p["suggested"] == "reverse:rep_one", str(p["suggested"]))
check("#586 suffix: reason names the suffix and the dates", "(1) copy is the later download" in p["suggested_reason"]
      and "modified" in p["suggested_reason"], p["suggested_reason"])
check("#586 suffix: no 'same file' offered for different bytes", not any(k.startswith("same:") for k in keys(d)))
res = decisions.resolve(d["id"], choice="reverse:rep_one")
check("#586 reverse: link recorded as the (1) copy superseding the plain one", db.revision_pairs() == {"rep_plain": "rep_one"}
      or db.revision_pairs().get("rep_plain") == "rep_one", str(db.revision_pairs()))
check("#586 reverse: decision resolved", decision_for("rep_plain") is None)
cards.undo(res["batch_id"])
check("#586 reverse: undo removes the link and re-opens the question",
      "rep_plain" not in db.revision_pairs() and decision_for("rep_plain") is not None)
res = decisions.resolve(decision_for("rep_plain")["id"], choice="none")
check("#586: 'No, separate' still links nothing", "rep_plain" not in db.revision_pairs() and res["applied"] == [])

# (c) modified date decides when there is no suffix, even when the OLDER file was uploaded last
mk("memo_new", "memo.docx", media_type="word", data=b"new memo", source_modified_at=NOW - 2 * DAY)
set_ts("memo_new", NOW - 5000)
mk("memo_old", "memo_v1.docx", media_type="word", data=b"old memo", source_modified_at=NOW - 40 * DAY)
set_ts("memo_old", NOW - 100)  # uploaded last, but modified long ago
revisions.queue_replace_question("memo_old")
d = decision_for("memo_old")
p = d["payload"]
check("#586 date: older-by-date file uploaded last is NOT suggested to replace", p["suggested"] == "reverse:memo_new", str(p["suggested"]))
check("#586 date: reason names the modified dates", (p["suggested_reason"] or "").startswith("The other file is the later one: modified"),
      str(p["suggested_reason"]))
# and the forward direction: this file is the newer one
mk("plan_old", "plan.pdf", media_type="pdf", data=b"p1", source_modified_at=NOW - 50 * DAY)
mk("plan_new", "plan_rev2.pdf", media_type="pdf", data=b"p2", source_modified_at=NOW - 3 * DAY)
revisions.queue_replace_question("plan_new")
p = decision_for("plan_new")["payload"]
check("#586 date: this file is newer -> forward answer suggested", p["suggested"] == "plan_old"
      and "This is the later file" in p["suggested_reason"], str(p))
# upload time is the last resort
mk("zed_a", "zedfile.pdf", media_type="pdf", data=b"z1")
mk("zed_b", "zedfile_v2.pdf", media_type="pdf", data=b"z2")
set_ts("zed_a", NOW - 900)
set_ts("zed_b", NOW - 100)
revisions.queue_replace_question("zed_b")
p = decision_for("zed_b")["payload"]
check("#586 last resort: upload time, and the reason says so", p["suggested"] == "zed_a" and "uploaded later" in p["suggested_reason"], str(p))

# candidate already replaces something: no reverse offered (the link would be refused)
mk("chain_a", "spec.pdf", media_type="pdf", data=b"c1")
mk("chain_b", "spec_v2.pdf", media_type="pdf", data=b"c2")
revisions.mark_superseded("chain_a", "chain_b")
mk("chain_c", "spec (1).pdf", media_type="pdf", data=b"c3")
revisions.queue_replace_question("chain_c")
d = decision_for("chain_c")
check("#586: a candidate that already has a predecessor gets no 'replaces this' answer",
      "reverse:chain_b" not in keys(d) and "chain_b" in keys(d), str(keys(d)))

# ============================ #584 kind suggestion ============================
BASE = {"title": "screenshot-correlator", "description": "", "id": 1, "slug": "x"}
sk = card_migration.suggest_kind
kind, why, _ = sk(BASE, {"type_counts": {"code": 55, "markdown": 4, "data": 2, "image": 7}})
check("#584 scripts-heavy card -> Project, reason shows the mix", kind == "project" and "61 of 68 files are" in why
      and "source code" in why, f"{kind}: {why}")
kind, why, _ = sk({**BASE, "title": "Walnut shelf"}, {"type_counts": {"code": 6, "image": 2}})
check("#584 a plain title no longer implies Thing", kind == "project", why)
kind, why, _ = sk({**BASE, "title": "Tool kit app"}, {"type_counts": {"image": 12, "stl": 3}, "physical_piece_files": 4})
check("#584 photos + STLs + physical-piece fields -> Thing (even with a software-ish title)", kind == "thing"
      and "physical-piece" in why, f"{kind}: {why}")
kind, why, _ = sk(BASE, {"type_counts": {"image": 9}})
check("#584 photos only -> Thing, reason says so", kind == "thing" and "9 of 9 files are" in why, f"{kind}: {why}")
kind, why, _ = sk(BASE, {"type_counts": {}, "whereabouts": "on_shelf"})
check("#584 no files but a whereabouts -> Thing", kind == "thing" and "whereabouts" in why, f"{kind}: {why}")
kind, why, _ = sk(BASE, {"type_counts": {}})
check("#584 no evidence -> Project (the less-claiming answer)", kind == "project" and "less-claiming" in why, f"{kind}: {why}")
kind, why, _ = sk(BASE, {"type_counts": {"image": 5, "code": 5}})
check("#584 an even split with no physical signal -> Project", kind == "project", why)
kind, why, _ = sk(BASE, {})
check("#584 a caller with no facts at all -> Project", kind == "project")

# the real planner uses the evidence and does not touch an answered decision
a = db._create_project("Scripts Card")["id"]
b = db._create_project("Photo Card")["id"]
for i in range(4):
    mk(f"sc{i}", f"tool{i}.py", media_type="code", data=b"print(1)")
    db._add_item_to_project(a, f"sc{i}")
for i in range(3):
    mk(f"ph{i}", f"photo{i}.jpg", media_type="image", data=b"jpg")
    db._add_item_to_project(b, f"ph{i}")
conn = db.get_conn()
conn.execute("UPDATE projects SET stage = NULL, kind = 'project', status = 'complete' WHERE id IN (?, ?)", (a, b))
conn.commit()
conn.close()
plan = {e["card"]["id"]: e for e in card_migration.build_plan()}
kinds = {cid: [p for k, p in e["decisions"] if k == "card_kind"] for cid, e in plan.items()}
sa = kinds.get(a, [{}])[0].get("suggested") if kinds.get(a) else None
sb = kinds.get(b, [{}])[0].get("suggested") if kinds.get(b) else None
reason_a = kinds[a][0]["suggested_reason"] if kinds.get(a) else ""
check("#584 planner: scripts card -> project, photo card -> thing", (sa, sb) == ("project", "thing"), f"{sa} {sb} {reason_a}")
check("#584 planner: reason shows the file mix", "4 of 4 files are source code" in reason_a, reason_a)
# an already-answered (resolved) decision is never re-asked
card_a = db.get_project(a)
did = db.queue_decision_once("card_kind", f"card:{card_a['slug']}", {"suggested": "thing"})
check("#584: queue_decision_once skips an existing decision (open or answered)",
      db.queue_decision_once("card_kind", f"card:{card_a['slug']}", {"suggested": "project"}) is None and did is not None)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
