#!/usr/bin/env python3
"""Wallpaper to go (#780) and the editable word lists (#556: brand roles + wallpaper tags).

Throwaway DB (`_testenv.isolate`), no server: the app in-process through TestClient, as the admin
(the install token, `_testenv.client`) and as a real signed-in viewer (a session cookie). Covers:
  * the zip: its contents and names (de-duplicated, any case, README.txt reserved), the README;
  * what is left out and why: sensitive (flagged), a restricted type, a private card, a link, a file
    missing on disk; counted on the page's summary AND in the README, per viewer;
  * streaming: the generator yields before it has read every file; the response is chunked;
  * viewer vs admin (and anonymous), the page's button and the greyed admin controls;
  * vocab: the seeds, add / rename / remove / merge / move, every refusal code, dry runs, undo,
    the role labels (viewer reads, admin writes).

    python scripts/test_wallpaper_vocab_780.py      exits 1 if any check fails
"""

import io
import os
import re
import secrets
import sys
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("wallzip780-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, items, membership, paths, sharing, users, vocab, wallpapers  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
BASE = "http://testhost.local"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code(r):
    try:
        b = r.json()
    except ValueError:
        return None
    return (b.get("error") or {}).get("code") if isinstance(b, dict) else None


def raises(want, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except vocab.VocabError as e:
        return e.code == want
    except Exception as e:  # noqa: BLE001 - a NotFound etc. answers the code test too
        return getattr(e, "code", None) == want
    return False


def make(name, filename, body, tags=("wallpaper",), media_type="image", sensitive=False):
    slug = f"w-{name}-{secrets.token_hex(3)}"
    stored = f"{slug}{os.path.splitext(filename)[1] or '.bin'}"
    (paths.storage_dir() / stored).write_bytes(body)
    db.insert_upload(slug, filename, stored, "tester", media_type=media_type, tags=list(tags), sensitive=sensitive)
    return slug


def table():
    conn = db.get_conn()
    try:
        return [tuple(r) for r in conn.execute("SELECT id, vocab, term, sort_order FROM vocab_terms ORDER BY id")]
    finally:
        conn.close()


db.init_db()
users.limiter.reset()

# ---- 1. seeds ---------------------------------------------------------------------------------
print("--- 1. seeds ---")
check("brand_role seed = the old db.BRAND_ROLES, in order",
      vocab.terms("brand_role") == ["logo", "logotype", "icon", "color", "typography", "swag", "template-asset", "other"],
      vocab.terms("brand_role"))
check("wallpaper_tag seed = the old WALLPAPER_TAG_NAMES", vocab.terms("wallpaper_tag") == ["wallpaper", "desktop picture"])
check("the old constants are gone from core/db.py", not hasattr(db, "BRAND_ROLES") and not hasattr(db, "WALLPAPER_TAG_NAMES"))
before = table()
conn = db.get_conn()
vocab.ensure_seed(conn)
conn.commit()
conn.close()
check("re-seeding is a no-op (INSERT OR IGNORE)", table() == before)
check("vocab_terms is imaged and kept by delete-all",
      db.IMAGE_TABLE_KEYS.get("vocab_terms") == ("id",) and "vocab_terms" in __import__("core.reset", fromlist=["x"]).KEPT_TABLES)

# ---- 2. fixtures --------------------------------------------------------------------------------
BIG = os.urandom(300_000)
with actor.acting_as(actor.ACTOR_SCRIPT):
    A = make("a", "sunset.jpg", b"A" * 5000)
    B = make("b", "sunset.jpg", b"B" * 4000)                 # same name
    C = make("c", "SUNSET.JPG", b"C" * 3000)                 # same name, other case
    R = make("r", "readme.txt", b"not the readme", media_type="document")   # clashes with README.txt
    D = make("d", "desk.png", BIG, tags=("Desktop Picture",))   # the other synonym, any case
    SENS = make("s", "secret.jpg", b"S" * 100, sensitive=True)
    CERT = make("k", "key.pem", b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", media_type="certkey")
    PRIV = make("p", "private.jpg", b"P" * 200)
    GONE = make("g", "gone.jpg", b"G" * 10)
    (paths.storage_dir() / db.get_by_slug(GONE)["stored_filename"]).unlink()
    LINK = "w-link-" + secrets.token_hex(3)
    db.insert_content(LINK, "tester", "url", external_url="https://example.com/wall", tags=["wallpaper"])
    OTHER = make("o", "not-a-wallpaper.jpg", b"O", tags=("holiday",))
    card = cards.create("Private Wallpapers").data["card"]
    membership.add_files(card["id"], [PRIV], **membership.UI_EFFECTS)
    sharing.set_private("card", card["id"], True)
    LOGO1 = make("l1", "logo1.png", b"L1", tags=())
    LOGO2 = make("l2", "logo2.png", b"L2", tags=())
    items.update(LOGO1, is_brand_asset=True, brand_role="logo")
    items.update(LOGO2, is_brand_asset=True, brand_role="logo")
    vname, vpw = "viewer_" + secrets.token_hex(2), "vw-" + secrets.token_urlsafe(14)
    users.create_user(vname, vpw, "viewer")

admin = _testenv.client(webapp.app, base_url=BASE)
viewer = TestClient(webapp.app, base_url=BASE)
r = viewer.post("/api/auth/login", json={"username": vname, "password": vpw}, headers=SAME)
check("viewer signs in", r.status_code == 200, r.text[:200])
VCSRF = re.search(r'<meta name="csrf-token" content="([^"]+)"', viewer.get("/").text).group(1)
VH = {**SAME, "X-CSRF-Token": VCSRF}
anon = TestClient(webapp.app, base_url=BASE)

# ---- 3. the summary (what the page shows beside the button) ---------------------------------------
print("--- 3. summary ---")
s = admin.get("/api/wallpapers/download-summary").json()
check("admin: 6 files go in (A B C R D + the private card's)", s["included"] == 6, s)
check("admin: 2 sensitive left out (flagged + restricted type), 0 no access",
      s["sensitive"] == 2 and s["no_access"] == 0, s)
check("admin: the link and the missing file are counted", s["links"] == 1 and s["missing"] == 1, s)
check("admin: left_out adds up", s["left_out"] == 4, s)
check("admin: the notes say each reason", any("2 not included (sensitive)" in n for n in s["notes"])
      and any("link" in n for n in s["notes"]) and any("missing on disk" in n for n in s["notes"]), s["notes"])
check("admin: bytes = the files' sizes", s["bytes"] == 5000 + 4000 + 3000 + len(b"not the readme") + len(BIG) + 200, s)

sv = viewer.get("/api/wallpapers/download-summary").json()
check("viewer: 5 files go in (not the private card's)", sv["included"] == 5, sv)
check("viewer: 3 not included (no access): flagged, restricted type, private card", sv["no_access"] == 3, sv)
check("viewer: the note says 'N not included (no access)'", any(n.startswith("3 not included (no access)") for n in sv["notes"]),
      sv["notes"])
check("viewer: the page's own list agrees (5 files + the link + the missing file)",
      len(viewer.get("/api/wallpapers").json()) == 7)
check("anonymous: summary 401", anon.get("/api/wallpapers/download-summary").status_code == 401)
check("anonymous: zip 401", anon.get("/api/wallpapers/download.zip").status_code == 401)

# ---- 4. the zip ---------------------------------------------------------------------------------
print("--- 4. zip ---")
r = admin.get("/api/wallpapers/download.zip")
check("admin: zip 200 application/zip", r.status_code == 200 and r.headers["content-type"] == "application/zip",
      f"{r.status_code} {r.headers.get('content-type')}")
check("zip: attachment, nosniff, sandbox CSP", r.headers.get("content-disposition", "").startswith("attachment")
      and "wallpapers.zip" in r.headers.get("content-disposition", "")
      and r.headers.get("x-content-type-options") == "nosniff" and "sandbox" in r.headers.get("content-security-policy", ""),
      dict(r.headers))
check("zip: streamed (no Content-Length, chunked)", "content-length" not in r.headers, dict(r.headers))
check("zip: count headers", r.headers.get("x-wallpapers-included") == "6" and r.headers.get("x-wallpapers-left-out") == "4")
zf = zipfile.ZipFile(io.BytesIO(r.content))
names = zf.namelist()
check("zip: valid (testzip clean)", zf.testzip() is None)
check("zip: names de-duplicated in any case (the first keeps its own case), README.txt reserved",
      sorted(n.lower() for n in names) == sorted(["sunset.jpg", "sunset (2).jpg", "sunset (3).jpg", "readme (2).txt",
                                                  "desk.png", "private.jpg", "readme.txt"])
      and "README.txt" in names and len(names) == 7, names)
by_name = {n: zf.read(n) for n in names}
check("zip: full-size bytes, untouched", by_name["desk.png"] == BIG)
check("zip: the three sunsets are the three files",
      sorted(len(v) for k, v in by_name.items() if k.lower().startswith("sunset")) == [3000, 4000, 5000])
check("zip: entries are stored, not deflated", all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist()))
readme = by_name["README.txt"].decode()
check("README: how many are in", "6 wallpapers in this zip" in readme, readme)
check("README: the sensitive count", "2 not included (sensitive)" in readme, readme)
check("README: the link and the missing file, by name",
      "A link, not a file:" in readme and "File missing on disk: gone.jpg" in readme, readme)
check("no sensitive bytes in the admin's zip", all(b"BEGIN CERTIFICATE" not in v and v != b"S" * 100 for v in by_name.values()))

rv = viewer.get("/api/wallpapers/download.zip")
zv = zipfile.ZipFile(io.BytesIO(rv.content))
check("viewer: zip 200, 5 files + README", rv.status_code == 200 and len(zv.namelist()) == 6, zv.namelist())
check("viewer: the private card's wallpaper is not in it", "private.jpg" not in zv.namelist())
rmv = zv.read("README.txt").decode()
check("viewer README: '3 not included (no access)'", "3 not included (no access)" in rmv, rmv)

# streaming: the generator hands over bytes before it has read the later files
wallpapers.CHUNK = 64 * 1024
with actor.acting_as(actor.ACTOR_SCRIPT):
    plan = wallpapers.plan()
gen = wallpapers.stream(plan)
first = next(gen)
check("stream: the first chunk is a zip local header, long before the end",
      first[:4] == b"PK\x03\x04" and len(first) < sum(e.size for e in plan.entries), len(first))
rest = list(gen)
check("stream: the 300 KB file arrives in several chunks (64 KB reads)", len(rest) >= 5, len(rest))
check("stream: the whole stream is the same valid zip", zipfile.ZipFile(io.BytesIO(first + b"".join(rest))).testzip() is None)
wallpapers.CHUNK = 1024 * 1024

# a file deleted between the plan and the read: skipped, named in the README, never silent
with actor.acting_as(actor.ACTOR_SCRIPT):
    plan = wallpapers.plan()
victim = next(e for e in plan.entries if e.arcname == "desk.png")
saved = victim.path.read_bytes()
victim.path.unlink()
z = zipfile.ZipFile(io.BytesIO(b"".join(wallpapers.stream(plan))))
txt = z.read("README.txt").decode()
check("vanished mid-stream: left out and named", "desk.png" not in z.namelist()
      and "couldn't be read while the zip was made" in txt, txt)
victim.path.write_bytes(saved)

# ---- 5. the page ------------------------------------------------------------------------------------
print("--- 5. page ---")
pv = viewer.get("/wallpaper").text
pa = admin.get("/wallpaper").text
check("page: the Download all (.zip) button", 'id="wallpaper-zip-btn"' in pv and "Download all (.zip)" in pv)
check("page: the note beside it (role=status)", 'id="wallpaper-zip-note"' in pv)
check("viewer page: the tag control greyed, labelled with its role, with the why",
      'id="wallpaper-tags-locked"' in pv and "(admins only)" in pv and 'id="wallpaper-tags-why"' in pv)
check("admin page: the real link to Admin", 'href="/admin#admin-vocab-section"' in pa and 'id="wallpaper-tags-locked"' not in pa)
check("page: no inline handlers", not re.search(r"\son[a-z]+=", pv))
bv = viewer.get("/brand").text
check("brand page: viewer sees the brand-roles control greyed", 'id="brand-roles-locked"' in bv and "(admins only)" in bv)
ad = admin.get("/admin").text
check("admin page: the word lists panel + its script", 'id="admin-vocab-section"' in ad and "/static/js/admin-vocab.js" in ad)
check("item page: the brand-role picker reads the list", "template-asset" in admin.get(f"/object/{LOGO1}").text)

# ---- 6. vocab over HTTP: roles ---------------------------------------------------------------------
print("--- 6. vocab roles ---")
r = viewer.get("/api/vocab/brand_role")
check("viewer: GET the list 200, no item counts", r.status_code == 200 and all("items" not in t for t in r.json()["terms"]))
r = admin.get("/api/vocab/brand_role")
check("admin: GET has item counts (logo = 2)", next(t for t in r.json()["terms"] if t["term"] == "logo")["items"] == 2, r.text)
for path, form in (("", {"term": "mascot"}), ("/rename", {"term": "logo", "new_term": "x"}),
                   ("/remove", {"term": "other"}), ("/merge", {"term": "logo", "into": "icon"}),
                   ("/move", {"term": "logo", "direction": "down"})):
    r = viewer.post(f"/api/vocab/brand_role{path}", data=form, headers=VH)
    check(f"viewer: POST /api/vocab/brand_role{path} -> 403 forbidden", r.status_code == 403 and code(r) == "forbidden",
          f"{r.status_code} {r.text[:120]}")
check("viewer writes changed nothing", vocab.terms("brand_role")[0] == "logo" and "mascot" not in vocab.terms("brand_role"))
r = admin.get("/api/vocab/nope")
check("unknown list -> 400 bad_vocab", r.status_code == 400 and code(r) == "bad_vocab", r.text[:120])

# ---- 7. vocab CRUD, refusals, undo ------------------------------------------------------------------
print("--- 7. vocab CRUD ---")
snap = table()
r = admin.post("/api/vocab/brand_role", data={"term": "Mascot", "dry_run": "true"})
check("dry-run add answers as the write, writes nothing", r.status_code == 200 and r.json()["dry_run"]
      and "mascot" in [t["term"] for t in r.json()["terms"]] and table() == snap, r.text[:200])
r = admin.post("/api/vocab/brand_role", data={"term": "Mascot"})
body = r.json()
check("add: lowercased, at the end, batch_id", r.status_code == 200 and vocab.terms("brand_role")[-1] == "mascot"
      and body["batch_id"], r.text[:200])
cards.undo(body["batch_id"])
check("add: undo removes it", table() == snap)
r = admin.post("/api/vocab/brand_role", data={"term": "Bad Role!"})
check("add: bad term -> 400 bad_vocab_term", r.status_code == 400 and code(r) == "bad_vocab_term", r.text[:150])
r = admin.post("/api/vocab/brand_role", data={"term": "LOGO"})
check("add: existing (any case) -> 409 vocab_conflict", r.status_code == 409 and code(r) == "vocab_conflict", r.text[:150])
r = admin.post("/api/vocab/wallpaper_tag", data={"term": "   "})
check("add: empty wallpaper tag -> 400 bad_vocab_term", r.status_code == 400 and code(r) == "bad_vocab_term")

# rename a brand role: its items move with it, one undo restores both
r = admin.post("/api/vocab/brand_role/rename", data={"term": "logo", "new_term": "logomark"})
body = r.json()
check("rename brand role: items moved", r.status_code == 200 and body["items_moved"] == 2
      and db.get_by_slug(LOGO1)["brand_role"] == "logomark" and db.get_by_slug(LOGO2)["brand_role"] == "logomark", r.text[:200])
check("rename: the list says logomark first", vocab.terms("brand_role")[0] == "logomark")
cards.undo(body["batch_id"])
check("rename: undo restores the term AND the items",
      vocab.terms("brand_role")[0] == "logo" and db.get_by_slug(LOGO1)["brand_role"] == "logo" and table() == snap)
r = admin.post("/api/vocab/brand_role/rename", data={"term": "logo", "new_term": "icon"})
check("rename onto an existing term -> 409 vocab_conflict, says merge", r.status_code == 409
      and code(r) == "vocab_conflict" and "merge" in r.json()["error"]["message"].lower(), r.text[:200])
r = admin.post("/api/vocab/brand_role/rename", data={"term": "nope", "new_term": "x"})
check("rename unknown -> 404 not_found", r.status_code == 404 and code(r) == "not_found")

# remove: in use is refused and names the items; merge instead
r = admin.post("/api/vocab/brand_role/remove", data={"term": "logo"})
err = r.json().get("error", {})
check("remove a brand role in use -> 409 vocab_in_use", r.status_code == 409 and err.get("code") == "vocab_in_use", r.text[:200])
check("vocab_in_use names the items and the count",
      err.get("details", {}).get("count") == 2 and {i["slug"] for i in err["details"]["items"]} == {LOGO1, LOGO2}
      and "logo1.png" in err.get("message", ""), err)
check("the refused remove wrote nothing", table() == snap)
r = admin.post("/api/vocab/brand_role/merge", data={"term": "logo", "into": "icon", "dry_run": "true"})
check("merge dry run: says 2 move, writes nothing", r.json().get("items_moved") == 2 and table() == snap
      and db.get_by_slug(LOGO1)["brand_role"] == "logo", r.text[:200])
r = admin.post("/api/vocab/brand_role/merge", data={"term": "logo", "into": "icon"})
body = r.json()
check("merge: items take the role, the term leaves", r.status_code == 200 and body["items_moved"] == 2
      and db.get_by_slug(LOGO2)["brand_role"] == "icon" and "logo" not in vocab.terms("brand_role"), r.text[:200])
cards.undo(body["batch_id"])
check("merge: one undo restores both", table() == snap and db.get_by_slug(LOGO2)["brand_role"] == "logo")
r = admin.post("/api/vocab/brand_role/merge", data={"term": "logo", "into": "logo"})
check("merge into itself -> 400 bad_vocab_merge", r.status_code == 400 and code(r) == "bad_vocab_merge")
r = admin.post("/api/vocab/wallpaper_tag/merge", data={"term": "wallpaper", "into": "desktop picture"})
check("merge a wallpaper tag -> 400 bad_vocab_merge", r.status_code == 400 and code(r) == "bad_vocab_merge")
r = admin.post("/api/vocab/brand_role/remove", data={"term": "swag"})
body = r.json()
check("remove an unused brand role: ok + undo", r.status_code == 200 and "swag" not in vocab.terms("brand_role"), r.text[:150])
cards.undo(body["batch_id"])
check("remove: undo brings it back in place", table() == snap)

# move
r = admin.post("/api/vocab/brand_role/move", data={"term": "logo", "direction": "up"})
check("move up at the top: no-op, no batch", r.status_code == 200 and r.json()["batch_id"] is None and table() == snap)
r = admin.post("/api/vocab/brand_role/move", data={"term": "logo", "direction": "down"})
check("move down", vocab.terms("brand_role")[:2] == ["logotype", "logo"])
cards.undo(r.json()["batch_id"])
check("move: undo", table() == snap)
r = admin.post("/api/vocab/brand_role/move", data={"term": "logo", "direction": "sideways"})
check("move bad direction -> 400 bad_vocab_move", r.status_code == 400 and code(r) == "bad_vocab_move")

# wallpaper tags: rename / remove say what the page gains or loses; the last can't go
r = admin.post("/api/vocab/wallpaper_tag/remove", data={"term": "desktop picture", "dry_run": "true"})
check("remove wallpaper tag dry run: 1 leaves the page, nothing written",
      r.json().get("wallpapers_leaving") == 1 and table() == snap, r.text[:200])
r = admin.post("/api/vocab/wallpaper_tag/rename", data={"term": "wallpaper", "new_term": "Holiday"})
body = r.json()
check("rename wallpaper tag: before/after counts", r.status_code == 200 and body["wallpapers_after"] == 2,
      r.text[:200])  # desk.png (desktop picture) + not-a-wallpaper.jpg (holiday)
check("the page follows the rename", {w["slug"] for w in admin.get("/api/wallpapers").json()} == {D, OTHER})
cards.undo(body["batch_id"])
r1 = admin.post("/api/vocab/wallpaper_tag/remove", data={"term": "desktop picture"}).json()
r = admin.post("/api/vocab/wallpaper_tag/remove", data={"term": "wallpaper"})
check("remove the last wallpaper tag -> 409 vocab_last_term", r.status_code == 409 and code(r) == "vocab_last_term", r.text[:150])
r2 = admin.post("/api/vocab/wallpaper_tag", data={"term": "nothing here"}).json()
r3 = admin.post("/api/vocab/wallpaper_tag/remove", data={"term": "wallpaper"}).json()
r = admin.get("/api/wallpapers/download.zip")
check("no wallpaper files -> 404 no_wallpapers (never an empty zip)", r.status_code == 404 and code(r) == "no_wallpapers",
      r.text[:150])
check("the summary agrees: 0 included", admin.get("/api/wallpapers/download-summary").json()["included"] == 0)
for b in (r3, r2, r1):
    cards.undo(b["batch_id"])
check("undo restores the wallpaper list exactly", table() == snap, table())
check("and the zip again holds 6", admin.get("/api/wallpapers/download-summary").json()["included"] == 6)

# core-level refusals the routes share
check("core: unknown term remove -> not_found", raises("not_found", vocab.remove, "brand_role", "nope"))
check("core: bad vocab -> bad_vocab", raises("bad_vocab", vocab.add, "colours", "red"))

# ---- 8. MCP read ------------------------------------------------------------------------------------
print("--- 8. MCP ---")
from mcp_server import server  # noqa: E402
out = server.constructicon_list_vocab("wallpaper_tag")
check("MCP constructicon_list_vocab", out.get("ok") and [t["term"] for t in out["terms"]] == ["wallpaper", "desktop picture"], out)
check("MCP: viewer role row", server.role_for("constructicon_list_vocab") == "viewer")

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
