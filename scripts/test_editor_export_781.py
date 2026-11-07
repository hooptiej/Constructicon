#!/usr/bin/env python3
"""Bring your own keys, scope (a) (#781): editors get their own export path. Throwaway DB, storage and
exports (_testenv), no server, no network.

Checks: the sandbox (an admin's is the install's, an editor's is exports/users/<id>, keyed by number,
never leaving exports/); a viewer has none; what goes in (`plan`): with nothing picked only the
builder's own in-progress cards and ready entries, another user's "held back: owned by <name>"; picked
by hand, any open card; never a private card ("you're a viewer on it" for a viewer member); hidden
cards counted, never named; an unknown slug named; the install token's default build keeps admin-owned
cards. Over the web: an editor builds (POST /api/export/build is editor now) into their own sandbox with
the left-out list; the install's build is untouched; one build per sandbox (409 export_busy saying when
it started) but another user's build goes ahead; both previews survive; one user can't open another's
preview (403 saying why), an admin can; no build yet / a bad id are clear refusals; the newest two builds
are kept per sandbox; the config is per sandbox; /api/export/sandbox lists the candidates with owners and
reasons; publishing and the targets stay admin; the drawer is an editor's now, with Publish greyed and
explained for an editor and live for an admin.

    python scripts/test_editor_export_781.py
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _testenv  # noqa: E402
TMP = _testenv.isolate("editor-export-781-")
os.environ["CAPTION_DISABLED"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

import io  # noqa: E402

from core import actor, blog, cards, db, export_sandbox, ingest, items, membership, paths, sharing, users  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()

ingest.run_in_thread = lambda fn, *a: None
NOOP = lambda fn, *a: None  # noqa: E731
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:400]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def refusal(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e
    return None


def upload(name, data):
    res = ingest.ingest_file(io.BytesIO(data), name, size=len(data), source="tester", run_background=NOOP)
    assert res.row is not None, res.error
    return res.row["slug"]


def reasons(notes):
    return {n["reason"]: n for n in notes}


db.init_db()
users.limiter.reset()

PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("ex_admin", "admin"), ("ex_e1", "editor"), ("ex_e2", "editor"), ("ex_viewer", "viewer")):
        PW[name] = "ex-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role, display_name={"ex_e2": "Second Editor"}.get(name))
UID = {n: db.get_user(username=n)["id"] for n in PW}
E1, E2, ADM, VIEW = "user:ex_e1", "user:ex_e2", "user:ex_admin", "user:ex_viewer"

print("--- fixtures ---")
c_admin = cards.create("Admin Workbench", stage="in_progress").data["card"]  # created by a script: NULL = admin-owned
with actor.acting_as(E1):
    c_e1 = cards.create("Editor One Robot", stage="in_progress").data["card"]
    c_priv = cards.create("Editor One Secret Lab", stage="in_progress").data["card"]
    sharing.set_private("card", c_priv["id"], True)
    sharing.add_member("card", c_priv["id"], "ex_e2", "viewer")
    e1_note = upload("robot-notes.txt", b"robot notes\n")
    e1_secret = upload("robot-keys.txt", b"robot secret\n")
    membership.add_files(c_e1["id"], [e1_note, e1_secret], link_tag=True, auto_cover=False)
    entry = blog.create("Robot Diary", body="It moves.", status="ready").data["entry"]
items.set_sensitive([e1_secret], True)
with actor.acting_as(E2):
    c_e2 = cards.create("Editor Two Kite", stage="in_progress").data["card"]
check("fixture: ownership recorded", db.get_project(c_e1["id"])["created_by_user_id"] == UID["ex_e1"]
      and db.get_project(c_admin["id"])["created_by_user_id"] is None)

print("--- sandboxes ---")
sa = export_sandbox.sandbox_for(ADM)
check("an admin's sandbox is the install's (exports/current, /preview/)", sa.is_install and sa.preview_url == "/preview/"
      and sa.current == paths.current_export_dir())
s1 = export_sandbox.sandbox_for(E1)
check("an editor's sandbox is exports/users/<id>, previewed at /preview/u/<id>/",
      s1.key == f"user:{UID['ex_e1']}" and s1.root == (paths.exports_dir() / "users" / str(UID["ex_e1"])).resolve()
      and s1.preview_url == f"/preview/u/{UID['ex_e1']}/", s1)
check("the install token's sandbox is the install's", export_sandbox.sandbox_for("token").is_install)
e = refusal(export_sandbox.sandbox_for, VIEW)
check("a viewer has no sandbox (403 naming the role)", e is not None and e.status == 403 and "editor" in e.message)
for bad in ("../1", "-3", "0", "abc", "1/../../x"):
    e = refusal(export_sandbox.user_root, bad)
    check(f"a sandbox id {bad!r} is refused", e is not None and e.status == 400)

print("--- what goes in ---")
p = export_sandbox.plan({}, E1)
r = reasons(p["left_out"])
check("an editor's default build takes only their own in-progress cards", set(p["projects"]) == {c_e1["id"]}, list(p["projects"]))
check("... another user's card is held back, saying whose",
      any("held back: owned by Second Editor" in k and "Editor Two Kite" in v["names"] for k, v in r.items())
      and any("held back: owned by Admin (no user recorded)" in k for k in r), list(r))
check("... their own private card is left out (a site is meant to be shared)",
      any("private" in k and "Editor One Secret Lab" in v["names"] for k, v in r.items()), list(r))
check("... their own ready entry goes in", [x["slug"] for x in p["entries"]] == [entry["slug"]])
p = export_sandbox.plan({"project_slugs": [c_admin["slug"], c_e2["slug"], c_priv["slug"], "no-such-card"],
                         "blog_entry_slugs": []}, E1)
r = reasons(p["left_out"])
check("picked by hand, an editor's build takes any open card", set(p["projects"]) == {c_admin["id"], c_e2["id"]}, list(p["projects"]))
check("... still never a private card", any("private" in k for k in r), list(r))
check("... and an unknown slug is named", any("not found" in k and "no-such-card" in v["names"] for k, v in r.items()), list(r))
p2 = export_sandbox.plan({"project_slugs": [c_priv["slug"]]}, E2)
check("a viewer member of a private card is told they're a viewer on it",
      any("you're a viewer on it" in n["reason"] for n in p2["left_out"]), p2["left_out"])
pa = export_sandbox.plan({}, ADM)
check("an admin's default build takes admin-owned cards and holds back the editors'",
      set(pa["projects"]) == {c_admin["id"]}
      and sum(n["count"] for n in pa["left_out"] if n["reason"].startswith("card held back")) == 2, pa["left_out"])
check("... and the editor's ready entry too (theirs, not the admin's)", not pa["entries"]
      and any(n["reason"].startswith("blog entry held back") for n in pa["left_out"]), pa["left_out"])
pt = export_sandbox.plan({}, "token")
check("the install token's default build keeps admin-owned cards", set(pt["projects"]) == {c_admin["id"]}, list(pt["projects"]))
check("the admin's own explicit pick of an editor's card goes in", set(export_sandbox.plan(
    {"project_slugs": [c_e1["slug"]]}, ADM)["projects"]) == {c_e1["id"]})
pv = export_sandbox.plan({"project_slugs": [c_priv["slug"]]}, "user:ex_viewer")
check("a card the builder can't see is counted, never named",
      any(n["reason"] == "card not visible to you" and n["count"] == 1 and not n["names"] for n in pv["left_out"]), pv["left_out"])

print("--- web: builds ---")
from starlette.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
BASE = "http://testhost.local"
SAME = {"Origin": BASE}
C, H = {}, {}
for name in PW:
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', c.get("/").text)
    C[name], H[name] = c, {**SAME, "X-CSRF-Token": m.group(1) if m else "", "Content-Type": "application/json"}


def build(name, cfg=None):
    return C[name].post("/api/export/build", content=json.dumps(cfg or {}), headers=H[name])


install_index = paths.current_export_dir() / "index.html"
check("fixture: the install has no build yet", not install_index.exists())
rb = build("ex_e1")
body = rb.json() if rb.status_code == 200 else {}
check("an editor builds (200) into their own sandbox", rb.status_code == 200 and body.get("sandbox") == "user"
      and body.get("preview_url") == s1.preview_url and (s1.current / "index.html").is_file(), rb.text[:300])
check("... the report lists what was left out, and why (held back, private, a sensitive file)",
      any("owned by" in l for l in body.get("left_out_lines", [])) and any("private" in l for l in body.get("left_out_lines", []))
      and any("sensitive" in l and "robot-keys" in l for l in body.get("left_out_lines", [])), body.get("left_out_lines"))
check("... can't publish from there", body.get("can_publish") is False)
check("... and the install's build is untouched", not install_index.exists())
page = (s1.current / "projects" / f"{c_e1['slug']}.html").read_text(encoding="utf-8")
check("the editor's site has their card", "Editor One Robot" in page)
rv = build("ex_viewer")
check("a viewer can't build (403 naming the role)", rv.status_code == 403 and "editor" in rv.text, rv.text[:200])

s2 = export_sandbox.sandbox_for(E2)
with export_sandbox.build_lock(s1):
    busy = build("ex_e1")
    other = build("ex_e2")
check("a second build in the same sandbox is refused, saying when the first started",
      busy.status_code == 409 and busy.json()["error"]["code"] == "export_busy" and "started" in busy.json()["error"]["message"],
      busy.text[:300])
check("... while another user's build goes ahead", other.status_code == 200 and (s2.current / "index.html").is_file(), other.text[:200])
ra = build("ex_admin")
check("an admin's build is the install's, as before", ra.status_code == 200 and ra.json()["sandbox"] == "install"
      and install_index.is_file() and ra.json()["can_publish"] is True, ra.text[:200])
check("... and its default holds back the editors' cards, saying so",
      sum(1 for l in ra.json().get("left_out_lines", []) if "held back" in l) >= 1, ra.json().get("left_out_lines"))

print("--- web: previews ---")
u1, u2 = UID["ex_e1"], UID["ex_e2"]
check("both previews survive: the first editor's", C["ex_e1"].get(f"/preview/u/{u1}/index.html").status_code == 200)
check("... and the second's", C["ex_e2"].get(f"/preview/u/{u2}/index.html").status_code == 200)
check("... and the install's /preview/", C["ex_viewer"].get("/preview/index.html").status_code == 200)
x = C["ex_e1"].get(f"/preview/u/{u2}/index.html")
check("one user can't open another's preview (403 saying why)", x.status_code == 403
      and "another user's export sandbox" in x.json()["error"]["message"], x.text[:200])
check("a viewer can't open an editor's preview either", C["ex_viewer"].get(f"/preview/u/{u1}/index.html").status_code == 403)
check("an admin can", C["ex_admin"].get(f"/preview/u/{u1}/index.html").status_code == 200)
nb = C["ex_admin"].get(f"/preview/u/{UID['ex_viewer']}/index.html")
check("a sandbox with no build yet says so (404)", nb.status_code == 404 and "no build yet" in nb.json()["error"]["message"], nb.text[:200])
bad = C["ex_admin"].get("/preview/u/abc/index.html")
check("a preview id that isn't a number is a 400", bad.status_code == 400, bad.text[:200])
check("the preview's media/ get the safe-serving headers in a user sandbox too",
      all(C["ex_e1"].get(f"/preview/u/{u1}/media/{n}").headers.get("x-content-type-options") == "nosniff"
          for n in os.listdir(s1.current / "media")[:2]) if (s1.current / "media").is_dir() and os.listdir(s1.current / "media")
      else True)

print("--- retention, config ---")
for ts in ("20200101_000000", "20200102_000000", "20200103_000000"):
    (s1.root / ts).mkdir(parents=True, exist_ok=True)
build("ex_e1")
kept = sorted(d.name for d in s1.root.iterdir() if re.match(r"^\d{8}_\d{6}$", d.name))
check("the newest two builds are kept, per sandbox", len(kept) == 2 and "20200101_000000" not in kept, kept)
check("... and pruning one sandbox never touches another's", (s2.current / "index.html").is_file())
build("ex_e1", {"project_slugs": [c_e1["slug"]], "site": {"title": "E1 Site"}})
cfg1 = C["ex_e1"].get("/api/export/config").json()
cfga = C["ex_admin"].get("/api/export/config").json()
check("the saved config is per sandbox", cfg1.get("site", {}).get("title") == "E1 Site" and cfga.get("site", {}).get("title") != "E1 Site",
      (cfg1, cfga))

print("--- web: the drawer's lists, publish ---")
sb = C["ex_e1"].get("/api/export/sandbox").json()
cands = {c["slug"]: c for c in sb["candidates"]["cards"]}
check("/api/export/sandbox: the editor's own card is theirs and ticked by default",
      cands[c_e1["slug"]]["own"] and cands[c_e1["slug"]]["default"], cands.get(c_e1["slug"]))
check("... another user's shows its owner, unticked but pickable", cands[c_e2["slug"]]["owner"] == "Second Editor"
      and not cands[c_e2["slug"]]["default"] and cands[c_e2["slug"]]["ok"], cands.get(c_e2["slug"]))
check("... a private card is listed, not pickable, with why", c_priv["slug"] in cands and cands[c_priv["slug"]]["ok"] is False
      and "private" in cands[c_priv["slug"]]["why"], cands.get(c_priv["slug"]))
check("... the editor's entry is theirs and ticked", any(e_["slug"] == entry["slug"] and e_["default"] for e_ in sb["candidates"]["entries"]))
check("a viewer gets 403 from /api/export/sandbox", C["ex_viewer"].get("/api/export/sandbox").status_code == 403)
rp = C["ex_e1"].post("/api/export/publish", content=json.dumps({"target": "test"}), headers=H["ex_e1"])
check("publishing stays admin (403 naming the role)", rp.status_code == 403 and "admin" in rp.text, rp.text[:200])
check("the publish targets stay admin", C["ex_e1"].get("/api/export/targets").status_code == 403)

ep = C["ex_e1"].get("/").text
check("an editor's page carries the Export drawer now", 'id="export-builder"' in ep)
pub = re.search(r'<button[^>]*>Publish to Pages</button>', ep)
check("... with Publish drawn greyed and explained (never hidden)", pub is not None and "role-locked" in pub.group(0)
      and "data-locked-message" in pub.group(0) and "admin" in pub.group(0) and 'id="export-publish-section"' not in ep,
      pub and pub.group(0))
ap = C["ex_admin"].get("/").text
check("an admin's drawer has the live publish section", 'id="export-publish-section"' in ap)
check("a viewer's page doesn't carry the drawer (its tab is greyed)", 'id="export-builder"' not in C["ex_viewer"].get("/").text)
node = shutil.which("node")
if node:
    for who, html in (("editor", ep), ("admin", ap)):
        m = re.search(r"<script nonce=\"[^\"]+\">\s*// Export builder(.*?)</script>", html, re.S)
        f = Path(tempfile.mkdtemp(prefix="ex781-")) / "drawer.js"
        f.write_text("// Export builder" + (m.group(1) if m else ""), encoding="utf-8")
        res = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        check(f"the {who}'s drawer script parses (node --check)", m is not None and res.returncode == 0, res.stderr[:300])
        shutil.rmtree(f.parent, ignore_errors=True)
else:
    print("NOTE node not on PATH: the drawer script's syntax was not checked here")

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
