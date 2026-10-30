#!/usr/bin/env python3
"""One report card (#545): the Curator score and the card pips grade a card the same way, and the
card's stage is the only status. Throwaway DB, no server, no network.

    python scripts/test_one_report_card_545.py

Owner decisions on #545 (2026-10-17): a write-up counts at >= 200 characters, dates count when
>= 80% of the card's files have a real date (never the upload time), and `projects.status` is
retired (the stage is the only status). Covers:
  1. the shared checks: write-up, dates and cover pass or fail in BOTH engines together, with the
     same reason text, on cards built to sit on each side of the thresholds (and on the gap the old
     rules left: a 50-character write-up and 70% dated used to pass the Curator, fail the pips);
  2. every card in the fixture: the pips' cover/dates/writeup checks equal the Curator checklist's
     cover_image/content_dates/writeup_body, and the reasons match word for word;
  3. the stage drives the Curator: paused / idea / stopped are silent, in progress excuses the
     timeline gap and the highlight, done / in use apply everything (the old status column is
     gone since #760);
  4. nudges: the missing_writeup / missing_dates nudge summary is the pips' reason; the stage
     weights the priority (done 3, in progress 1); the stale nudge is for in-progress cards only;
  5. the export's default selection follows the stage only, whatever the old column says;
  6. the retired names are gone (curator_status, export_status, curator_status_for,
     STATUS_APPLICABILITY, PROJECT_STATUSES, list_projects(status=)), and no API, page or MCP read
     hands out the old `status` word for a card (score, dashboard, related, hobby, project zip,
     get_project).
Prints PASS/FAIL per check and ends with ALL PASS or FAILED: <names>.
"""

import io
import json
import os
import sqlite3
import sys
import time
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("reportcard545-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from core import (actor, card_level, card_rules, cards, curator, curator_needs, db, hobbies,  # noqa: E402
                  items, membership, paths)
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def sql(statement, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    try:
        c.execute(statement, args)
        c.commit()
    finally:
        c.close()


db.init_db()
DAY = 86400
BASE = int(time.mktime((2019, 5, 12, 12, 0, 0, 0, 0, -1)))
_n = [0]


def add_files(card_id, dated, undated):
    """`dated` files with a real content_date, `undated` with only their upload time."""
    slugs = []
    for i in range(dated + undated):
        _n[0] += 1
        slug = f"rc545-{_n[0]}"
        (paths.storage_dir() / f"{slug}.txt").write_text(f"file {slug}\n")
        db.insert_upload(slug, f"{slug}.txt", f"{slug}.txt", "tester", media_type="document",
                         content_date=(BASE + i * DAY) if i < dated else None)
        slugs.append(slug)
    membership.add_files(card_id, slugs, link_tag=True, auto_cover=False)
    return slugs


def set_writeup(card, text):
    items.update(card["writeup_slug"], type_metadata={"body": text})


def card(title):
    return next(p for p in db.list_projects() if p["title"] == title)


def checklist(score, item):
    return next(c for c in score["checklist"] if c["item"] == item)


SHARED = (("cover", "cover_image"), ("dates", "content_dates"), ("writeup", "writeup_body"))

with actor.acting_as(actor.ACTOR_UI):
    for t in ("Good Card", "Gap Card", "Overrides Card", "Edge Card", "Paused Card", "Done Card", "Stale Card"):
        cards.create(t)
    good, gap, ovr, edge = card("Good Card"), card("Gap Card"), card("Overrides Card"), card("Edge Card")
    # Good: 4 of 5 dated (exactly 80%), a 250-character write-up
    add_files(good["id"], 4, 1)
    set_writeup(good, "x" * 250)
    # Gap: 7 of 10 dated (70%: the old Curator's 60% passed it), a 50-character write-up
    add_files(gap["id"], 7, 3)
    set_writeup(gap, "A short note about the card, fifty characters long")
    # Overrides: nothing dated, but the card's own start and end are set
    add_files(ovr["id"], 0, 2)
    cards.update(ovr["id"], start=BASE, end=BASE + 30 * DAY)
    # Edge: 199 characters, spaces not counted (so 300 with spaces still fails)
    add_files(edge["id"], 3, 0)
    set_writeup(edge, " ".join(["y" * 199]) + " " * 101)

    good, gap, ovr, edge = card("Good Card"), card("Gap Card"), card("Overrides Card"), card("Edge Card")

# --- 1. the shared checks, both sides of each threshold ----------------------------------------------
print("--- 1. write-up >= 200 characters, >= 80% real dates, in both engines ---")
expect = {
    "Good Card": {"dates": True, "writeup": True},
    "Gap Card": {"dates": False, "writeup": False},
    "Overrides Card": {"dates": True, "writeup": False},
    "Edge Card": {"dates": True, "writeup": False},
}
for title, want in expect.items():
    c = card(title)
    pips = card_level.card_level(c)
    score = curator.score_project(c)
    for pip, item in (("dates", "content_dates"), ("writeup", "writeup_body")):
        check(f"{title}: pips {pip} = {want[pip]}", pips["checks"][pip] is want[pip], pips["reasons"][pip])
        check(f"{title}: Curator {item} = {want[pip]}", checklist(score, item)["passed"] is want[pip],
              checklist(score, item))
gap_score = curator.score_project(gap)
check("Gap Card: the write-up reason names the 200-character rule, in both",
      checklist(gap_score, "writeup_body")["reason"] == card_level.card_level(gap)["reasons"]["writeup"]
      and "Needs 200" in checklist(gap_score, "writeup_body")["reason"], checklist(gap_score, "writeup_body"))
check("Gap Card: the dates reason says 7 of 10 (need 80%)",
      checklist(gap_score, "content_dates")["reason"] == "7 of 10 files have real dates (need 80%).",
      checklist(gap_score, "content_dates"))
check("Overrides Card: dates pass on the card's start + end", checklist(curator.score_project(ovr), "content_dates")["reason"]
      == "Start and end dates are set.")
check("the write-up doc is not counted as a file (Good Card: 4 of 5, not 4 of 6)",
      checklist(curator.score_project(good), "content_dates")["reason"].startswith("4 of 5 "))
check("SCORING_RULES descriptions carry the shared thresholds",
      "200" in json.dumps(curator.SCORING_RULES) and "80%" in json.dumps(curator.SCORING_RULES))

# --- 2. every card: the two engines agree on every shared check ---------------------------------------
print("--- 2. every card: pips == Curator on cover, dates, write-up ---")
mismatch = []
for c in db.list_projects():
    pips = card_level.card_level(c)
    score = curator.score_project(c)
    for pip, item in SHARED:
        row = checklist(score, item)
        if pips["checks"][pip] != row["passed"] or pips["reasons"][pip] != row.get("reason"):
            mismatch.append((c["title"], pip, pips["checks"][pip], pips["reasons"][pip], row))
check(f"no card where the pips and the Curator disagree ({len(db.list_projects())} cards)", not mismatch, mismatch)

# --- 3. the stage drives the Curator ----------------------------------------------------------------
print("--- 3. the stage is the only status ---")
with actor.acting_as(actor.ACTOR_UI):
    paused, done = card("Paused Card"), card("Done Card")
    add_files(paused["id"], 1, 0)
    add_files(done["id"], 1, 0)
    cards.set_status(paused["id"], "paused")
    cards.set_status(done["id"], "done")
for stage in ("paused", "idea", "stopped"):
    with actor.acting_as(actor.ACTOR_UI):
        cards.set_status(card("Paused Card")["id"], stage, "abandoned" if stage == "stopped" else None)
    s = curator.score_project(card("Paused Card"))
    check(f"stage {stage}: silent, nothing applies", s["silent"] and s["stage"] == stage and not s["unmet"]
          and all(c["excused"] for c in s["checklist"]), s)
s_done = curator.score_project(card("Done Card"))
check("stage done: everything applies (highlight and timeline gap too)",
      not s_done["silent"] and not checklist(s_done, "highlighted")["excused"]
      and not checklist(s_done, "no_large_gap")["excused"], s_done)
s_prog = curator.score_project(good)
check("stage in progress: the highlight and the timeline gap are excused, the rest applies",
      checklist(s_prog, "highlighted")["excused"] and checklist(s_prog, "no_large_gap")["excused"]
      and not checklist(s_prog, "writeup_body")["excused"], s_prog)
with actor.acting_as(actor.ACTOR_UI):
    cards.set_status(card("Done Card")["id"], "in_use")
check("stage in use: scored like done", not curator.score_project(card("Done Card"))["silent"]
      and curator.score_project(card("Done Card"))["stage_label"] == card_rules.stage_label("in_use"))
s = curator.score_project(card("Good Card"))
check("a card in progress is scored (#760: the old status column is gone, so only the stage exists)",
      not s["silent"] and s["stage"] == "in_progress"
      and "status" not in {r[1] for r in db.get_conn().execute("PRAGMA table_info(projects)")}, s)
check("the score result carries stage + stage_label and no v1 `status`",
      s["stage_label"] == "In progress" and "status" not in s, sorted(s))
check("every stage has a Curator rule", set(curator.STAGE_APPLICABILITY) == set(card_rules.STAGES))
with actor.acting_as(actor.ACTOR_UI):
    cards.create("Reference Shelf", kind="collection")
check("a reference collection (no provenance) stays silent, as v1's reference-only was",
      curator.score_project(card("Reference Shelf"))["silent"])

# --- 4. nudges --------------------------------------------------------------------------------------
print("--- 4. nudges speak the pips' words, weighted by stage ---")
nud = {n["kind"]: n for n in curator_needs.project_nudges(card("Gap Card"), dismissed=set())}
check("missing_writeup nudge: summary is the pips' reason",
      nud.get("missing_writeup", {}).get("summary") == card_level.card_level(card("Gap Card"))["reasons"]["writeup"], nud)
check("missing_dates nudge: summary is the pips' reason",
      nud.get("missing_dates", {}).get("summary") == card_level.card_level(card("Gap Card"))["reasons"]["dates"], nud)
check("in progress: weight 1", nud["missing_writeup"]["status_weight"] == 1, nud["missing_writeup"])
check("a card with a 250-character write-up gets no missing_writeup nudge",
      "missing_writeup" not in {n["kind"] for n in curator_needs.project_nudges(card("Good Card"), dismissed=set())})
dn = curator_needs.project_nudges(card("Done Card"), dismissed=set())
check("in use: weight 3", dn and all(n["status_weight"] == 3 for n in dn), dn)
with actor.acting_as(actor.ACTOR_UI):
    stale = card("Stale Card")
    add_files(stale["id"], 2, 0)   # dated 2019: long quiet
kinds = {n["kind"] for n in curator_needs.project_nudges(card("Stale Card"), dismissed=set())}
check("an in-progress card quiet for years gets the stale nudge", "stale_wip" in kinds, kinds)
with actor.acting_as(actor.ACTOR_UI):
    cards.set_status(card("Stale Card")["id"], "done")
kinds = {n["kind"] for n in curator_needs.project_nudges(card("Stale Card"), dismissed=set())}
check("the same card once done: no stale nudge", "stale_wip" not in kinds, kinds)

# --- 5. the export's default selection --------------------------------------------------------------
print("--- 5. export default: the stage only ---")
rows = [
    ({"stage": "in_progress", "status": "wip", "kind": "project"}, True),
    ({"stage": "in_progress", "status": "shelved", "kind": "project"}, True),
    ({"stage": "paused", "status": "active", "kind": "project"}, False),
    ({"stage": "done", "status": "active", "kind": "project"}, False),
    ({"stage": "in_progress", "status": "active", "kind": "collection"}, False),
    ({"stage": None, "status": "active", "kind": "project"}, False),
]
for row, want in rows:
    check(f"export_included_by_default({row}) = {want}", card_rules.export_included_by_default(row) is want)

# --- 6. the retired names and the old word ----------------------------------------------------------
print("--- 6. retired names are gone; no read hands out the old word ---")
for mod, name in ((card_rules, "curator_status"), (card_rules, "export_status"), (cards, "curator_status_for"),
                  (curator, "STATUS_APPLICABILITY"), (curator, "_score_means_to_an_end"),
                  (curator_needs, "_status_weight"), (db, "PROJECT_STATUSES")):
    check(f"{mod.__name__}.{name} is gone", not hasattr(mod, name))
try:
    db.list_projects(status="active")
    check("db.list_projects has no status filter", False)
except TypeError:
    check("db.list_projects has no status filter", True)
src = open(os.path.join(HERE, "..", "core", "curator.py"), encoding="utf-8").read()
check("curator.py speaks no v1 status words", not any(w in src for w in ('"wip"', '"complete"', '"shelved"',
                                                                        "means-to-an-end", '"published"')))

dash = curator.score_all_projects()
check("dashboard: projects_by_stage keyed by stage label, no projects_by_status",
      "projects_by_status" not in dash and "In progress" in dash["projects_by_stage"], dash.get("projects_by_stage"))
check("dashboard: every score has stage, none has status",
      all("stage" in s and "status" not in s for s in dash["all_project_scores"]))

from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app, base_url="http://testhost.local:8000")
with actor.acting_as(actor.ACTOR_UI):
    cards.link(card("Good Card")["id"], card("Gap Card")["id"], "related")
    h = hobbies.create("Report Cards").data["hobby"]
    hobbies.add_card(h["id"], card("Good Card")["id"])
# (POST /api/project/{slug}/related was retired in #556; it 404s now.)
r = client.get(f"/api/hobby/{h['slug']}")
hp = r.json().get("projects") if r.status_code == 200 else None
check("GET hobby: its cards carry stage, not status",
      bool(hp) and all("stage_label" in p and "status" not in p for p in hp), (r.status_code, r.text[:300]))
r = client.get("/api/projects")
check("GET /api/projects: no status on a card option",
      r.status_code == 200 and all("status" not in p for p in r.json()), (r.status_code, r.text[:300]))
r = client.get("/api/curator/dashboard")
check("GET /api/curator/dashboard answers with projects_by_stage",
      r.status_code == 200 and "projects_by_stage" in r.json(), (r.status_code, r.text[:300]))
r = client.get(f"/project/{card('Paused Card')['slug']}")
check("a silent card's page renders and names its stage", r.status_code == 200 and "not scored by the Curator" in r.text,
      r.status_code)
r = client.get(f"/api/projects/{card('Good Card')['id']}/export.zip")
manifest = None
if r.status_code == 200:
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        manifest = json.loads(z.read("manifest.json"))
check("project zip: the manifest carries kind + stage, not status",
      bool(manifest) and manifest["project"].get("stage") == "in_progress" and "status" not in manifest["project"],
      (r.status_code, manifest and manifest.get("project")))
from mcp_server import server as S  # noqa: E402
with actor.acting_as(actor.ACTOR_MCP):
    gp = S.constructicon_get_project(card("Good Card")["slug"])
check("MCP get_project: stage, no status", gp.get("stage") == "in_progress" and "status" not in gp, sorted(gp)[:40])

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
