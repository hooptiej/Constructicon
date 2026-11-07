#!/usr/bin/env python3
"""Name Tag Mix-Up (#819) and Gallery by Nametag (#820). Throwaway DB, no server.

    python scripts/test_uploader_identity_819_820.py

  #819  who a web upload belongs to is the ACTUAL signed-in uploader, not the owner. (#845 then retired
        the "<name> — manual upload" Source note it used to write; this test follows that.)
        1. a web upload by a non-owner / the owner / a user with no display name is theirs
           (uploaded_by_user_id, channel web) and writes NO Source note; the install token's upload stays
           admin-owned with no note either; users.manual_upload_source is gone, db.source_manual_upload
           stays only to recognise the old label;
        2. forward-looking only: every row that existed before keeps its tech and uploader, and the
           label mechanism (db.source_manual_upload, source_group) is still there for old rows.
  #820  /api/gallery and the grid's "uploaded by" badge group and name by `uploaded_by_user_id`:
        3. /api/gallery: one group per user id named by display name, NULL -> "Admin (no user
           recorded)", each linking to /gallery/user/<username> (NULL and deleted users: no link);
           a row whose `tech` is the owner's label but whose uploader is Ed is grouped under ED, not
           the label (negative control: the old source_group() key for that row is the label's);
           totals agree with the listed items, also with a ?query=;
        4. the grid badge (card payload): `uploaded_by_display` is the user's name (or the NULL
           words), `uploaded_by_username` is the link key, `uploaded_by` still carries the Source note;
           cards.js renders a link to the user's gallery, a plain label for NULL, and escapes it;
        5. the Source note is still visible in the item details panel.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

import _testenv  # noqa: E402
TMP = _testenv.isolate("uploader-identity-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, install_config, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}
OWNER_NAME = "Ui Owner"
OWNER_LABEL = f"{OWNER_NAME} (me)"
LABEL = f"{OWNER_LABEL} — manual upload"
NO_USER = "Admin (no user recorded)"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def sql(q, args=()):
    conn = db.get_conn()
    try:
        cur = conn.execute(q, args)
        rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def set_config(**vals):
    for k, v in vals.items():
        sql("INSERT OR REPLACE INTO install_config (key, value, updated_at) VALUES (?, ?, ?)", (k, v, time.time()))
    install_config._cache["values"] = None


def row(slug):
    return dict(sql("SELECT * FROM capture_events WHERE slug = ?", (slug,))[0])


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def png_bytes(seed):
    from PIL import Image
    img = Image.new("RGB", (32, 24), (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


db.init_db()
users.limiter.reset()
set_config(owner_name=OWNER_NAME, owner_label=OWNER_LABEL)
check("setup: the owner's manual-upload label is what the old code wrote for everyone",
      db.source_manual_upload() == LABEL, db.source_manual_upload())

NAMES = {"O": ("ui_owner", "admin", OWNER_NAME), "E": ("ui_ed", "editor", "Ed Itor"),
         "N": ("ui_nameless", "editor", ""), "V": ("ui_view", "viewer", "Vee Ewer")}
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for k, (n, role, disp) in NAMES.items():
        PW[k] = "ui-" + secrets.token_urlsafe(16)
        users.create_user(n, PW[k], role, display_name=disp)
UID = {k: db.get_user(username=n)["id"] for k, (n, _r, _d) in NAMES.items()}
C = {"token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for k, (n, _r, _d) in NAMES.items():
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": n, "password": PW[k]}, headers=SAME)
    check(f"{k} signs in", r.status_code == 200, r.text[:200])
    C[k] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in NAMES else dict(SAME)) for k, c in C.items()}
check("setup: the nameless user has no display name (the username has to stand in)",
      not (db.get_user(user_id=UID["N"]).get("display_name") or "").strip())

# ---- before: rows that already exist (the owner's #560 backfill left them on the owner) -------------
with actor.acting_as(actor.ACTOR_SCRIPT):
    OLD = []
    for i in range(3):
        slug = f"ui-old-{i}-{secrets.token_hex(3)}"
        db.insert_upload(slug, f"old{i}.png", None, LABEL)
        OLD.append(slug)
sql("UPDATE capture_events SET uploaded_by_user_id = ? WHERE slug = ?", (UID["O"], OLD[0]))
BEFORE = {r["slug"]: (r["tech"], r["uploaded_by_user_id"])
          for r in map(dict, sql("SELECT slug, tech, uploaded_by_user_id FROM capture_events"))}


def upload(who, filename, seed, description=""):
    r = C[who].post("/api/upload", files={"file": (filename, png_bytes(seed), "image/png")},
                    data={"description": description}, headers=H[who])
    check(f"{who} uploads {filename}", r.status_code == 200, r.text[:200])
    return r.json()["slug"] if r.status_code == 200 else None


# ---- 1. #819: the note names the real uploader -----------------------------------------------------
print("--- 1. #819: the Source note names the uploader ---")
ED_UP = upload("E", "ui-ed.png", 1, "uid820ed")
OWN_UP = upload("O", "ui-owner.png", 2, "uid820owner")
NAMELESS_UP = upload("N", "ui-nameless.png", 3)
TOK_UP = upload("token", "ui-token.png", 4, "uid820token")
re_ = row(ED_UP)
check("a non-owner's web upload writes no Source note (#845), and is theirs via the web channel",
      re_["tech"] == "" and re_["uploaded_by_user_id"] == UID["E"] and re_["channel"] == "web", re_)
ro = row(OWN_UP)
check("the owner's own upload: no note, theirs", ro["tech"] == "" and ro["uploaded_by_user_id"] == UID["O"], ro["tech"])
rn = row(NAMELESS_UP)
check("a user with no display name: no note, theirs", rn["tech"] == "" and rn["uploaded_by_user_id"] == UID["N"], rn["tech"])
rt = row(TOK_UP)
check("the install token (the owner's scripts): no note, and stays admin-owned",
      rt["tech"] == "" and rt["uploaded_by_user_id"] is None, (rt["tech"], rt["uploaded_by_user_id"]))
check("the name-the-uploader writer is gone (users.manual_upload_source), the label recogniser stays",
      not hasattr(users, "manual_upload_source") and db.source_manual_upload() == LABEL
      and db.is_retired_source_note(LABEL) and not db.is_retired_source_note("Claude — authored"))
r = C["E"].post("/api/content", data={"external_url": "", "content_description": "x"}, headers=H["E"])
check("/api/content still refuses an empty request cleanly (the Source change did not break it)",
      r.status_code == 400, (r.status_code, r.text[:200]))

# ---- 2. nothing rewritten, label mechanism intact ---------------------------------------------------
print("--- 2. existing rows untouched; the label mechanism stays ---")
AFTER = {r["slug"]: (r["tech"], r["uploaded_by_user_id"])
         for r in map(dict, sql("SELECT slug, tech, uploaded_by_user_id FROM capture_events"))}
check("every row that existed before is unchanged (tech and uploader): no backfill, no rewrite",
      all(AFTER.get(s) == v for s, v in BEFORE.items()), {s: (v, AFTER.get(s)) for s, v in BEFORE.items() if AFTER.get(s) != v})
check("the old rows still carry the owner's label", all(AFTER[s][0] == LABEL for s in OLD))
check("the label mechanism is still there (source_manual_upload, source_group)",
      db.source_manual_upload() == LABEL and db.source_group(LABEL) == OWNER_LABEL)

# ---- 3. #820: /api/gallery groups by user id -------------------------------------------------------
print("--- 3. #820: /api/gallery ---")
# the trap: the owner's LABEL on a row that is really Ed's, and the same label on a row with no uploader
with actor.acting_as("user:ui_ed"):
    TRAP = f"ui-trap-{secrets.token_hex(3)}"
    db.insert_upload(TRAP, "trap.png", None, LABEL, description="uid820trap")
with actor.acting_as(actor.ACTOR_MCP):
    NOBODY = f"ui-nobody-{secrets.token_hex(3)}"
    db.insert_upload(NOBODY, "nobody.png", None, LABEL, description="uid820nobody")
check("fixtures: the trap row has the owner's label but Ed as uploader; the other has no uploader",
      row(TRAP)["tech"] == LABEL and row(TRAP)["uploaded_by_user_id"] == UID["E"]
      and row(NOBODY)["tech"] == LABEL and row(NOBODY)["uploaded_by_user_id"] is None)

r = C["V"].get("/api/gallery", params={"per_user": 50})
check("GET /api/gallery: 200 for a viewer", r.status_code == 200, r.text[:200])
G = {g["uploaded_by_display"]: g for g in r.json()}
slugs_of = lambda g: {i["slug"] for i in g["items"]}  # noqa: E731
check("groups are named by display name; the no-user group says exactly 'Admin (no user recorded)'",
      {"Ed Itor", OWNER_NAME, "ui_nameless", NO_USER} <= set(G) and OWNER_LABEL not in G, sorted(G))
check("the group key is the display name too (uploaded_by == uploaded_by_display)",
      all(g["uploaded_by"] == g["uploaded_by_display"] for g in G.values()))
check("Ed's group holds his upload AND the trap row (owner's label, Ed's id), not the owner's group",
      {ED_UP, TRAP} <= slugs_of(G["Ed Itor"]) and G["Ed Itor"]["total"] == 2
      and TRAP not in slugs_of(G[OWNER_NAME]), (G["Ed Itor"]["total"], sorted(G)))
check("the no-user group holds the install-token upload, the old unowned rows and the label-bearing NULL row",
      {TOK_UP, NOBODY, OLD[1], OLD[2]} <= slugs_of(G[NO_USER]) and G[NO_USER]["total"] == 4, G[NO_USER]["total"])
check("the owner's group holds the owner's web upload and the backfilled old row",
      {OWN_UP, OLD[0]} <= slugs_of(G[OWNER_NAME]) and G[OWNER_NAME]["total"] == 2)
check("each user group links to /gallery/user/<username> and carries the user",
      G["Ed Itor"]["gallery_url"] == "/gallery/user/ui_ed" and G[OWNER_NAME]["gallery_url"] == "/gallery/user/ui_owner"
      and G["ui_nameless"]["gallery_url"] == "/gallery/user/ui_nameless"
      and G["Ed Itor"]["uploaded_by_user"] == {"id": UID["E"], "username": "ui_ed", "name": "Ed Itor"}, G["Ed Itor"])
check("the no-user group has no account to link", G[NO_USER]["gallery_url"] is None and G[NO_USER]["uploaded_by_user"] is None)
r = C["V"].get(G["Ed Itor"]["gallery_url"])
check("the link is a real page (200, titled with the name)", r.status_code == 200 and "Ed Itor" in r.text, r.status_code)
st = C["V"].get("/api/items/grid", params={"source": "user:ui_ed", "limit": "500"})
check("... and Ed's gallery lists exactly the group's items",
      st.status_code == 200 and {i["slug"] for i in st.json()["items"]} == {ED_UP, TRAP}, st.text[:200])
total = sum(g["total"] for g in G.values())
listed = sum(len(g["items"]) for g in C["V"].get("/api/gallery", params={"per_user": 500}).json())
check("totals add up to the items listed", total == listed == sum(1 for _ in sql("SELECT 1 FROM capture_events")), (total, listed))
# negative control: what grouping by the label would have done to the same rows
old_keys = {s: db.source_group(row(s)["tech"]) for s in (TRAP, NOBODY, ED_UP)}
check("negative control: grouping by the label puts the trap row (and the NULL one) under the OWNER's label "
      "and Ed's own upload under a different one", old_keys[TRAP] == OWNER_LABEL == old_keys[NOBODY]
      and old_keys[ED_UP] != OWNER_LABEL, old_keys)
g = {x["uploaded_by_display"]: x for x in C["V"].get("/api/gallery", params={"query": "uid820trap"}).json()}
check("?query= still works: only the trap row, under Ed, total agrees",
      set(g) == {"Ed Itor"} and slugs_of(g["Ed Itor"]) == {TRAP} and g["Ed Itor"]["total"] == 1, sorted(g))
g = {x["uploaded_by_display"]: x for x in C["V"].get("/api/gallery", params={"query": "uid820nobody"}).json()}
check("?query= on a NULL-uploader row: the no-user group, total agrees",
      set(g) == {NO_USER} and slugs_of(g[NO_USER]) == {NOBODY} and g[NO_USER]["total"] == 1, sorted(g))
# a user deleted since: still named, nothing to link
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("ui_gone", "ui-" + secrets.token_urlsafe(16), "editor", display_name="Gone Girl")
GONE_ID = db.get_user(username="ui_gone")["id"]
with actor.acting_as("user:ui_gone"):
    GONE = f"ui-gone-{secrets.token_hex(3)}"
    db.insert_upload(GONE, "gone.png", None, "whatever note")
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.delete_user(GONE_ID)
G2 = {g["uploaded_by_display"]: g for g in C["V"].get("/api/gallery", params={"per_user": 50}).json()}
check("a deleted user's group is named 'deleted user #<id>' and has no link (no dead link)",
      f"deleted user #{GONE_ID}" in G2 and G2[f"deleted user #{GONE_ID}"]["gallery_url"] is None
      and slugs_of(G2[f"deleted user #{GONE_ID}"]) == {GONE}, sorted(G2))

# ---- 4. #820: the grid badge --------------------------------------------------------------------------
print("--- 4. #820: the grid's uploaded-by badge ---")
home = C["V"].get("/api/items/grid", params={"source": "home", "limit": "500"})
check("the home grid answers 200", home.status_code == 200, home.text[:200])
cards = {i["slug"]: i for i in home.json()["items"]}
check("the badge names the uploader, not the label: trap row -> 'Ed Itor' + username ui_ed",
      cards[TRAP]["uploaded_by_display"] == "Ed Itor" and cards[TRAP]["uploaded_by_username"] == "ui_ed", cards[TRAP])
check("the owner's label is no longer the badge anywhere (no 'Ui Owner (me)' badge)",
      all(c["uploaded_by_display"] != OWNER_LABEL for c in cards.values()))
check("a NULL uploader -> 'Admin (no user recorded)' with nothing to link",
      cards[NOBODY]["uploaded_by_display"] == NO_USER and cards[NOBODY]["uploaded_by_username"] is None, cards[NOBODY])
check("the install-token upload (NULL uploader) too", cards[TOK_UP]["uploaded_by_display"] == NO_USER)
check("no display name -> the username", cards[NAMELESS_UP]["uploaded_by_display"] == "ui_nameless")
check("a deleted uploader is named and not linked",
      cards[GONE]["uploaded_by_display"] == f"deleted user #{GONE_ID}" and cards[GONE]["uploaded_by_username"] is None)
pub = C["V"].get(f"/api/image/{TRAP}").json()
check("the item JSON keeps the Source note in `uploaded_by` (a free-text note, still there) beside the real uploader",
      pub["uploaded_by"] == LABEL and pub["uploaded_by_user"]["username"] == "ui_ed" and pub["uploaded_by_display"] == "Ed Itor", pub["uploaded_by"])
for slug in (ED_UP, TRAP, NOBODY):  # the group on /api/gallery and the badge on the same item say the same name
    in_group = [g["uploaded_by_display"] for g in C["V"].get("/api/gallery", params={"per_user": 50}).json()
                if slug in slugs_of(g)]
    check(f"gallery group and badge agree for {slug[:12]}", in_group == [cards[slug]["uploaded_by_display"]], in_group)

node = shutil.which("node")
if node:
    shim = ("global.window = {}; require(process.argv[1]); "
            "process.stdout.write(window.ItemCards.html(JSON.parse(process.argv[2]), {}));")
    js = os.path.join(ROOT, "web", "static", "js", "cards.js")

    def render(item):
        r = subprocess.run([node, "-e", shim, js, json.dumps(item)], capture_output=True, text=True, encoding="utf-8")
        return r.stdout if r.returncode == 0 else "NODE FAILED: " + r.stderr

    out = render(cards[TRAP])
    check("cards.js: a user's badge is a link to /gallery/user/<username>",
          '<a class="uploader-label" href="/gallery/user/ui_ed"' in out and ">Ed Itor</a>" in out, out[-300:])
    out = render(cards[NOBODY])
    check("cards.js: the no-user badge is plain text, no link",
          f'<span class="uploader-label" title="Uploaded by">{NO_USER}</span>' in out and "/gallery/user/" not in out, out[-300:])
    hostile = dict(cards[TRAP], uploaded_by_display='<img src=x onerror=alert(1)>"', uploaded_by_username='a b"<d>')
    out = render(hostile)
    check("cards.js: the badge escapes its name and URL-encodes the username",
          "<img src=x" not in out and 'onerror=alert(1)>"' not in out and "/gallery/user/a%20b%22%3Cd%3E" in out, out[-300:])
else:
    print("SKIP cards.js render checks (node not found on this machine)")

# ---- 5. the Source note is still visible in the details panel -------------------------------------------
print("--- 5. the Source note stays on the item page ---")
page = C["V"].get(f"/object/{TRAP}")
check("the item page leaves out the retired '— manual upload' note (#845), though the row still carries it",
      page.status_code == 200 and "Source:" not in page.text and row(TRAP)["tech"] == LABEL, page.status_code)
check("... and says who really uploaded it (ORIGIN: 'Uploaded by' Ed, linked)",
      "/gallery/user/ui_ed" in page.text and "Ed Itor" in page.text)
with actor.acting_as(actor.ACTOR_MCP):
    GENUINE = f"ui-genuine-{secrets.token_hex(3)}"
    db.insert_upload(GENUINE, "genuine.png", None, db.SOURCE_AUTHORED)
page = C["V"].get(f"/object/{GENUINE}")
check("a genuine label still shows: 'Source: Claude — authored'", f"Source: {db.SOURCE_AUTHORED}" in page.text)
page = C["V"].get(f"/object/{ED_UP}")
check("a new web upload shows no Source line, but 'Uploaded by' Ed and 'Arrived via the web app'",
      "Source:" not in page.text and "/gallery/user/ui_ed" in page.text and "the web app" in page.text)
first = C["E"].post("/api/upload", files={"file": ("ui-dup.png", png_bytes(9), "image/png")},
                    data={"modified_at": "1700000000000"}, headers=H["E"])
dup = C["E"].post("/api/upload", files={"file": ("ui-dup.png", png_bytes(9), "image/png")},
                  data={"modified_at": "1700000000000"}, headers=H["E"])
check("a duplicate upload is refused with who and when, not an empty label (409 'Already uploaded by Ed Itor on ...')",
      dup.status_code == 409 and "Already uploaded by Ed Itor on" in dup.text, (dup.status_code, dup.text[:200]))

print()
print(("FAILED: " + ", ".join(FAILS)) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
