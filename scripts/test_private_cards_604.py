#!/usr/bin/env python3
"""Private cards and hobbies, members, inheritance and the write guard (#604 build step 3).
Throwaway DB, no server.

    python scripts/test_private_cards_604.py

Users: admin A, the owner OW (editor), ED (editor, an EDITOR member), VM (editor role, a VIEWER
member), OUT (editor, not a member), VW (viewer role); anonymous; the install token.
Fixtures (made by OW through the web, so OW owns them): a private card P with a file, a card C nested
in P (no tick of its own), an open card O, a file on both O and P, a private hobby H with a card HC
and a loose file, and an unfiled file of OUT's.
  1. sharing: only the owner or an admin may tick Private / edit members (403 otherwise, 404 for a
     card the caller can't see); role ceiling; set / add / remove are imaged and undoable; a
     non-owner can't undo a sharing change;
  2. the view matrix per actor: card page, card API, card list, home, item page / API, search, the
     file grid, hotlinks (stay public), "private wins", the hobby page and its cards and loose file,
     the tag tree, the decision and Curator queues, the change log, MCP list / get / search;
  3. inheritance: C follows P; a private child with its own list closes it to P's other members;
  4. the write guard: a viewer member can't change P or its files (403, nothing written) but may file
     their own upload; an editor member may; OUT gets 404 for P; merge private -> open is refused;
  5. the static export leaves P, C and their files out.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import json
import os
import re
import secrets
import sys

import _testenv  # noqa: E402
TMP = _testenv.isolate("private604-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, policy, sharing, users  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def new_client():
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def err(r):
    try:
        b = r.json()
    except ValueError:
        return None
    return (b.get("error") or {}).get("code") if isinstance(b, dict) else None


def raises(fn, code=None):
    try:
        fn()
    except AppError as e:
        return code is None or e.code == code
    return False


db.init_db()
users.limiter.reset()

print("--- users ---")
PW, NAMES = {}, {"A": ("pc_admin", "admin"), "OW": ("pc_owner", "editor"), "ED": ("pc_editor", "editor"),
                 "VM": ("pc_viewmem", "editor"), "OUT": ("pc_out", "editor"), "VW": ("pc_viewer", "viewer")}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for key, (name, role) in NAMES.items():
        PW[key] = "pc-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[key], role)
UID = {k: db.get_user(username=n)["id"] for k, (n, _r) in NAMES.items()}
ACT = {k: f"user:{n}" for k, (n, _r) in NAMES.items()}
C = {"anonymous": new_client(), "token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for key, (name, _r) in NAMES.items():
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": PW[key]}, headers=SAME)
    check(f"{key} signs in", r.status_code == 200, r.text[:200])
    C[key] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in NAMES else dict(SAME)) for k, c in C.items()}


def upload(who, filename, text):
    r = C[who].post("/api/upload", files={"file": (filename, text.encode(), "text/plain")},
                    data={"description": f"pc {filename}"}, headers=H[who])
    assert r.status_code == 200, r.text[:300]
    return r.json()["slug"]


def new_card(who, title, parent=None):
    data = {"title": title}
    if parent is not None:
        data["parent_id"] = str(parent)
    r = C[who].post("/api/projects", data=data, headers=H[who])
    assert r.status_code == 200, r.text[:300]
    return db.get_project(r.json()["slug"])


def file_onto(who, slug, card_id):
    return C[who].post(f"/api/image/{slug}/project", data={"project_id": str(card_id)}, headers=H[who])


# ---- fixtures ----------------------------------------------------------------------------------
print("--- fixtures ---")
P = new_card("OW", "Zebracorn Private Workshop")
CH = new_card("OW", "Zebracorn Nested Part", parent=P["id"])
O = new_card("OW", "Zebracorn Open Card")
I_P = upload("OW", "zebracorn-private-notes.txt", "private notes")
I_C = upload("OW", "zebracorn-nested-notes.txt", "nested notes")
I_O = upload("OW", "zebracorn-open-notes.txt", "open notes")
I_BOTH = upload("OW", "zebracorn-both-notes.txt", "on both")
I_U = upload("OUT", "zebracorn-unfiled-notes.txt", "out's unfiled")
for slug, card in ((I_P, P), (I_C, CH), (I_O, O), (I_BOTH, O), (I_BOTH, P)):
    r = file_onto("OW", slug, card["id"])
    check(f"OW files {slug[:6]} onto {card['title']}", r.status_code == 200, r.text[:200])
r = C["OW"].post("/api/hobbies", data={"name": "Zebracorn Secret Hobby"}, headers=H["OW"])
HOB = db.get_hobby(r.json()["slug"])
HC = new_card("OW", "Zebracorn Hobby Card")
with actor.acting_as(ACT["OW"]):
    cards.add_to_hobby(HC["id"], HOB["id"])
I_H = upload("OW", "zebracorn-hobby-loose.txt", "loose in the hobby")
with actor.acting_as(actor.ACTOR_SCRIPT):
    from core import tags as tags_svc
    tags_svc.attach(I_H, [HOB["name"]])
check("OW owns P (created_by_user_id)", P["created_by_user_id"] == UID["OW"])
check("nothing is private yet: OUT sees P", policy.can_view_card(db.get_project(P["id"]), ACT["OUT"]))

# ---- 1. sharing -------------------------------------------------------------------------------
print("--- 1. sharing ---")
r = C["OUT"].post(f"/api/sharing/card/{O['id']}/private", data={"private": "true"}, headers=H["OUT"])
check("OUT can't tick Private on OW's card (403 forbidden, says who can)",
      r.status_code == 403 and err(r) == "forbidden" and "owner" in r.json()["error"]["message"], r.text[:200])
r = C["OW"].post(f"/api/sharing/card/{P['id']}/private", data={"private": "true"}, headers=H["OW"])
check("OW ticks Private on P (200, batch_id)", r.status_code == 200 and r.json().get("batch_id"), r.text[:200])
PRIV_BATCH = r.json().get("batch_id")
check("P is private in the DB", db.get_project(P["id"])["private"] == 1)
r = C["OUT"].get(f"/api/sharing/card/{P['id']}")
check("OUT asking about P: 404 (not found, as if missing)", r.status_code == 404, r.status_code)
r = C["OUT"].post(f"/api/sharing/card/{P['id']}/private", data={"private": "false"}, headers=H["OUT"])
check("OUT can't untick P (404: can't even see it)", r.status_code == 404, r.status_code)
for who, role in (("ED", "editor"), ("VM", "viewer")):
    r = C["OW"].post(f"/api/sharing/card/{P['id']}/members", data={"user": NAMES[who][0], "role": role}, headers=H["OW"])
    check(f"OW adds {who} as {role}", r.status_code == 200 and r.json().get("batch_id"), r.text[:200])
r = C["OW"].post(f"/api/sharing/card/{P['id']}/members", data={"user": NAMES["VW"][0], "role": "editor"}, headers=H["OW"])
check("an install viewer can't be a card editor (400 role_ceiling, says how to fix it)",
      r.status_code == 400 and err(r) == "role_ceiling" and "viewer" in r.json()["error"]["message"], r.text[:200])
r = C["OW"].post(f"/api/sharing/card/{P['id']}/members", data={"user": "nobody-here", "role": "viewer"}, headers=H["OW"])
check("an unknown user: 404 with a pointer", r.status_code == 404 and "user" in r.json()["error"]["message"].lower(), r.text[:200])
r = C["VM"].post(f"/api/sharing/card/{P['id']}/members", data={"user": NAMES["OUT"][0], "role": "viewer"}, headers=H["VM"])
check("a viewer member can't add people (403)", r.status_code == 403, r.status_code)
st = C["ED"].get(f"/api/sharing/card/{P['id']}").json()
check("state: private, owner OW, two members, ED can't manage",
      st["private"] and st["owner"]["username"] == "pc_owner" and len(st["members"]) == 2 and not st["can_manage"], st)
r = C["OUT"].post(f"/api/changes/{PRIV_BATCH}/undo", headers=H["OUT"])
check("OUT can't undo OW's Private tick (403/404, nothing undone)", r.status_code in (403, 404)
      and db.get_project(P["id"])["private"] == 1, r.text[:200])
r = C["ED"].post(f"/api/changes/{PRIV_BATCH}/undo", headers=H["ED"])
check("an editor member can't undo the owner's Private tick either (403)", r.status_code == 403
      and db.get_project(P["id"])["private"] == 1, r.text[:200])
# dry run writes nothing
with actor.acting_as(ACT["OW"]):
    res = sharing.add_member("card", P["id"], NAMES["OUT"][0], "viewer", dry_run=True)
check("dry run add: says it would add, writes nothing", res.dry_run and not any(m["user_id"] == UID["OUT"]
      for m in db.share_members("card", P["id"])))

# ---- 2. the view matrix ------------------------------------------------------------------------
print("--- 2. view matrix ---")
SEE_P = {"A": True, "OW": True, "ED": True, "VM": True, "OUT": False, "VW": False, "token": True}
for who, sees in SEE_P.items():
    c = C[who]
    want = 200 if sees else 404
    check(f"{who:5} /project/P -> {want}", c.get(f"/project/{P['slug']}").status_code == want)
    check(f"{who:5} /project/C (nested in P) -> {want}", c.get(f"/project/{CH['slug']}").status_code == want)
    check(f"{who:5} /api/cards/P -> {want}", c.get(f"/api/cards/{P['slug']}").status_code == want)
    listed = [p["slug"] for p in c.get("/api/projects").json()]
    check(f"{who:5} /api/projects {'lists' if sees else 'hides'} P and C",
          (P["slug"] in listed) == sees and (CH["slug"] in listed) == sees)
    check(f"{who:5} home {'shows' if sees else 'hides'} P's title", ("Zebracorn Private Workshop" in c.get("/").text) == sees)
    check(f"{who:5} /object/<P's file> -> {want}", c.get(f"/object/{I_P}").status_code == want)
    check(f"{who:5} /api/image/<C's file> -> {want}", c.get(f"/api/image/{I_C}").status_code == want)
    check(f"{who:5} /api/image/<file on P and O> -> {want} (private wins)", c.get(f"/api/image/{I_BOTH}").status_code == want)
    check(f"{who:5} the open card's file -> 200", c.get(f"/api/image/{I_O}").status_code == 200)
    check(f"{who:5} OUT's unfiled file -> 200 (unfiled: everyone)", c.get(f"/api/image/{I_U}").status_code == 200)
    found = {i["slug"] for i in c.get("/api/search?query=zebracorn").json()}
    check(f"{who:5} search {'finds' if sees else 'hides'} P's and C's files",
          (I_P in found) == sees and (I_C in found) == sees and (I_BOTH in found) == sees and I_O in found and I_U in found,
          sorted(found))
    grid = {i["slug"] for i in c.get("/api/items/grid?source=home&limit=500").json().get("items", [])}
    check(f"{who:5} the home file grid {'has' if sees else 'hides'} P's file", (I_P in grid) == sees)
    check(f"{who:5} /f/<P's file> stays 200 (a hotlink is the deliberate share)", c.get(f"/f/{I_P}").status_code == 200)
check("anonymous /f/<P's file> -> 200 too (public hotlink rule unchanged)", C["anonymous"].get(f"/f/{I_P}").status_code == 200)

# tag tree
tags_out = json.dumps(C["OUT"].get("/api/tags").json())
tags_ow = json.dumps(C["OW"].get("/api/tags").json())
check("tag tree: OUT doesn't get P's linked tag; OW does",
      "Zebracorn Private Workshop" not in tags_out and "Zebracorn Private Workshop" in tags_ow)

# change log
with actor.acting_as(ACT["OUT"]):
    out_rows = cards.list_changes(limit=500)
with actor.acting_as(ACT["OW"]):
    ow_rows = cards.list_changes(limit=500)
touches_p = lambda rows: any(m.get("table") == "projects" and m.get("key", {}).get("id") == P["id"]
                             for r in rows for m in r["mutations"])
check("change log: OUT reads no row touching P; OW does", not touches_p(out_rows) and touches_p(ow_rows))

# MCP as users (the tool wrapper runs as the request's actor; here we set it directly)
from mcp_server import server as mcp  # noqa: E402
from mcp_server import auth as mcp_auth  # noqa: E402


def as_user(key, fn, *a, **kw):
    token = mcp_auth.IDENTITY.set({"actor": ACT[key], "user_id": UID[key], "username": NAMES[key][0]})
    try:
        return fn(*a, **kw)
    finally:
        mcp_auth.IDENTITY.reset(token)


lp = [p["slug"] for p in as_user("OUT", mcp.constructicon_list_projects)]
check("MCP list_projects as OUT hides P and C", P["slug"] not in lp and CH["slug"] not in lp and O["slug"] in lp)
g = as_user("OUT", mcp.constructicon_get_project, P["slug"])
check("MCP get_project(P) as OUT: not_found", isinstance(g, dict) and (g.get("error") or {}).get("code") == "not_found", g)
g = as_user("VM", mcp.constructicon_get_project, P["slug"])
check("MCP get_project(P) as VM (member): the card, with its sharing state",
      g.get("slug") == P["slug"] and g.get("sharing", {}).get("private") is True, str(g)[:200])
s = as_user("OUT", mcp.constructicon_search, "zebracorn")
check("MCP search as OUT: none of P's files", isinstance(s, list) and I_P not in {i["slug"] for i in s})
sh = as_user("OUT", mcp.constructicon_sharing, action="get", card=P["slug"])
check("MCP sharing(get, P) as OUT: not_found", (sh.get("error") or {}).get("code") == "not_found", sh)
sh = as_user("OW", mcp.constructicon_sharing, action="get", card=P["slug"])
check("MCP sharing(get, P) as OW: private, can_manage", sh.get("private") and sh.get("can_manage"), sh)

# hobby
r = C["OW"].post(f"/api/sharing/hobby/{HOB['id']}/private", data={"private": "true"}, headers=H["OW"])
check("OW ticks Private on the hobby H", r.status_code == 200, r.text[:200])
for who, sees in (("OW", True), ("A", True), ("OUT", False), ("VM", False)):
    want = 200 if sees else 404
    from core import install_config  # noqa: E402
    check(f"{who:5} the hobby page -> {want}", C[who].get(f"/hobby/{HOB['slug']}").status_code == want)
    check(f"{who:5} the hobby's card (inherits) -> {want}", C[who].get(f"/project/{HC['slug']}").status_code == want)
    check(f"{who:5} the hobby's loose file -> {want}", C[who].get(f"/api/image/{I_H}").status_code == want)
    names = [h["slug"] for h in C[who].get("/api/hobbies").json()]
    check(f"{who:5} /api/hobbies {'lists' if sees else 'hides'} H", (HOB["slug"] in names) == sees)

# queues
from core import curation_queue, decisions  # noqa: E402
with actor.acting_as(ACT["OUT"]):
    q = curation_queue.cached_queue()
groups = [g["id"] for g in q["groups"] + q["deferred"]]
check("Curator queue as OUT: no group for P, C or HC",
      f"card:{P['slug']}" not in groups and f"card:{CH['slug']}" not in groups and f"card:{HC['slug']}" not in groups, groups)
with actor.acting_as(ACT["OW"]):
    q2 = curation_queue.cached_queue()
check("Curator queue as OW: P's group is there", f"card:{P['slug']}" in [g["id"] for g in q2["groups"] + q2["deferred"]])

# ---- 3. inheritance ---------------------------------------------------------------------------
print("--- 3. inheritance ---")
with actor.acting_as(ACT["OW"]):
    stc = sharing.state("card", CH["id"])
check("C (no tick of its own): effective, inherited from P",
      not stc["private"] and stc["effective"] and [i["slug"] for i in stc["inherited_from"]] == [P["slug"]], stc)
with actor.acting_as(ACT["OW"]):
    res = sharing.set_private("card", CH["id"], False)
check("unticking C can't open it: warning names P, OUT still can't see C",
      any("inherits" in w for w in res.warnings) and not policy.can_view_card(db.get_project(CH["id"]), ACT["OUT"]), res.warnings)
with actor.acting_as(ACT["OW"]):
    sharing.set_private("card", CH["id"], True)
    sharing.add_member("card", CH["id"], NAMES["ED"][0], "editor")
check("a private child with its own list: ED (on both lists) sees C", policy.can_view_card(db.get_project(CH["id"]), ACT["ED"]))
check("... VM (on P's list only) doesn't", not policy.can_view_card(db.get_project(CH["id"]), ACT["VM"]))
check("... VM still sees P", policy.can_view_card(db.get_project(P["id"]), ACT["VM"]))
check("... and C's file is hidden from VM", C["VM"].get(f"/api/image/{I_C}").status_code == 404)

# ---- 4. the write guard --------------------------------------------------------------------------
print("--- 4. write guard ---")
before = db.get_by_slug(I_P)["display_name"]
r = C["VM"].post(f"/api/image/{I_P}", data={"display_name": "renamed by a viewer member"}, headers=H["VM"])
check("VM (viewer member) can't edit OW's file on P: 403, says why", r.status_code == 403 and err(r) == "forbidden"
      and "look" in r.json()["error"]["message"], r.text[:200])
check("... nothing written", db.get_by_slug(I_P)["display_name"] == before)
r = C["VM"].post(f"/api/projects/{P['id']}/text", data={"synopsis": "viewer was here"}, headers=H["VM"])
check("VM can't change P's text (403)", r.status_code == 403, r.text[:200])
I_VM = upload("VM", "zebracorn-viewmember-own.txt", "my own")
r = file_onto("VM", I_VM, P["id"])
check("VM may file their OWN upload onto P (follow-up 2)", r.status_code == 200, r.text[:200])
r = file_onto("VM", I_U, P["id"])
check("VM can't file OUT's unfiled file onto P (403, nothing written)", r.status_code == 403
      and P["id"] not in [p["id"] for p in db.list_projects_for_post(I_U)], r.text[:200])
r = file_onto("ED", I_U, P["id"])
check("ED (editor member) may file OUT's file onto P", r.status_code == 200, r.text[:200])
r = C["ED"].post(f"/api/image/{I_P}", data={"display_name": "renamed by an editor member"}, headers=H["ED"])
check("ED may edit OW's file on P", r.status_code == 200, r.text[:200])
r = C["OUT"].post(f"/api/projects/{P['id']}/text", data={"synopsis": "out was here"}, headers=H["OUT"])
check("OUT changing P: 404 (can't see it)", r.status_code == 404, r.text[:200])
r = file_onto("OUT", I_O, P["id"])
check("OUT filing onto P: 404", r.status_code in (400, 404), r.text[:200])
check("OUT may still edit OUT's own file now on P? no: it's hidden from OUT now (404)",
      C["OUT"].post(f"/api/image/{I_U}", data={"display_name": "x"}, headers=H["OUT"]).status_code == 404)
with actor.acting_as(ACT["OW"]):
    check("merge private P into open O: refused (privacy_conflict), nothing written",
          raises(lambda: cards.merge_cards(O["id"], [P["id"]]), "privacy_conflict") and db.get_project(P["id"]) is not None)

# ---- 5. static export --------------------------------------------------------------------------
print("--- 5. export ---")
from core import site_export  # noqa: E402
out_dir = os.path.join(TMP, "export-604")
with actor.acting_as(actor.ACTOR_SCRIPT):
    rep = site_export.build_site({"project_slugs": [P["slug"], CH["slug"], O["slug"]], "blog_entry_slugs": []},
                                 out_dir=out_dir)
blob = ""
for dp, _dn, fns in os.walk(out_dir):
    for fn in fns:
        if fn.endswith(".html"):
            blob += open(os.path.join(dp, fn), encoding="utf-8", errors="replace").read()
check("export: P and C left out with a warning, O kept",
      "Zebracorn Private Workshop" not in blob and "Zebracorn Open Card" in blob
      and any("private" in w for w in rep.get("warnings", [])), rep.get("warnings"))
check("export: the file on both O and P is not published (private wins)", "zebracorn-both-notes" not in blob)

# ---- 6. undated items never name a private card (#725 x #604) -----------------------------------
print("--- 6. constructicon_undated and private cards ---")
from core import item_dates, items  # noqa: E402
I_PD = upload("OW", "zebracorn-dated-notes.txt", "dated, so P has a range to lend")
check("OW files the dated file onto P", file_onto("OW", I_PD, P["id"]).status_code == 200)
with actor.acting_as(actor.ACTOR_SCRIPT):
    items.update(I_PD, content_date=1388534400)  # 2014-01-01: P now has a real-dated range
check("precondition: P is private, hidden from OUT, visible to OW",
      not policy.can_view_card(db.get_project(P["id"]), ACT["OUT"])
      and policy.can_view_card(db.get_project(P["id"]), ACT["OW"]))
TITLE = P["title"]
u_ow = as_user("OW", mcp.constructicon_undated)
ent = {e["slug"]: e for e in u_ow.get("items", [])}
check("undated as OW (owner): P's undated file borrows P's range and names P",
      I_P in ent and TITLE in ent[I_P]["shows"] and (ent[I_P]["inherits"] or {}).get("card_title") == TITLE, ent.get(I_P))
for who in ("OUT", "VW"):
    u = as_user(who, mcp.constructicon_undated)
    blob = json.dumps(u)
    check(f"undated as {who}: P's files are not listed and P's title appears nowhere",
          isinstance(u, dict) and "items" in u and TITLE not in blob and I_P not in blob and I_C not in blob, blob[:300])
    u2 = as_user(who, mcp.constructicon_undated, include_inherited=False)
    check(f"undated(include_inherited=false) as {who}: P's title appears nowhere", TITLE not in json.dumps(u2))
    acc = as_user(who, mcp.constructicon_undated, action="accept", slug=I_P, source="siblings")
    check(f"undated(accept, P's file) as {who}: not_found, P not named",
          (acc.get("error") or {}).get("code") == "not_found" and TITLE not in json.dumps(acc), acc)
# The second lock: even handed a row on P directly (no filter_visible), the entry never names P.
row_p = db.get_by_slug(I_P)
e_out = item_dates.listing_entry(row_p, access=policy.card_access(ACT["OUT"]))
check("listing_entry for OUT's access: the range stays, P is not named in shows / inherits / guesses",
      TITLE not in json.dumps(e_out) and e_out["shows"].startswith(item_dates.APPROX)
      and e_out["inherits"] is not None and e_out["inherits"]["card_title"] is None, e_out)
e_ow = item_dates.listing_entry(row_p, access=policy.card_access(ACT["OW"]))
check("listing_entry for OW's access names P", TITLE in e_ow["shows"], e_ow["shows"])

print()
print(("FAILED: " + ", ".join(FAILS)) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
