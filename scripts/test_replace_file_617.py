#!/usr/bin/env python3
"""Replace an item's file, keep the item (#617). Throwaway DB + storage, no server.

    python scripts/test_replace_file_617.py

In-process (scripts/_testenv.py), the real FastAPI app through TestClient (real sessions for an admin,
an editor and a viewer; the install token for plain admin calls) and the MCP tool function called
directly. Covers, through BOTH the MCP tool and the HTTP route:
  1. what stays: slug, cards, tags, title, caption, relations; what changes: the bytes served at
     /f/<slug>, size, extracted text (old-only words stop matching, new words match), the search index
     (no drift), the thumbnail (an image item);
  2. undo: the previous bytes are in the trash in the same batch, constructicon_undo brings the old file,
     size and text back; after the replace the displaced file is recoverable from the trash dir;
  3. who: the change-log actor is `mcp` for the tool and `user:<name>` for the web action;
  4. roles and policy: viewer refused (403 forbidden, and at the service), editor and admin succeed, a
     flagged-sensitive item is invisible (404) to an editor who is not its uploader, a redacted item is
     refused with replace_redacted, a content-only item with no_file;
  5. limits: oversize -> file_too_large (413) before anything is written, empty -> empty_file, bad base64
     -> bad_base64, identical bytes -> file_unchanged, a different TYPE -> replace_type_mismatch (and
     nothing is changed, no stray file is left in storage), an unsupported extension, dry_run;
  6. the object page: "Replace file..." is offered to an editor/admin on a file item, not to a viewer,
     not on a redacted item.
Exits 1 if any check fails.
"""

import base64
import io
import json
import os
import re
import secrets
import sys
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("replace617-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, items, membership, paths, storage, users  # noqa: E402
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


def new_client(**headers):
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False, headers=headers or None)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def png_bytes(seed, size=(64, 48)):
    """A noisy picture (a flat one is "too uniform" for a perceptual hash)."""
    import random
    from PIL import Image
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def err_code(r):
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("error", {}).get("code") if isinstance(body, dict) else None


def b64(data):
    return base64.b64encode(data).decode()


def wait_for(fn, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return fn()


def settled(slug):
    return wait_for(lambda: (db.get_by_slug(slug) or {}).get("ocr_status") == "done")


def storage_files():
    return sorted(p.name for p in paths.storage_dir().iterdir() if p.is_file())


def file_bytes(slug):
    return db.get_by_slug(slug) and storage.path_for(db.get_by_slug(slug)["stored_filename"]).read_bytes()


db.init_db()
users.limiter.reset()
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)

# ---- users and clients ---------------------------------------------------------------------------
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("rf_admin", "admin"), ("rf_ed", "editor"), ("rf_ed2", "editor"), ("rf_view", "viewer")):
        PW[name] = "rf-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
C, H = {}, {}
for key, name in (("A", "rf_admin"), ("E", "rf_ed"), ("E2", "rf_ed2"), ("V", "rf_view")):
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    check(f"{key} signs in", r.status_code == 200, r.text[:200])
    C[key] = c
    H[key] = {**SAME, "X-CSRF-Token": csrf_of(c)}
TOKEN = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)


def upload(who, filename, data, mime="text/markdown", sensitive=False):
    fields = {"description": "replace617 fixture"}
    if sensitive:
        fields["sensitive"] = "true"
    r = C[who].post("/api/upload", files={"file": (filename, data, mime)}, data=fields, headers=H[who])
    assert r.status_code == 200, r.text[:300]
    return r.json()["slug"]


def replace_http(who, slug, filename, data, mime="text/markdown"):
    return C[who].post(f"/api/image/{slug}/replace-file", files={"file": (filename, data, mime)}, headers=H[who])


def mcp_replace(slug, data, filename=None, raw_b64=None):
    return mcp.constructicon_replace_file(slug, raw_b64 if raw_b64 is not None else b64(data), filename)


def change_actor(batch_id):
    rows = db.list_change_log(batch_id=batch_id)
    return [(r["op"], r["actor"]) for r in rows]


def search_slugs(q):
    with actor.acting_as(actor.ACTOR_MCP):
        return [r["slug"] for r in mcp.constructicon_search(query=q)]


def make_card_item(who, filename, data):
    """An uploaded item with a title, caption, tag, card and relation, to prove they survive."""
    slug = upload(who, filename, data)
    with actor.acting_as(actor.ACTOR_SCRIPT):
        items.update(slug, display_name="1104 car reference", content_description="Caption written by hand",
                     tags=["cars617"])
        cards.create(f"Car card {slug}")
        card = next(p for p in db.list_projects() if p["title"] == f"Car card {slug}")
        membership.add_files(card["id"], [slug], **membership.UI_EFFECTS)
        other = upload(who, f"related-{slug}.md", b"# a related note\n")
        items.relate(slug, other)
    settled(slug)
    return slug, card, other


OLD = b"# Car notes\nzanzibarquartz is the old only word\nshared marker\n"
NEW = b"# Car notes v2\nplatypusnebula is the new only word\nshared marker\n"
NEWER = b"# Car notes v3\nmarmotcobalt arrived last\nshared marker\n"

# ===== 1 + 2 + 3a. MCP replace: what stays, what changes, undo, actor =================================
print("--- 1. replace through the MCP tool ---")
M, CARD_M, REL_M = make_card_item("A", "car-notes.md", OLD)
before = db.get_by_slug(M)
check("fixture: the old-only word is searchable", M in search_slugs("zanzibarquartz"), search_slugs("zanzibarquartz"))
check("fixture: extracted text has the old words", "zanzibarquartz" in before["extracted_text"], before["extracted_text"])
old_stored = before["stored_filename"]

out = mcp_replace(M, NEW)
check("MCP: ok, same slug", out.get("slug") == M and out.get("replaced") is True, out)
check("MCP: batch_id returned", bool(out.get("batch_id")), out)
after = db.get_by_slug(M)
check("kept: slug, title, caption, filename", after["slug"] == M and after["display_name"] == "1104 car reference"
      and after["content_description"] == "Caption written by hand" and after["filename"] == "car-notes.md", dict(after))
check("kept: free-text tag", "cars617" in (after["tags"] or []), after["tags"])
check("kept: card membership", any(p["id"] == CARD_M["id"] for p in db.list_projects_for_post(M)))
_conn = db.get_conn()
check("kept: related link", _conn.execute("SELECT COUNT(*) FROM capture_event_relations WHERE slug_a IN (?, ?) AND slug_b IN (?, ?)",
                                          (M, REL_M, M, REL_M)).fetchone()[0] >= 1)
_conn.close()
check("changed: stored file and size", after["stored_filename"] != old_stored and after["file_size"] == len(NEW),
      (after["stored_filename"], after["file_size"]))
check("/f/<slug> serves the NEW bytes", TOKEN.get(f"/f/{M}").content == NEW)
check("MCP download serves the new bytes", base64.b64decode(mcp.constructicon_download(M)["content_base64"]) == NEW)
check("text extraction re-ran on the new bytes", settled(M) and "platypusnebula" in db.get_by_slug(M)["extracted_text"],
      db.get_by_slug(M)["extracted_text"])
check("old-only text is gone from the row", "zanzibarquartz" not in db.get_by_slug(M)["extracted_text"])
check("search: the new-only word finds it", M in search_slugs("platypusnebula"), search_slugs("platypusnebula"))
check("search: the old-only word no longer finds it", M not in search_slugs("zanzibarquartz"), search_slugs("zanzibarquartz"))
check("search: a word in both still finds it", M in search_slugs("shared marker"))
d = db.check_search_index()
check("search index has no drift", not d["missing"] and not d["stale"], d)
check("title and card still findable", M in search_slugs("1104 car reference") and M in search_slugs(f"Car card {M}"))

print("--- 2. the previous bytes are recoverable; undo restores them ---")
trash = db.list_trash()
mine = [t for t in trash if t["slug"] == M and t["reason"] == "replace"]
check("a trash entry exists for the displaced file (reason replace, expires)", len(mine) == 1 and mine[0]["expires_at"], trash)
held = paths.trash_dir(out["batch_id"]) / old_stored
check("the OLD bytes sit in the trash dir", held.is_file() and held.read_bytes() == OLD, str(held))
check("it is not mistaken for a redact hold", db.get_redact_hold(M) is None)
check("the change is ONE batch: row update + trash row", [o for o, _a in change_actor(out["batch_id"])] == ["item_replace_file"],
      change_actor(out["batch_id"]))
u = mcp.constructicon_undo(out["batch_id"])
check("undo ok", u.get("ok") is True, u)
check("undo: the OLD bytes are served again", TOKEN.get(f"/f/{M}").content == OLD)
check("undo: stored name and size are back", db.get_by_slug(M)["stored_filename"] == old_stored
      and db.get_by_slug(M)["file_size"] == len(OLD), dict(db.get_by_slug(M)))
check("undo: the trash entry is gone", not [t for t in db.list_trash() if t["slug"] == M])
check("undo: text re-extracted from the old file", settled(M) and "zanzibarquartz" in db.get_by_slug(M)["extracted_text"]
      and "platypusnebula" not in db.get_by_slug(M)["extracted_text"], db.get_by_slug(M)["extracted_text"])
check("undo: search finds the old word, not the new one", M in search_slugs("zanzibarquartz") and M not in search_slugs("platypusnebula"))
check("undo: title, tags and card untouched", db.get_by_slug(M)["display_name"] == "1104 car reference"
      and any(p["id"] == CARD_M["id"] for p in db.list_projects_for_post(M)))
d = db.check_search_index()
check("undo: search index has no drift", not d["missing"] and not d["stale"], d)

print("--- 3a. actor for the MCP tool ---")
out2 = mcp_replace(M, NEWER)
check("replace again after an undo works", out2.get("replaced") is True and TOKEN.get(f"/f/{M}").content == NEWER, out2)
check("change-log actor is mcp", change_actor(out2["batch_id"]) == [("item_replace_file", "mcp")], change_actor(out2["batch_id"]))
check("a second replace keeps the first replace's batch undoable independently",
      mcp.constructicon_undo(out2["batch_id"]).get("ok") is True and TOKEN.get(f"/f/{M}").content == OLD)

# ===== HTTP route ====================================================================================
print("--- 1b. replace through the HTTP route (editor) ---")
H1, CARD_H, REL_H = make_card_item("E", "http-notes.md", OLD)
r = replace_http("E", H1, "http-notes.md", NEW)
check("HTTP: editor replaces (200)", r.status_code == 200, r.text[:300])
body = r.json()
check("HTTP: same slug, replaced, batch_id", body.get("slug") == H1 and body.get("replaced") is True and body.get("batch_id"), body)
check("HTTP: new bytes at /f/<slug>", TOKEN.get(f"/f/{H1}").content == NEW)
check("HTTP: card, title, tag kept", any(p["id"] == CARD_H["id"] for p in db.list_projects_for_post(H1))
      and db.get_by_slug(H1)["display_name"] == "1104 car reference" and "cars617" in db.get_by_slug(H1)["tags"])
check("HTTP: text re-extracted, old word unsearchable, new word searchable",
      settled(H1) and H1 in search_slugs("platypusnebula") and H1 not in search_slugs("zanzibarquartz"))
check("3b. change-log actor is user:rf_ed", change_actor(body["batch_id"]) == [("item_replace_file", "user:rf_ed")],
      change_actor(body["batch_id"]))
r = C["E"].post(f"/api/changes/{body['batch_id']}/undo", headers=H["E"])
check("HTTP undo (the page's Undo bar) restores the old bytes", r.status_code == 200 and TOKEN.get(f"/f/{H1}").content == OLD,
      (r.status_code, r.text[:200]))
check("change-log actor of the undo is the editor", ("undo", "user:rf_ed") in [(o, a) for o, a in
      [(x["op"], x["actor"]) for x in db.list_change_log(limit=5)]], [(x["op"], x["actor"]) for x in db.list_change_log(limit=5)])

# an image: the thumbnail is regenerated
print("--- 1c. an image: the thumbnail is regenerated ---")
IMG = upload("A", "pic.png", png_bytes(1), mime="image/png")
t_before = TOKEN.get(f"/f/{IMG}/thumb").content
r = replace_http("A", IMG, "pic.png", png_bytes(7, (96, 64)), mime="image/png")
check("image replace (200)", r.status_code == 200, r.text[:300])
settled(IMG)
t_after = TOKEN.get(f"/f/{IMG}/thumb")
check("thumbnail exists and is different from before", t_after.status_code == 200 and t_after.content != t_before,
      (t_after.status_code, len(t_before), len(t_after.content)))
check("thumbnail is a JPEG of the NEW picture's shape (96x64 -> 96x64 thumb)", t_after.content[:3] == b"\xff\xd8\xff")
check("the original is the new picture", TOKEN.get(f"/f/{IMG}").content == png_bytes(7, (96, 64)))
check("perceptual hash recomputed by the pipeline", db.get_by_slug(IMG)["perceptual_hash"] is not None, db.get_by_slug(IMG)["perceptual_hash"])

# ===== 4. roles and policy ===========================================================================
print("--- 4. roles and policy ---")
R = upload("A", "roles-notes.md", OLD)
before_files = storage_files()
r = replace_http("V", R, "roles-notes.md", NEW)
check("viewer refused: 403 forbidden", r.status_code == 403 and err_code(r) == "forbidden", (r.status_code, r.text[:200]))
check("viewer refusal changed nothing", TOKEN.get(f"/f/{R}").content == OLD and storage_files() == before_files)
with actor.acting_as("user:rf_view"):
    try:
        items.replace_file(R, NEW)
        got = None
    except AppError as e:
        got = (e.code, e.status)
check("viewer refused at the service too (forbidden, 403)", got == ("forbidden", 403), got)
r = replace_http("E", R, "roles-notes.md", NEW)
check("editor succeeds", r.status_code == 200 and TOKEN.get(f"/f/{R}").content == NEW, (r.status_code, r.text[:200]))
r = replace_http("A", R, "roles-notes.md", NEWER)
check("admin succeeds", r.status_code == 200 and TOKEN.get(f"/f/{R}").content == NEWER, (r.status_code, r.text[:200]))
rt = TOKEN.post(f"/api/image/{R}/replace-file", files={"file": ("roles-notes.md", NEW, "text/markdown")})
check("install token (admin) succeeds", rt.status_code == 200, (rt.status_code, rt.text[:200]))
check("anonymous refused", new_client().post(f"/api/image/{R}/replace-file",
                                             files={"file": ("x.md", NEW, "text/markdown")}).status_code in (401, 403))

S = upload("A", "flagged-notes.md", OLD, sensitive=True)
r = replace_http("E2", S, "flagged-notes.md", NEW)
check("sensitive item: another editor sees 404 not_found (it is not theirs to see)",
      r.status_code == 404 and err_code(r) == "not_found", (r.status_code, r.text[:200]))
r = replace_http("A", S, "flagged-notes.md", NEW)
check("sensitive item: an admin may replace it, and it stays sensitive",
      r.status_code == 200 and db.get_by_slug(S)["sensitive"], (r.status_code, r.text[:200]))

X = upload("A", "redact-me.md", OLD)
with actor.acting_as(actor.ACTOR_SCRIPT):
    items.redact(X)
r = replace_http("A", X, "redact-me.md", NEW)
check("redacted item refused: 409 replace_redacted", r.status_code == 409 and err_code(r) == "replace_redacted",
      (r.status_code, r.text[:300]))
out = mcp_replace(X, NEW)
check("redacted item refused over MCP too", out.get("ok") is False and out["error"]["code"] == "replace_redacted", out)
check("redacted item: still no stored file, nothing written", db.get_by_slug(X)["stored_filename"] is None)

# a content-only item (no file)
with actor.acting_as(actor.ACTOR_SCRIPT):
    db.insert_content("rf-yt", "tester", "youtube", external_url="https://youtu.be/abc", content_description="a video")
out = mcp_replace("rf-yt", NEW, "x.md")
check("content-only item: no_file", out.get("ok") is False and out["error"]["code"] == "no_file", out)
out = mcp_replace("no-such-slug", NEW)
check("unknown slug: not_found", out.get("ok") is False and out["error"]["code"] == "not_found", out)

# ===== 5. limits and odd input ======================================================================
print("--- 5. limits, odd input, type change ---")
L = upload("A", "limits-notes.md", OLD)
files0 = storage_files()
saved = (storage.MAX_BYTES, storage.MAX_MB)
storage.MAX_BYTES, storage.MAX_MB = 1024, 0
try:
    big = b"# big\n" + b"x" * 4096
    r = replace_http("A", L, "limits-notes.md", big)
    check("oversize over HTTP: 413 file_too_large", r.status_code == 413 and err_code(r) == "file_too_large", (r.status_code, r.text[:300]))
    out = mcp_replace(L, big)
    check("oversize over MCP: file_too_large (with the cap in the message)", out.get("ok") is False
          and out["error"]["code"] == "file_too_large" and "limit" in out["error"]["message"], out)
    out = mcp_replace(L, b"", raw_b64=b64(b"y" * 200_000))
    check("oversize is refused from the base64 length, before decoding", out.get("ok") is False and out["error"]["code"] == "file_too_large", out)
finally:
    storage.MAX_BYTES, storage.MAX_MB = saved
check("oversize left the item and storage untouched", TOKEN.get(f"/f/{L}").content == OLD and storage_files() == files0)

out = mcp_replace(L, b"", raw_b64="!!!not base64!!!")
check("bad base64: bad_base64 with detail", out.get("ok") is False and out["error"]["code"] == "bad_base64"
      and "base64" in out["error"]["message"], out)
out = mcp_replace(L, b"", raw_b64="QUJD=ZZ")
check("malformed padding: bad_base64", out.get("ok") is False and out["error"]["code"] == "bad_base64", out)
out = mcp_replace(L, b"")
check("empty file over MCP: empty_file", out.get("ok") is False and out["error"]["code"] == "empty_file", out)
r = replace_http("A", L, "limits-notes.md", b"")
check("empty file over HTTP: 400 empty_file", r.status_code == 400 and err_code(r) == "empty_file", (r.status_code, r.text[:200]))
out = mcp_replace(L, OLD)
check("identical bytes: file_unchanged", out.get("ok") is False and out["error"]["code"] == "file_unchanged", out)
out = mcp_replace(L, b"", raw_b64=b64(NEW)[:20] + "\n" + b64(NEW)[20:])
check("base64 with a newline in it is accepted (whitespace ignored)", out.get("replaced") is True, out)

T = upload("A", "type-notes.md", OLD)
files1 = storage_files()
r = replace_http("A", T, "type-notes.txt", b"plain text now\n", mime="text/plain")
check("type change .md -> .txt: 422 replace_type_mismatch naming both types",
      r.status_code == 422 and err_code(r) == "replace_type_mismatch"
      and r.json()["error"]["details"].get("current_type") == "markdown" and r.json()["error"]["details"].get("new_type") == "text", r.text[:400])
out = mcp_replace(T, png_bytes(3), "picture.png")
check("type change .md -> .png over MCP: replace_type_mismatch", out.get("ok") is False
      and out["error"]["code"] == "replace_type_mismatch", out)
check("a refused type change changed nothing and left no stray file", TOKEN.get(f"/f/{T}").content == OLD and storage_files() == files1,
      set(storage_files()) ^ set(files1))
check("... and logged no change", not [x for x in db.list_change_log(limit=3) if x["op"] == "item_replace_file"
                                       and T in (x["affected_slugs"] or [])])
out = mcp_replace(T, b"whatever", "weird.zzzunknown")
check("unsupported extension: unsupported_type", out.get("ok") is False and out["error"]["code"] == "unsupported_type", out)
out = mcp_replace(T, NEW, "type-notes.markdown")
check("same type, other extension (.md -> .markdown): allowed, stem kept, extension swapped",
      out.get("replaced") is True and db.get_by_slug(T)["filename"] == "type-notes.markdown", out)
check("... a title that comes from the filename follows only its extension; a set title never moves",
      out["display_name"] == "type-notes.markdown" and db.get_by_slug(T)["display_name"] is None, out["display_name"])

D = upload("A", "dry-notes.md", OLD)
files2 = storage_files()
with actor.acting_as(actor.ACTOR_SCRIPT):
    res = items.replace_file(D, NEW, dry_run=True)
check("dry_run: reports ok but changes nothing (bytes, stored name, trash, storage files, log)",
      res.ok and TOKEN.get(f"/f/{D}").content == OLD and storage_files() == files2
      and not [t for t in db.list_trash() if t["slug"] == D]
      and not [x for x in db.list_change_log(limit=3) if x["op"] == "item_replace_file" and D in (x["affected_slugs"] or [])],
      set(storage_files()) ^ set(files2))

# ===== 6. the object page ============================================================================
print("--- 6. the object page ---")
page = {k: C[k].get(f"/object/{R}").text for k in ("A", "E", "V")}
check("page: editor sees Replace file...", 'id="replace-file-btn"' in page["E"] and "Replace file" in page["E"])
check("page: admin sees Replace file...", 'id="replace-file-btn"' in page["A"])
check("page: viewer does not", 'id="replace-file-btn"' not in page["V"])
check("page: a redacted item does not offer it", 'id="replace-file-btn"' not in C["A"].get(f"/object/{X}").text)
check("page: a content-only item does not offer it", 'id="replace-file-btn"' not in C["A"].get("/object/rf-yt").text)

print("--- the tool is registered with its documented signature ---")
import inspect  # noqa: E402
sig = inspect.signature(mcp.constructicon_replace_file)
check("constructicon_replace_file(slug, content_base64, filename=None)",
      list(sig.parameters) == ["slug", "content_base64", "filename"] and sig.parameters["filename"].default is None, sig)
check("it is in the wrapped-tools registry (actor + error shape applied)", "constructicon_replace_file" in mcp._WRAPPED_TOOLS)

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")
