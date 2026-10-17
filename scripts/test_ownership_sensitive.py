#!/usr/bin/env python3
"""Ownership and the sensitive switch (#604 build steps 1-2, #603). Throwaway DB, no server.

    python scripts/test_ownership_sensitive.py

In-process, with real sessions (admin A, editors E1 and E2, viewer V), anonymous, and the per-run
install token:
  1. ownership: an upload by a signed-in editor records them (DB, item JSON, item page ORIGIN, MCP
     get); a token upload and an MCP upload record NULL (admin-owned); a card, its write-up, a hobby
     and a blog entry record their creator; the one-time backfill rebuilds what it can from the
     audit log and counts it;
  2. the sensitive matrix: E1 marks their own file; then for E1 (the uploader), E2, V, anonymous, A and
     the token: the item page, the item API, /f, the thumbnail, a search hit, a browse listing (the
     gallery and Unfiled), the processing drawer, the Curator queue, and the MCP (as the token);
  3. who may flip it: E2 can't clear it (403), an admin can, a viewer can't mark; undo restores it, and
     an editor can't undo their way to an unmark (403); bulk marking is one batch;
  4. the upload-time checkbox: the item is never visible to V, not even right after the upload;
  5. a restricted TYPE uploaded by E1 follows the uploader rule on direct doors and stays out of browsing;
  6. never offered to a captioning agent (list_needs_caption), never exported (project zip);
  7. the view log: E1, A and the token each open the flagged item once -> three rows; an ordinary item
     gets none; folding; Admin's restricted list carries the reason and the access summary.
Exits 1 if any check fails.
"""

import io
import json
import os
import re
import secrets
import sqlite3
import sys
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("ownsens-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import access_log, actor, blog, cards, db, hobbies, policy, users  # noqa: E402
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


def new_client(**headers):
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False, headers=headers or None)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def png_bytes(seed):
    from PIL import Image
    img = Image.new("RGB", (32, 24), (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def err_code(r):
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("error", {}).get("code") if isinstance(body, dict) else None


def row(slug):
    return db.get_by_slug(slug)


db.init_db()
users.limiter.reset()

# ---- users -------------------------------------------------------------------------------------
print("--- users ---")
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("os_admin", "admin"), ("os_ed1", "editor"), ("os_ed2", "editor"), ("os_view", "viewer")):
        PW[name] = "os-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
UID = {n: db.get_user(username=n)["id"] for n in PW}
C = {"anonymous": new_client(), "token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for key, name in (("A", "os_admin"), ("E1", "os_ed1"), ("E2", "os_ed2"), ("V", "os_view")):
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    check(f"{key} signs in", r.status_code == 200, r.text[:200])
    C[key] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in ("A", "E1", "E2", "V") else dict(SAME)) for k, c in C.items()}


def upload(who, filename, data, sensitive=False, mime="image/png"):
    fields = {"description": f"ownsens {filename}"}
    if sensitive:
        fields["sensitive"] = "true"
    return C[who].post("/api/upload", files={"file": (filename, data, mime)}, data=fields, headers=H[who])


# ---- 1. ownership ------------------------------------------------------------------------------
print("--- 1. ownership ---")
r = upload("E1", "ownsens-e1-photo.png", png_bytes(1))
check("E1 uploads (200)", r.status_code == 200, r.text[:200])
E1_ITEM = r.json()["slug"]
check("DB: uploaded_by_user_id = E1", row(E1_ITEM)["uploaded_by_user_id"] == UID["os_ed1"], row(E1_ITEM)["uploaded_by_user_id"])
j = C["A"].get(f"/api/image/{E1_ITEM}").json()
check("item JSON: uploaded_by_user = E1 (and the Source label untouched)",
      (j.get("uploaded_by_user") or {}).get("username") == "os_ed1" and isinstance(j.get("uploaded_by"), str), j.get("uploaded_by_user"))
page = C["A"].get(f"/object/{E1_ITEM}").text
check("item page ORIGIN: Uploaded by os_ed1", re.search(r"Uploaded by</dt><dd>\s*os_ed1", page) is not None)
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)
with actor.acting_as(actor.ACTOR_MCP):
    g = mcp.constructicon_get(E1_ITEM)
check("MCP get: uploaded_by_user = E1", (g.get("uploaded_by_user") or {}).get("id") == UID["os_ed1"], g.get("uploaded_by_user"))

r = upload("token", "ownsens-token.png", png_bytes(2))
TOKEN_ITEM = r.json()["slug"]
check("token upload: uploaded_by_user_id NULL (admin-owned)", row(TOKEN_ITEM)["uploaded_by_user_id"] is None)
check("token upload: JSON uploaded_by_user null", r.json().get("uploaded_by_user") is None)
import base64  # noqa: E402
with actor.acting_as(actor.ACTOR_MCP):
    m = mcp.constructicon_upload("ownsens-mcp.png", base64.b64encode(png_bytes(3)).decode())
check("MCP upload: uploaded_by_user_id NULL", row(m["slug"])["uploaded_by_user_id"] is None)
MCP_ITEM = m["slug"]

r = C["E1"].post("/api/projects", data={"title": "Ownsens E1 Card"}, headers=H["E1"])
check("E1 creates a card (200)", r.status_code == 200, r.text[:200])
CARD = db.get_project(r.json()["slug"] if "slug" in r.json() else r.json()["id"])
check("card: created_by_user_id = E1", CARD["created_by_user_id"] == UID["os_ed1"], CARD.get("created_by_user_id"))
check("card's write-up: uploaded_by_user_id = E1", row(CARD["writeup_slug"])["uploaded_by_user_id"] == UID["os_ed1"])
check("card page ORIGIN: Created by os_ed1",
      re.search(r"Created by</dt><dd>\s*os_ed1", C["A"].get(f"/project/{CARD['slug']}").text) is not None)
with actor.acting_as(actor.ACTOR_MCP):
    gp = mcp.constructicon_get_project(CARD["slug"])
check("MCP get_project: created_by_user = E1", (gp.get("created_by_user") or {}).get("username") == "os_ed1",
      gp.get("created_by_user"))
TOKEN_CARD = C["token"].post("/api/projects", data={"title": "Ownsens Token Card"}).json()
check("token card: created_by_user_id NULL", db.get_project(TOKEN_CARD["id"])["created_by_user_id"] is None)
with actor.acting_as("user:os_ed1"):
    HOBBY = hobbies.create("Ownsens Hobby").data["hobby"]
    ENTRY = blog.create("Ownsens entry").data["entry"]
conn = sqlite3.connect(db.DB_PATH)
conn.row_factory = sqlite3.Row
check("hobby: hobby_settings.created_by_user_id = E1",
      conn.execute("SELECT created_by_user_id FROM hobby_settings WHERE hobby_tag_id = ?", (HOBBY["id"],)).fetchone()[0]
      == UID["os_ed1"])
check("blog entry: created_by_user_id = E1",
      conn.execute("SELECT created_by_user_id FROM blog_entries WHERE id = ?", (ENTRY["id"],)).fetchone()[0] == UID["os_ed1"])
with actor.acting_as(actor.ACTOR_MCP):
    TOKEN_HOBBY = hobbies.create("Ownsens Token Hobby").data["hobby"]
check("MCP hobby: no creator row", conn.execute("SELECT COUNT(*) FROM hobby_settings WHERE hobby_tag_id = ? "
                                                "AND created_by_user_id IS NOT NULL", (TOKEN_HOBBY["id"],)).fetchone()[0] == 0)

# the backfill: forget the recorded owners, rebuild them from the audit log
conn.execute("UPDATE capture_events SET uploaded_by_user_id = NULL")
conn.execute("UPDATE projects SET created_by_user_id = NULL")
conn.execute("UPDATE hobby_settings SET created_by_user_id = NULL")
conn.execute("UPDATE blog_entries SET created_by_user_id = NULL")
conn.commit()
bconn = db.get_conn()
counts = db._ownership_backfill(bconn)
bconn.commit()
bconn.close()
check("backfill counts: 1 item, 1 card (+ its write-up), 1 hobby, 1 blog entry",
      counts == {"items": 1, "cards": 1, "card_writeups": 1, "hobbies": 1, "blog_entries": 1}, counts)
check("backfill: E1's upload is E1's again; the token's and the MCP's stay NULL",
      row(E1_ITEM)["uploaded_by_user_id"] == UID["os_ed1"] and row(TOKEN_ITEM)["uploaded_by_user_id"] is None
      and row(MCP_ITEM)["uploaded_by_user_id"] is None)
check("backfill: E1's card is E1's again; the token's stays NULL",
      db.get_project(CARD["id"])["created_by_user_id"] == UID["os_ed1"]
      and db.get_project(TOKEN_CARD["id"])["created_by_user_id"] is None)
bconn = db.get_conn()
again = db._ownership_backfill(bconn)
bconn.commit()
bconn.close()
check("backfill is idempotent (second run changes nothing)", sum(again.values()) == 0, again)
check("the migration is registered", "ownership_backfill_604" in [n for n, _ in db.MIGRATIONS])

# ---- 2. the sensitive matrix ---------------------------------------------------------------------
print("--- 2. sensitive matrix ---")
r = C["E1"].post(f"/api/image/{E1_ITEM}/sensitive", data={"sensitive": "true"}, headers=H["E1"])
check("E1 marks their own file sensitive (200)", r.status_code == 200 and r.json()["changed"] == [E1_ITEM], r.text[:200])
MARK_BATCH = r.json()["batch_id"]
fr = row(E1_ITEM)
check("DB: sensitive=1, sensitive_by=user:os_ed1", fr["sensitive"] == 1 and fr["sensitive_by"] == "user:os_ed1")
log_row = conn.execute("SELECT op, actor FROM audit_log WHERE batch_id = ?", (MARK_BATCH,)).fetchone()
check("change log: one item_sensitive row by user:os_ed1", tuple(log_row) == ("item_sensitive", "user:os_ed1"), tuple(log_row or ()))

with actor.acting_as(actor.ACTOR_SCRIPT):
    db.add_pending_decision("retype", E1_ITEM, {"question": "ownsens question", "options": [{"key": "image", "label": "Image"}]})

MATRIX_WHO = ["E1", "E2", "V", "anonymous", "A", "token"]
EXPECT_SEE = {"E1": True, "E2": False, "V": False, "anonymous": False, "A": True, "token": True}


def sees(who):
    c = C[who]
    out = {}
    r = c.get(f"/object/{E1_ITEM}")
    out["page"] = r.status_code == 200
    r = c.get(f"/api/image/{E1_ITEM}")
    out["api"] = r.status_code == 200
    r = c.get(f"/f/{E1_ITEM}")
    out["file"] = r.status_code == 200
    r = c.get(f"/f/{E1_ITEM}/thumb")
    out["thumb"] = r.status_code == 200
    r = c.get("/api/search?query=ownsens-e1-photo")
    out["search"] = r.status_code == 200 and E1_ITEM in r.text
    r = c.get("/api/gallery?query=ownsens")
    out["gallery"] = r.status_code == 200 and E1_ITEM in r.text
    r = c.get("/unfiled")
    out["unfiled"] = r.status_code == 200 and E1_ITEM in r.text
    r = c.get(f"/api/processing?session={E1_ITEM}")
    out["processing"] = r.status_code == 200 and E1_ITEM in r.text
    r = c.get("/api/curator/queue")
    out["curator"] = r.status_code == 200 and E1_ITEM in r.text
    return out


MATRIX = {}
for who in MATRIX_WHO:
    MATRIX[who] = sees(who)
    want = EXPECT_SEE[who]
    wrong = [k for k, v in MATRIX[who].items() if v != want]
    check(f"matrix: {who} {'sees' if want else 'does not see'} it on every door", not wrong, f"wrong doors: {wrong}")
# denials answer 404 (not 403) for a signed-in user, so existence isn't leaked
check("matrix: E2 page / API / file / thumb answer 404",
      all(C["E2"].get(p).status_code == 404 for p in (f"/object/{E1_ITEM}", f"/api/image/{E1_ITEM}", f"/f/{E1_ITEM}", f"/f/{E1_ITEM}/thumb")))
check("matrix: anonymous /f and thumb answer 404",
      C["anonymous"].get(f"/f/{E1_ITEM}").status_code == 404 and C["anonymous"].get(f"/f/{E1_ITEM}/thumb").status_code == 404)
with actor.acting_as(actor.ACTOR_MCP):
    mg = mcp.constructicon_get(E1_ITEM)
    ms = mcp.constructicon_search("ownsens-e1-photo")
    md = mcp.constructicon_download(E1_ITEM)
check("matrix: MCP (token) get / search / download see it",
      mg.get("slug") == E1_ITEM and mg.get("sensitive") is True and E1_ITEM in json.dumps(ms) and bool(md.get("content_base64")))
MATRIX["token"]["mcp"] = mg.get("slug") == E1_ITEM
# Every MCP tool runs as `mcp` (admin) until per-user tokens (#604 step 5); the policy itself is
# what those tokens will consult, so check it directly for the other actors.
check("policy.can_view: E1 yes, E2 / V / anonymous no, mcp / token yes",
      [policy.can_view(row(E1_ITEM), a) for a in ("user:os_ed1", "user:os_ed2", "user:os_view", "anonymous", "mcp", "token")]
      == [True, False, False, False, True, True])
check("an ordinary item is untouched for V (page 200)", C["V"].get(f"/object/{TOKEN_ITEM}").status_code == 200)

# ---- 3. who may flip it, undo, bulk --------------------------------------------------------------
print("--- 3. flipping, undo, bulk ---")
r = C["E2"].post(f"/api/image/{E1_ITEM}/sensitive", data={"sensitive": "false"}, headers=H["E2"])
check("E2 clearing -> 404 not_found (E2 can't see E1's sensitive item, so it doesn't exist for them)",
      r.status_code == 404 and err_code(r) == "not_found", f"{r.status_code} {r.text[:120]}")
check("can_unmark: the uploader E1 and an admin yes; E2 and V no (owner decision on #603)",
      [policy.can_unmark_sensitive(a, row(E1_ITEM)) for a in ("user:os_ed1", "user:os_admin", "user:os_ed2", "user:os_view")]
      == [True, True, False, False])
r = C["E1"].post(f"/api/image/{E1_ITEM}/sensitive", data={"sensitive": "false"}, headers=H["E1"])
check("E1 (the uploader) clearing -> 200", r.status_code == 200 and row(E1_ITEM)["sensitive"] == 0, f"{r.status_code} {r.text[:120]}")
r = C["E1"].post(f"/api/changes/{r.json()['batch_id']}/undo", headers=H["E1"])
check("E1 undoes their own clear (re-locks) -> 200", r.status_code == 200 and row(E1_ITEM)["sensitive"] == 1, f"{r.status_code} {r.text[:120]}")
r = C["V"].post(f"/api/image/{TOKEN_ITEM}/sensitive", data={"sensitive": "true"}, headers=H["V"])
check("V marking -> 403 (editor route)", r.status_code == 403, r.status_code)
r = C["E2"].post(f"/api/changes/{MARK_BATCH}/undo", headers=H["E2"])
check("E2 undoing the mark (= an unmark) -> 403 forbidden, still flagged",
      r.status_code == 403 and row(E1_ITEM)["sensitive"] == 1, f"{r.status_code} {r.text[:150]}")
r = C["A"].post(f"/api/image/{E1_ITEM}/sensitive", data={"sensitive": "false"}, headers=H["A"])
check("A clears it (200)", r.status_code == 200 and r.json()["sensitive"] is False, r.text[:200])
UNMARK_BATCH = r.json()["batch_id"]
check("after A clears it: V sees it again (page, search)", C["V"].get(f"/object/{E1_ITEM}").status_code == 200
      and E1_ITEM in C["V"].get("/api/search?query=ownsens-e1-photo").text)
r = C["E2"].post(f"/api/changes/{UNMARK_BATCH}/undo", headers=H["E2"])
check("undo of the unmark (re-locks; any editor) -> 200, flagged again",
      r.status_code == 200 and row(E1_ITEM)["sensitive"] == 1 and row(E1_ITEM)["sensitive_by"] == "user:os_ed1",
      f"{r.status_code} {r.text[:150]}")
check("after the undo: V can't see it", C["V"].get(f"/object/{E1_ITEM}").status_code == 404)
r = C["A"].post(f"/api/changes/{MARK_BATCH}/undo", headers=H["A"])
check("A (admin) may undo the original mark -> 200, cleared",
      r.status_code == 200 and row(E1_ITEM)["sensitive"] == 0, f"{r.status_code} {r.text[:120]}")
r = C["E1"].post(f"/api/image/{E1_ITEM}/sensitive", data={"sensitive": "true"}, headers=H["E1"])
check("E1 marks it again (200)", r.status_code == 200 and row(E1_ITEM)["sensitive"] == 1, r.text[:150])

b1 = upload("E1", "ownsens-bulk-1.png", png_bytes(5)).json()["slug"]
b2 = upload("E1", "ownsens-bulk-2.png", png_bytes(6)).json()["slug"]
r = C["E2"].post("/api/bulk/sensitive", data={"slugs": [b1, b2], "sensitive": "true"}, headers=H["E2"])
check("E2 bulk-marks E1's two files (one batch)", r.status_code == 200 and r.json()["count"] == 2
      and conn.execute("SELECT COUNT(*) FROM audit_log WHERE batch_id = ?", (r.json()["batch_id"],)).fetchone()[0] == 1,
      r.text[:200])
check("bulk: E2 (not the uploader) now can't see them; E1 can",
      C["E2"].get(f"/api/image/{b1}").status_code == 404 and C["E1"].get(f"/api/image/{b1}").status_code == 200)
r = C["E2"].post("/api/bulk/sensitive", data={"slugs": [b1], "sensitive": "true"}, headers=H["E2"])
check("bulk on an item E2 can no longer see -> 404 not_found", r.status_code == 404, r.status_code)
with actor.acting_as(actor.ACTOR_MCP):
    out = mcp.constructicon_set_sensitive(b2, False)
check("MCP set_sensitive(False) as the token (admin) clears it", out.get("sensitive") is False and row(b2)["sensitive"] == 0, out)

# ---- 4. the upload-time checkbox -----------------------------------------------------------------
print("--- 4. upload-time flag ---")
r = upload("E1", "ownsens-locked-at-birth.png", png_bytes(7), sensitive=True)
LOCKED = r.json()["slug"]
check("upload with the checkbox: flagged in the INSERT (sensitive=1, by user:os_ed1)",
      row(LOCKED)["sensitive"] == 1 and row(LOCKED)["sensitive_by"] == "user:os_ed1")
check("right after the upload, V gets 404 on the API and the page",
      C["V"].get(f"/api/image/{LOCKED}").status_code == 404 and C["V"].get(f"/object/{LOCKED}").status_code == 404)
check("right after the upload, V's processing drawer and search don't name it",
      LOCKED not in C["V"].get("/api/processing").text and LOCKED not in C["V"].get("/api/search?query=ownsens-locked").text)
check("no change-log row was needed for a flag set at birth (no item_sensitive row names it)",
      conn.execute("SELECT COUNT(*) FROM audit_log WHERE op = 'item_sensitive' AND affected_slugs LIKE ?",
                   (f'%{LOCKED}%',)).fetchone()[0] == 0)
r = upload("V", "ownsens-viewer.png", png_bytes(8))
check("(viewer can't upload at all: 403)", r.status_code == 403, r.status_code)

# ---- 5. a restricted TYPE with a known uploader ---------------------------------------------------
print("--- 5. restricted type + uploader ---")
pem = (b"-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUOwnsensFixtureNotARealCert0wCgYIKoZIzj0EAwIw\n"
       b"-----END CERTIFICATE-----\n")
r = upload("E1", "ownsens-cert.pem", pem, mime="application/x-pem-file")
CERT = r.json().get("slug") if r.status_code == 200 else None
if CERT and policy.is_type_restricted(row(CERT)):
    check("type-restricted, uploader E1: direct doors serve it to E1", C["E1"].get(f"/api/image/{CERT}").status_code == 200)
    check("type-restricted: E2 gets 404", C["E2"].get(f"/api/image/{CERT}").status_code == 404)
    check("type-restricted: still out of browsing even for its uploader and an admin (unchanged rule)",
          CERT not in C["E1"].get("/api/search?query=ownsens-cert").text and CERT not in C["A"].get("/api/search?query=ownsens-cert").text)
else:
    check("(the PEM fixture became a restricted type)", False, f"{r.status_code} {r.text[:150]}")

# ---- 6. captions agent and exports -----------------------------------------------------------------
print("--- 6. agent + exports ---")
with actor.acting_as(actor.ACTOR_MCP):
    nc = mcp.constructicon_list_needs_caption(limit=500)
check("list_needs_caption (as the admin MCP) never offers a flagged item", E1_ITEM not in json.dumps(nc)
      and LOCKED not in json.dumps(nc))
with actor.acting_as("user:os_ed1"):
    from core import membership  # noqa: E402
    membership.add_files(CARD["id"], [E1_ITEM, TOKEN_ITEM], **membership.UI_EFFECTS)
zr = C["A"].get(f"/api/projects/{CARD['id']}/export.zip")
manifest = zipfile.ZipFile(io.BytesIO(zr.content)).read("manifest.json").decode() if zr.status_code == 200 else ""
check("project zip (as admin) excludes the flagged item, keeps the ordinary one",
      E1_ITEM not in manifest and TOKEN_ITEM in manifest, zr.status_code)
check("card page: E2 doesn't see the flagged file, E1 does",
      E1_ITEM not in C["E2"].get(f"/project/{CARD['slug']}").text and E1_ITEM in C["E1"].get(f"/project/{CARD['slug']}").text)

# ---- 7. the view log ------------------------------------------------------------------------------
print("--- 7. view log ---")
conn.execute("DELETE FROM item_access_log")
conn.commit()
for who in ("E1", "A", "token"):
    C[who].get(f"/object/{E1_ITEM}")
rows = conn.execute("SELECT actor, how FROM item_access_log WHERE slug = ? ORDER BY id", (E1_ITEM,)).fetchall()
check("E1, A and the token each open it once -> three rows (who + how)",
      [tuple(r) for r in rows] == [("user:os_ed1", "page"), ("user:os_admin", "page"), ("token", "page")],
      [tuple(r) for r in rows])
C["V"].get(f"/object/{TOKEN_ITEM}")
C["A"].get(f"/object/{TOKEN_ITEM}")
check("an ordinary item is never logged", conn.execute("SELECT COUNT(*) FROM item_access_log WHERE slug = ?",
                                                        (TOKEN_ITEM,)).fetchone()[0] == 0)
C["E2"].get(f"/object/{E1_ITEM}")
check("a refused look (E2, 404) isn't logged", conn.execute("SELECT COUNT(*) FROM item_access_log WHERE actor = ?",
                                                            ("user:os_ed2",)).fetchone()[0] == 0)
C["E1"].get(f"/object/{E1_ITEM}")
check("the same person through the same door within the fold window: still one row",
      conn.execute("SELECT COUNT(*) FROM item_access_log WHERE slug = ? AND actor = 'user:os_ed1' AND how = 'page'",
                   (E1_ITEM,)).fetchone()[0] == 1)
C["E1"].get(f"/f/{E1_ITEM}")
C["E1"].get(f"/f/{E1_ITEM}/thumb")
with actor.acting_as(actor.ACTOR_MCP):
    mcp.constructicon_get(E1_ITEM)
    mcp.constructicon_download(E1_ITEM)
hows = {r[0] for r in conn.execute("SELECT DISTINCT how FROM item_access_log WHERE slug = ?", (E1_ITEM,))}
check("file, thumb, MCP get and download are logged too", {"file", "thumb", "mcp_get", "mcp_download"} <= hows, hows)
r = C["A"].get(f"/api/image/{E1_ITEM}/access-log")
check("admin access-log API lists them (who labels)", r.status_code == 200 and len(r.json()["entries"]) >= 6
      and r.json()["reason"]["kind"] == "flag", r.text[:200])
check("access-log API is admin only (E1: 403)", C["E1"].get(f"/api/image/{E1_ITEM}/access-log").status_code == 403)
check("the item page shows the access log to an admin, not to the uploader",
      "Opened by" in C["A"].get(f"/object/{E1_ITEM}").text and "Opened by" not in C["E1"].get(f"/object/{E1_ITEM}").text)
rl = C["A"].get("/api/restricted").json()
mine = next((i for i in rl["items"] if i["slug"] == E1_ITEM), None)
check("Admin's restricted list: the flagged item with reason 'marked by os_ed1 on ...' and an access summary",
      mine is not None and mine["reason"].startswith("marked by os_ed1") and mine["opened"]["count"] >= 6, mine and mine.get("reason"))
if CERT:
    c_item = next((i for i in rl["items"] if i["slug"] == CERT), None)
    check("Admin's restricted list: the certificate with reason 'type: ...'", c_item is not None
          and c_item["reason"].startswith("type:"), c_item and c_item.get("reason"))
conn.close()

print()
print("MATRIX (True = sees it):")
doors = list(MATRIX["E1"].keys())
print("who".ljust(10) + " ".join(d.ljust(10) for d in doors))
for who in MATRIX_WHO:
    print(who.ljust(10) + " ".join(str(MATRIX[who].get(d, "")).ljust(10) for d in doors))
print()
print(f"{len(FAILS)} failure(s)" if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
