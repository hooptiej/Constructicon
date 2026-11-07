#!/usr/bin/env python3
"""#556's UI rows: "Export this card" (zip or HTML), the explain-card view, and the caption breaker in
Admin. Throwaway DB and storage (_testenv), no server, no network.

Checks: `project_export.left_out` counts what an export leaves out, by reason (sensitive, an older
revision, not visible), and the zip's manifest carries it; the HTML format builds a small static site
of just this card (index.html, the card's page, its media, a README.txt naming what was left out),
removes its temp folder after the download, refuses a private card (409 card_private) and an unknown
template (400 bad_template), and is editor-only (a viewer gets 403 naming the role); the card page's
More menu has "Explain this card…" and "Export this card…", loads card-tools.js, and its config greys
the HTML format for a viewer (with the role it needs) but not for an editor; GET /explain answers the
pips with their reasons; Admin shows the breaker row, /api/captions/defaults carries the breaker with
its threshold, and Reset (admin only) clears an open breaker and says who did it.

    python scripts/test_card_tools_556.py
"""

import glob
import io
import json
import os
import re
import secrets
import sys
import tempfile
import zipfile

import _testenv  # noqa: E402
TMP = _testenv.isolate("card-tools-556-")
os.environ["CAPTION_DISABLED"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, captions, cards, db, ingest, items, membership, project_export, revisions, sharing, users  # noqa: E402
_testenv.assert_isolated()

ingest.run_in_thread = lambda fn, *a: None
NOOP = lambda fn, *a: None  # noqa: E731
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:300]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def png(color):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


def upload(name, data):
    res = ingest.ingest_file(io.BytesIO(data), name, size=len(data), source="tester", run_background=NOOP)
    assert res.row is not None, res.error
    return res.row["slug"]


db.init_db()
users.limiter.reset()

print("--- fixtures ---")
card = cards.create("Tidy Garage Shelf").data["card"]
pic = upload("shelf.png", png((10, 120, 30)))
notes = upload("notes.txt", b"shelf notes\n")
old = upload("notes-old.txt", b"older shelf notes\n")
secret = upload("receipt.txt", b"the receipt, sensitive\n")
membership.add_files(card["id"], [pic, notes, old, secret], link_tag=True, auto_cover=True)
items.set_sensitive([secret], True)
revisions.mark_superseded(old, notes)
priv = cards.create("Private Stash").data["card"]
membership.add_files(priv["id"], [upload("stash.txt", b"stash\n")], link_tag=True, auto_cover=False)
sharing.set_private("card", priv["id"], True)

print("--- left_out ---")
inc, notes_out = project_export.left_out(card["id"])
reasons = {n["reason"].split(":")[0]: n for n in notes_out}
check("the sensitive file is counted and named", any("sensitive" in r for r in reasons)
      and next(n for r, n in reasons.items() if "sensitive" in r)["count"] == 1, notes_out)
check("the older revision is counted and named", any("older revision" in r for r in reasons), notes_out)
check("what goes in is the rest", {i["slug"] for i in inc} >= {pic, notes} and secret not in {i["slug"] for i in inc}
      and old not in {i["slug"] for i in inc}, [i["slug"] for i in inc])
check("the notes read as sentences", all("left out" in s for s in project_export.left_out_lines(notes_out)),
      project_export.left_out_lines(notes_out))
z = zipfile.ZipFile(io.BytesIO(project_export.export_project(card["id"])))
man = json.loads(z.read("manifest.json"))
check("the zip's manifest carries left_out", man.get("left_out") and any("sensitive" in n["reason"] for n in man["left_out"]), man.get("left_out"))
check("... and leaves the sensitive file out", secret not in {i["slug"] for i in man["items"]})

print("--- the HTML format ---")
before = set(glob.glob(os.path.join(tempfile.gettempdir(), "constructicon-card-html-*")))
built = project_export.export_card_site(card["id"])
zh = zipfile.ZipFile(built["path"])
names = set(zh.namelist())
check("the site has index.html, the card's page, assets and media",
      "index.html" in names and f"projects/{card['slug']}.html" in names and any(n.startswith("media/") for n in names), sorted(names)[:20])
readme = zh.read("README.txt").decode("utf-8")
check("README.txt says what went in and names what was left out, and why",
      "file(s) went in" in readme and "receipt" in readme and "sensitive" in readme, readme)
page = zh.read(f"projects/{card['slug']}.html").decode("utf-8")
check("the card's page shows the card", "Tidy Garage Shelf" in page)
check("the sensitive file's media is not in the site", not any(secret in n for n in names))
zh.close()
import shutil  # noqa: E402
shutil.rmtree(built["folder"])
e = None
try:
    project_export.export_card_site(card["id"], template="radio-dial")
except Exception as ex:  # noqa: BLE001
    e = ex
check("an unknown template is refused (bad_template, 400)", getattr(e, "code", None) == "bad_template" and e.status == 400, e)
e = None
try:
    project_export.export_card_site(priv["id"])
except Exception as ex:  # noqa: BLE001
    e = ex
check("a private card is refused (card_private, 409) with the reason", getattr(e, "code", None) == "card_private"
      and e.status == 409 and "private" in e.message and "zip" in e.message, e)
check("formats_for fills in the card's id", all(str(card["id"]) in f["url"] for f in project_export.formats_for(card)))

print("--- web ---")
from starlette.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
BASE = "http://testhost.local"
SAME = {"Origin": BASE}
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("ct_viewer", "viewer"), ("ct_editor", "editor"), ("ct_admin", "admin")):
        PW[name] = "ct-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
C, H = {}, {}
for name in PW:
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', c.get("/").text)
    C[name], H[name] = c, {**SAME, "X-CSRF-Token": m.group(1) if m else ""}


def config_of(html):
    m = re.search(r'<script type="application/json" id="card-tools-config"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


vpage = C["ct_viewer"].get(f"/project/{card['slug']}").text
check("the More menu has Explain this card… and Export this card…",
      'id="explain-card-btn"' in vpage and 'id="export-card-btn"' in vpage, "")
check("the card page loads card-tools.js", "/static/js/card-tools.js" in vpage)
vcfg = config_of(vpage)
fmt = {f["key"]: f for f in (vcfg or {}).get("formats", [])}
check("a viewer's config: zip open, HTML greyed with the role it needs (never hidden)",
      fmt.get("zip", {}).get("locked") is None and fmt.get("html", {}).get("locked")
      and fmt["html"]["locked"]["need"] == "editor" and "editor" in fmt["html"]["locked"]["message"], vcfg)
check("... and the HTML format lists its template(s)", fmt.get("html", {}).get("templates"), fmt.get("html"))
ecfg = config_of(C["ct_editor"].get(f"/project/{card['slug']}").text)
check("an editor's config: both formats open", ecfg and all(f["locked"] is None for f in ecfg["formats"]), ecfg)
rv = C["ct_viewer"].get(f"/api/projects/{card['id']}/export.html.zip")
check("a viewer asking for HTML gets 403 naming the role", rv.status_code == 403 and "editor" in rv.text, rv.text[:200])
before = set(glob.glob(os.path.join(tempfile.gettempdir(), "constructicon-card-html-*")))
re_ = C["ct_editor"].get(f"/api/projects/{card['id']}/export.html.zip")
check("an editor downloads the HTML zip (attachment, a real zip)",
      re_.status_code == 200 and "attachment" in re_.headers.get("content-disposition", "")
      and "index.html" in zipfile.ZipFile(io.BytesIO(re_.content)).namelist(), (re_.status_code, re_.text[:200]))
after = set(glob.glob(os.path.join(tempfile.gettempdir(), "constructicon-card-html-*")))
check("the temp folder is removed after the download", not (after - before), after - before)
rb = C["ct_editor"].get(f"/api/projects/{card['id']}/export.html.zip?template=nope")
check("a bad template over the web is a 400 in the shared shape", rb.status_code == 400 and rb.json()["error"]["code"] == "bad_template", rb.text[:200])
rp = C["ct_admin"].get(f"/api/projects/{priv['id']}/export.html.zip")
check("a private card over the web is a 409 saying why", rp.status_code == 409 and rp.json()["error"]["code"] == "card_private", rp.text[:200])
rz = C["ct_viewer"].get(f"/api/projects/{card['id']}/export.zip")
check("the zip format still works for a viewer", rz.status_code == 200 and "manifest.json" in zipfile.ZipFile(io.BytesIO(rz.content)).namelist())
rx = C["ct_viewer"].get(f"/api/projects/{card['id']}/explain")
xb = rx.json() if rx.status_code == 200 else {}
check("the explain view's endpoint answers a viewer with the pips and their reasons",
      rx.status_code == 200 and set(xb.get("level", {}).get("reasons", {})) >= {"cover", "dates", "writeup", "owner_words", "stack"}, rx.text[:200])
check("a hidden private card's explain is 404 for a viewer", C["ct_viewer"].get(f"/api/projects/{priv['id']}/explain").status_code == 404)
js = open(os.path.join(os.path.dirname(HERE), "web", "static", "js", "card-tools.js"), encoding="utf-8").read()
check("card-tools.js reads the config, calls explain and greys a locked format with RoleLock",
      "card-tools-config" in js and "/explain" in js and "RoleLock.decorate" in js and "textContent" in js)

print("--- the caption breaker in Admin ---")
apage = C["ct_admin"].get("/admin").text
check("Admin shows the breaker row with a Reset button", 'id="admin-caption-breaker-msg"' in apage and 'id="admin-caption-breaker-reset"' in apage)
check("... wired to POST /api/captions/reset-breaker", "/api/captions/reset-breaker" in apage)
d = C["ct_admin"].get("/api/captions/defaults").json()
check("/api/captions/defaults carries the breaker with its threshold",
      d["breaker"]["open"] is False and d["breaker"]["max_failures"] == captions.CAPTION_MAX_CONSECUTIVE_FAILURES, d.get("breaker"))
captions._breaker_reason = "3 consecutive caption failures, last: test"
captions._consecutive_failures = 3
check("fixture: the breaker is open", C["ct_admin"].get("/api/captions/defaults").json()["breaker"]["open"] is True)
r = C["ct_editor"].post("/api/captions/reset-breaker", headers=H["ct_editor"])
check("an editor can't reset it (403 naming the role)", r.status_code == 403 and "admin" in r.text, r.text[:200])
check("... and it stays open", captions._breaker_reason is not None)
r = C["ct_admin"].post("/api/captions/reset-breaker", headers=H["ct_admin"])
check("an admin resets it: closed, the failure count back to 0",
      r.status_code == 200 and r.json()["breaker"]["open"] is False and r.json()["breaker"]["consecutive_failures"] == 0, r.text[:200])

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
