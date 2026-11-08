#!/usr/bin/env python3
"""Self-contained check for #771: the `place` card kind (a track, a club field, a shop).

Owner decisions, 2026-11-08:
  1. a place is a GROUP kind like a collection: its events (and things / projects) are its members,
     in an "At this place" section, with the same membership / privacy machinery;
  2. each member's page says "Where: <place>" (all of them when it is in several; a place the
     viewer can't see, #604, is never named);
  3. no whereabouts: never asked "do you still own it?", never greys, no provenance nag, kept out of
     the Apprentice's correlations;
  4. stage "Still going" (in_use) / "Closed" (done), nothing else, through ONE helper
     (card_rules.stage_label(stage, kind)) wherever a stage is shown;
  5. converting an existing collection keeps its members; a stage a place can't have is refused
     (never picked silently) unless an allowed stage rides in the same change; undoable.

Throwaway DB, no server:  python scripts/test_place_kind_771.py
Ends with ALL PASS, or FAILED: <names> (exit 1).
"""

import os
import re
import secrets
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("place-771-")
os.environ.setdefault("CAPTION_DISABLED", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from starlette.testclient import TestClient  # noqa: E402

from core import actor, apprentice_rules, card_level, card_rules, cards, curator, db, sharing, users  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from mcp_server import server  # noqa: E402

FAILS = []
BASE = "http://testhost.local"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def refusal(fn, *a, **kw):
    """(code, message, details) of an AppError, or None."""
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code, e.message, e.details
    return None


def mcp_err(out):
    return (out.get("error") or {}) if isinstance(out, dict) and out.get("ok") is False else None


def q(sql, *args):
    import sqlite3
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def execute(sql, *args):
    import sqlite3
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def new_client():
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def select_block(html, select_id):
    m = re.search(r'<select id="%s".*?</select>' % re.escape(select_id), html, re.S)
    return m.group(0) if m else ""


db.init_db()
users.limiter.reset()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()
admin = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)

# ---- 1. the kind, its rules and its words -------------------------------------------------------
print("--- 1. the kind, the rules, the words ---")
check("place is a kind, labelled Place", "place" in card_rules.KINDS and card_rules.kind_label("place") == "Place")
check("place is a GROUP kind (like a collection)", "place" in card_rules.GROUP_KINDS)
check("page config: members section 'At this place', no nesting",
      card_rules.page_config("place")["members"] and card_rules.page_config("place")["members_label"] == "At this place"
      and not card_rules.page_config("place")["nests"])
check("every kind has a page config", set(card_rules.CARD_PAGES) == set(card_rules.KINDS))
check("PLACE_STAGES is in_use + done", card_rules.PLACE_STAGES == ("in_use", "done"))
check("stage_label: a place says Still going / Closed",
      card_rules.stage_label("in_use", "place") == "Still going" and card_rules.stage_label("done", "place") == "Closed")
check("stage_label: every other kind (and no kind) keeps the old words",
      card_rules.stage_label("in_use", "thing") == "In use" and card_rules.stage_label("done", "collection") == "Done"
      and card_rules.stage_label("in_use") == "In use" and card_rules.stage_label("idea", "place") == "Idea"
      and card_rules.stage_label(None, "place") == "")
check("stages_for: a place offers two, others six",
      card_rules.stages_for("place") == ("in_use", "done") and card_rules.stages_for("thing") == card_rules.STAGES)
check("default stage: a place starts Still going, others In progress",
      card_rules.default_stage("place") == "in_use" and card_rules.default_stage("project") == "in_progress")
for bad in ("idea", "in_progress", "paused", "stopped"):
    try:
        card_rules.validate_status("place", bad, "failed" if bad == "stopped" else None)
        res = None
    except card_rules.CardError as e:
        res = e
    check(f"validate_status refuses a place at {bad!r}, naming Still going / Closed / the stages",
          res is not None and res.code == "bad_status" and "Still going" in res.message and "Closed" in res.message
          and "in_use" in res.message and "done" in res.message, res.message if res else "no error")
check("validate_status allows a place Still going and Closed",
      card_rules.validate_status("place", "in_use")["activity"] == "active"
      and card_rules.validate_status("place", "done")["activity"] == "inactive")
check("a place is outside WHEREABOUTS_KINDS", "place" not in card_rules.WHEREABOUTS_KINDS)
check("a place is never nagged for provenance", not card_rules.provenance_nag_applies("place")
      and "place" in card_rules.NO_PROVENANCE_NAG_KINDS)
check("a place is out of the Apprentice's correlation kinds", "place" not in apprentice_rules.CORRELATION_KINDS)
check("whereabouts never greys a place, whatever the column holds", not card_rules.whereabouts_gone("place", "lost"))
wref = refusal(card_rules.validate_whereabouts, "place", "have_it", "in_use")
check("validate_whereabouts refuses a place with a clear message",
      wref and wref[0] == "bad_whereabouts" and "Place" in wref[1], wref)

# ---- 2. creating, membership ---------------------------------------------------------------------
print("--- 2. a place, its members ---")
res = cards.create("RD 39 Raceway", kind="place")
place = res.data["card"]
check("a place is created Still going by default", place["kind"] == "place" and place["stage"] == "in_use"
      and place["activity"] == "active", place)
ref = refusal(cards.create, "Bad Track", kind="place", stage="in_progress")
check("creating a place In progress is refused in the place words", ref and ref[0] == "bad_status" and "Still going" in ref[1], ref)
ev1 = cards.create("Spring Race", kind="event", stage="done").data["card"]
ev2 = cards.create("Summer Race", kind="event", stage="done").data["card"]
shop_build = cards.create("Jeep Build", kind="project").data["card"]
loner = cards.create("Not Racing", kind="event", stage="done").data["card"]
for m in (ev1, ev2, shop_build):
    cards.add_to_family(place["id"], m["id"])
fam = cards.family_fields(db.get_project(place["id"]))
check("members join a place (events and a project)", {m["slug"] for m in fam["members"]} == {ev1["slug"], ev2["slug"], shop_build["slug"]})
check("a member lists the place among its families",
      [f["slug"] for f in cards.family_fields(db.get_project(ev1["id"]))["families"]] == [place["slug"]])
check("db.count_family_members counts a place's members", db.count_family_members(place["id"]) == 3)
cards.remove_from_family(place["id"], shop_build["id"])
check("a member leaves a place", db.count_family_members(place["id"]) == 2)
cards.add_to_family(place["id"], shop_build["id"])
ref = refusal(cards.add_to_family, place["id"], place["id"])
check("a place can't be its own member", ref and ref[0] == "bad_membership", ref)
other = cards.create("Club Field", kind="place").data["card"]
ref = refusal(cards.add_to_family, place["id"], other["id"])
check("a place can't hold another place (flat, one level)", ref and ref[0] == "bad_membership" and "places" in ref[1], ref)
ref = refusal(cards.nest, other["id"], ev1["id"])
check("a place can't be nested", ref and ref[0] == "nest_group_kind", ref)
ref = refusal(cards.add_to_family, ev1["id"], loner["id"])
check("a non-group card can't have members; the refusal names places too",
      ref and ref[0] == "bad_membership" and "place" in ref[1], ref)
sp = cards.set_status(place["id"], "done")
check("a place can be Closed, and the status fields say Closed",
      sp.batch_id and cards.status_fields(db.get_project(place["id"]))["stage_label"] == "Closed")
ref = refusal(cards.set_status, place["id"], "paused")
check("setting a place Paused is refused", ref and ref[0] == "bad_status" and "Still going" in ref[1], ref)
cards.set_status(place["id"], "in_use")

# ---- 3. no whereabouts ---------------------------------------------------------------------------
print("--- 3. no whereabouts ---")
ref = refusal(cards.set_whereabouts, place["id"], "have_it")
check("setting whereabouts on a place is refused, clearly", ref and ref[0] == "bad_whereabouts" and "Place" in ref[1], ref)
out = server.constructicon_update_project(card=place["slug"], fields={"whereabouts": "sold"})
e = mcp_err(out)
check("MCP: whereabouts on a place is refused (bad_whereabouts)", e and e["code"] == "bad_whereabouts", out)
r = admin.post(f"/api/projects/{place['id']}/whereabouts", data={"whereabouts": "lost"}, headers=SAME)
check("web: POST whereabouts on a place is 422 bad_whereabouts with a message",
      r.status_code == 422 and r.json()["error"]["code"] == "bad_whereabouts" and r.json()["error"]["message"], r.text[:200])
check("whereabouts_fields: it doesn't apply to a place", cards.whereabouts_fields(db.get_project(place["id"]))["whereabouts_applies"] is False)
rows = cards.provenance_whereabouts_needs(kind="place")
check("never asked: no missing-whereabouts / provenance need for a place", rows == [], rows)
execute("UPDATE projects SET whereabouts = 'lost' WHERE id = ?", place["id"])  # a stray column value must not grey it
face = cards.card_face(db.get_project(place["id"]))
check("never greys: the place's face is not 'gone'", face["gone"] is False and face["zone"] == "Still going", face)
execute("UPDATE projects SET whereabouts = NULL WHERE id = ?", place["id"])
thing = cards.create("A Truck", kind="thing").data["card"]
rows = cards.provenance_whereabouts_needs(kind="thing")
check("(control) a thing without whereabouts is still asked", any(x["card_slug"] == thing["slug"] for x in rows))
needs = [n for n in cards.list_needs_decision() if n.get("card_slug") == place["slug"]]
check("the place has no whereabouts or provenance questions in the Curator's needs",
      not [n for n in needs if n["need"] in ("missing_whereabouts", "missing_provenance", "missing_provenance_credit")], needs)

# ---- 4. the words, everywhere --------------------------------------------------------------------
print("--- 4. Still going / Closed everywhere a stage shows ---")
sc = curator.score_project(db.get_project(place["id"]))
check("Curator score: stage_label is Still going", sc["stage_label"] == "Still going", sc["stage_label"])
cards.set_status(place["id"], "done")
check("card face zone says Closed when closed", cards.card_face(db.get_project(place["id"]))["zone"] == "Closed")
check("Curator dashboard groups by the place words",
      "Closed" in curator.score_all_projects()["projects_by_stage"])
cards.set_status(place["id"], "in_use")
g = server.constructicon_get_project(place["slug"])
check("MCP get_project: stage_label Still going", g.get("stage") == "in_use" and g.get("stage_label") == "Still going", g.get("stage_label"))
ex = server.constructicon_explain_card(place["slug"])
check("MCP explain_card: stage_label Still going", ex.get("stage_label") == "Still going", {k: ex.get(k) for k in ("stage", "stage_label")})
lp = {p["slug"]: p for p in server.constructicon_list_projects(kind="place")}
check("MCP list_projects(kind=place): the place, Still going", lp.get(place["slug"], {}).get("stage_label") == "Still going", list(lp))
check("(control) a thing in use still says In use",
      cards.status_fields({**db.get_project(thing["id"]), "stage": "in_use"})["stage_label"] == "In use")

# ---- 5. the pages --------------------------------------------------------------------------------
print("--- 5. the pages ---")
r = admin.get(f"/project/{place['slug']}")
html = r.text
check("place page renders", r.status_code == 200, r.status_code)
check("place page: members section is titled 'At this place'", 'id="members-heading">At this place<' in html)
check("place page: both events are in it", "Spring Race" in html and "Summer Race" in html)
check("place page: the stage reads Still going", "Still going" in html)
check("place page: no whereabouts question, row or picker",
      'id="whereabouts-select"' not in html and "<dt>Whereabouts</dt>" not in html)
stage_sel = select_block(html, "project-stage-select")
check("place page: the stage picker offers only Still going and Closed",
      "Still going" in stage_sel and "Closed" in stage_sel and "In progress" not in stage_sel and "Idea" not in stage_sel
      and "Paused" not in stage_sel and "Stopped" not in stage_sel, stage_sel[:300])
check("place page: the kind picker offers Place", 'value="place"' in select_block(html, "project-kind-select"))
check("place page: no 'Where:' line on the place itself", 'id="card-where"' not in html)
r = admin.get(f"/project/{ev1['slug']}")
html = r.text
check("event page renders", r.status_code == 200)
check("event page: 'Where:' names the place, linked",
      'id="card-where"' in html and "Where:" in html and f'href="/project/{place["slug"]}"' in html and "RD 39 Raceway" in html)
check("event page: the stage picker is untouched (all stages, ordinary words)",
      "In progress" in select_block(html, "project-stage-select") and "Idea" in select_block(html, "project-stage-select"))
r = admin.get(f"/project/{loner['slug']}")
check("a card in no place shows no 'Where:'", 'id="card-where"' not in r.text)
other2 = cards.create("Second Track", kind="place").data["card"]
cards.add_to_family(other2["id"], ev1["id"])
html = admin.get(f"/project/{ev1['slug']}").text
check("in several places: all are listed",
      "Where:" in html and "RD 39 Raceway" in html and "Second Track" in html, re.findall(r'id="card-where".*?</p>', html, re.S))
cards.remove_from_family(other2["id"], ev1["id"])
r = admin.get(f"/project/{shop_build['slug']}")
check("a project in a place says Where: too (not only events)", 'id="card-where"' in r.text and "RD 39 Raceway" in r.text)
home = admin.get("/")
check("home renders with a place on it", home.status_code == 200)
face_html = admin.get(f"/project/{place['slug']}").text
check("the place's own face carries the place icon class", "cx-kind-place" in face_html)

# ---- 6. privacy: a private place is never named --------------------------------------------------
print("--- 6. a private place is never named ---")
PW = {"OW": "pl-" + secrets.token_urlsafe(16), "OUT": "pl-" + secrets.token_urlsafe(16)}
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("pl_owner", PW["OW"], "editor")
    users.create_user("pl_out", PW["OUT"], "editor")
C = {}
for key, name in (("OW", "pl_owner"), ("OUT", "pl_out")):
    c = new_client()
    rr = c.post("/api/auth/login", json={"username": name, "password": PW[key]}, headers=SAME)
    check(f"{key} signs in", rr.status_code == 200, rr.text[:200])
    C[key] = c
HDR = {k: {**SAME, "X-CSRF-Token": csrf_of(c)} for k, c in C.items()}
secret_place = C["OW"].post("/api/projects", data={"title": "Zebracorn Secret Raceway", "kind": "place"}, headers=HDR["OW"])
check("OW creates a place over the web (Still going by default)", secret_place.status_code == 200
      and secret_place.json()["kind"] == "place" and secret_place.json()["stage"] == "in_use", secret_place.text[:200])
sp_row = db.get_project(secret_place.json()["slug"])
sev = C["OW"].post("/api/projects", data={"title": "Zebracorn Night Race", "kind": "event", "stage": "done"}, headers=HDR["OW"])
sev_row = db.get_project(sev.json()["slug"])
with actor.acting_as("user:pl_owner"):
    cards.add_to_family(sp_row["id"], sev_row["id"])
html = C["OW"].get(f"/project/{sev_row['slug']}").text
check("owner sees 'Where:' with their private place", "Where:" in html and "Zebracorn Secret Raceway" in html)
with actor.acting_as("user:pl_owner"):
    pr = sharing.set_private("card", sp_row["id"], True)
for what, path in (("the private place", f"/project/{sp_row['slug']}"), ("its member event", f"/project/{sev_row['slug']}")):
    r = C["OUT"].get(path)
    check(f"outsider: {what} is a 404", r.status_code == 404, r.status_code)
    check(f"outsider: {what} doesn't leak the place's name", "Zebracorn Secret Raceway" not in r.text)
html = C["OW"].get(f"/project/{sev_row['slug']}").text
check("owner still sees the private place in 'Where:'", "Zebracorn Secret Raceway" in html)
# Belt and braces: where_places is built from the visibility-filtered families, so a place the actor
# can't see never reaches the template even if a card were somehow shown.
with actor.acting_as("user:pl_out"):
    ff = cards.family_fields(db.get_project(sev_row["id"]))
check("family_fields for the outsider leaves the private place out", all(f["slug"] != sp_row["slug"] for f in ff["families"]), ff)

# ---- 7. collection -> place keeps its members ----------------------------------------------------
print("--- 7. converting a collection ---")
coll = cards.create("RD 39 Raceway at Pollack Airpark", kind="collection", stage="in_use").data["card"]
e1 = cards.create("Pollack Race One", kind="event", stage="done").data["card"]
e2 = cards.create("Pollack Race Two", kind="event", stage="done").data["card"]
for m in (e1, e2):
    cards.add_to_family(coll["id"], m["id"])
res = cards.set_kind(coll["id"], "place")
check("collection -> place (stage in_use already fine) is one undoable change",
      res.batch_id and db.get_project(coll["id"])["kind"] == "place" and db.get_project(coll["id"])["stage"] == "in_use")
check("...and keeps its members", db.count_family_members(coll["id"]) == 2
      and {m["slug"] for m in db.list_family_members(coll["id"])} == {e1["slug"], e2["slug"]})
cards.undo(res.batch_id)
check("undo of the kind change: a collection again, members intact",
      db.get_project(coll["id"])["kind"] == "collection" and db.count_family_members(coll["id"]) == 2)

# 7b. a stage a place can't have
cards.set_status(coll["id"], "paused")
before = db.get_project(coll["id"])
ref = refusal(cards.set_kind, coll["id"], "place")
check("a Paused collection -> place is refused, never silently re-staged",
      ref and ref[0] == "bad_status" and "Nothing was changed" in ref[1] and "in_use" in ref[1] and "done" in ref[1]
      and "stage" in ref[1] and ref[2].get("allowed_stages") == ["in_use", "done"], ref)
check("...and nothing changed", db.get_project(coll["id"])["kind"] == "collection" and db.get_project(coll["id"])["stage"] == "paused"
      and before["updated_at"] == db.get_project(coll["id"])["updated_at"])
out = server.constructicon_update_project(card=coll["slug"], kind="place")
e = mcp_err(out)
check("MCP: update_project(kind=place) on a Paused collection is refused with the allowed stages",
      e and e["code"] == "bad_status" and "in_use" in e["message"] and "done" in e["message"], out)
check("...nothing was written", db.get_project(coll["id"])["kind"] == "collection")
r = admin.post(f"/api/projects/{coll['id']}", data={"kind": "place"}, headers=SAME)
check("web: Save kind=place on a Paused collection is 422 bad_status, with a how-to",
      r.status_code == 422 and r.json()["error"]["code"] == "bad_status" and "stage" in r.json()["error"]["message"], r.text[:300])
check("...and the web refusal changed nothing", db.get_project(coll["id"])["kind"] == "collection")
out = server.constructicon_update_project(card=coll["slug"], kind="place", stage="paused")
check("MCP: kind=place with stage=paused is still refused (the stage must be allowed too)",
      (mcp_err(out) or {}).get("code") == "bad_status" and db.get_project(coll["id"])["kind"] == "collection", out)

# 7c. kind + stage together
out = server.constructicon_update_project(card=coll["slug"], kind="place", stage="in_use", dry_run=True)
check("MCP dry run: kind=place + stage=in_use is accepted and writes nothing",
      mcp_err(out) is None and out.get("dry_run") and db.get_project(coll["id"])["kind"] == "collection", out)
out = server.constructicon_update_project(card=coll["slug"], kind="place", stage="in_use")
row = db.get_project(coll["id"])
check("MCP: kind=place + stage=in_use converts in ONE call",
      mcp_err(out) is None and row["kind"] == "place" and row["stage"] == "in_use" and out.get("batch_id"), out)
check("...members kept, stage reads Still going", db.count_family_members(coll["id"]) == 2 and out.get("stage_label") == "Still going", out)
und = server.constructicon_undo(out["batch_id"])
row = db.get_project(coll["id"])
check("MCP undo restores the collection AND its Paused stage",
      mcp_err(und) is None and row["kind"] == "collection" and row["stage"] == "paused" and db.count_family_members(coll["id"]) == 2, und)
r = admin.post(f"/api/projects/{coll['id']}", data={"kind": "place", "stage": "done"}, headers=SAME)
row = db.get_project(coll["id"])
check("web: Save kind=place + stage=done converts, answers a batch_id",
      r.status_code == 200 and row["kind"] == "place" and row["stage"] == "done" and r.json().get("batch_id"), r.text[:300])
html = admin.get(f"/project/{coll['slug']}").text
check("web: the converted place's page reads At this place / Closed, with both events",
      'id="members-heading">At this place<' in html and "Closed" in html and "Pollack Race One" in html and "Pollack Race Two" in html)
html_e = admin.get(f"/project/{e1['slug']}").text
check("web: its events now say Where: <the place>", "Where:" in html_e and "RD 39 Raceway at Pollack Airpark" in html_e)
und = admin.post(f"/api/changes/{r.json()['batch_id']}/undo", headers=SAME)
row = db.get_project(coll["id"])
check("web undo: back to a Paused collection with its members", und.status_code == 200 and row["kind"] == "collection"
      and row["stage"] == "paused" and db.count_family_members(coll["id"]) == 2, und.text[:200])

# 7d. whereabouts on the collection must be cleared first
cards.set_status(coll["id"], "in_use")
cards.set_whereabouts(coll["id"], "have_it")
ref = refusal(cards.set_kind, coll["id"], "place")
check("a collection WITH whereabouts -> place is refused, saying to clear it first",
      ref and ref[0] == "bad_whereabouts" and "Clear this card's whereabouts first" in ref[1] and "nothing was changed" in ref[1], ref)
cards.set_whereabouts(coll["id"], None)
res = cards.set_kind(coll["id"], "place")
check("...cleared, it converts", db.get_project(coll["id"])["kind"] == "place")

# 7e. place -> non-group follows the collection rules
ref = refusal(cards.set_kind, coll["id"], "project")
check("place -> project with members is refused unless force (like a collection)",
      ref and ref[0] == "bad_kind" and "member" in ref[1], ref)
res = cards.set_kind(coll["id"], "collection")
check("place -> collection is fine (members kept)", db.get_project(coll["id"])["kind"] == "collection"
      and db.count_family_members(coll["id"]) == 2)
res = cards.set_kind(coll["id"], "place")
res = cards.set_kind(coll["id"], "project", force=True)
check("place -> project with force drops the memberships (as a collection does)",
      db.get_project(coll["id"])["kind"] == "project" and db.count_family_members(coll["id"]) == 0 and bool(res.warnings))
cards.undo(res.batch_id)
check("...and undo brings the place and its members back", db.get_project(coll["id"])["kind"] == "place"
      and db.count_family_members(coll["id"]) == 2)

# ---- 8. the rest of the surface ------------------------------------------------------------------
print("--- 8. the rest of the surface ---")
out = server.constructicon_create_project(title="MCP Track", kind="place")
check("MCP create_project(kind=place) starts Still going", mcp_err(out) is None and out.get("kind") == "place"
      and out.get("stage") == "in_use" and out.get("stage_label") == "Still going", out)
out = server.constructicon_family(family="mcp-track", member=e1["slug"])
check("MCP family(add) puts an event in a place", mcp_err(out) is None, out)
tools = {t.name: t for t in __import__("asyncio").run(server.mcp.list_tools())}
doc = tools["constructicon_update_project"].description
check("MCP tool docs name the place kind and its two stages",
      "place" in doc and "Still going" in doc and "Closed" in doc and "stage=" in doc, doc[:200])
check("MCP list_projects doc lists the place kind", "place" in tools["constructicon_list_projects"].description)
lv = card_level.card_level(db.get_project(coll["id"]))
check("card_level grades a place like a group card (members are graded, the stack pip counts them)",
      "2 member(s)" in lv["reasons"]["stack"] and "members are dated" in lv["reasons"]["dates"], lv)
r = admin.get("/api/projects")
check("/api/projects lists the place with kind 'place'", any(p["kind"] == "place" for p in r.json()))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
