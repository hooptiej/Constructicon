#!/usr/bin/env python3
"""#602 (.vpptoken, .jfif/.jpe, content-mismatch refusal), #600 (firmware `log`), #601 (uploader
source zip). Throwaway DB + storage (scripts/_testenv.py), the real FastAPI app through TestClient,
no server.

    python scripts/test_types_602.py

1. .vpptoken: a fake token file (base64 JSON with a dummy token) is generated at runtime. Uploads as
   a restricted `vpptoken` item with ONLY the org name and expiry stored; "expires soon" / "expired"
   labels; the token value appears NOWHERE outside the stored file: item JSON, the object page,
   properties, search, the audit log and change log, every row of every table, the admin restricted
   list, the MCP get / search / list_restricted / download-metadata. A file that won't parse is kept
   as an opaque restricted file with a note, not refused.
2. Restricted behaviour: a viewer and an anonymous visitor get 404 at every door (page, JSON, file,
   thumbnail), and the policy keeps it out of exports and browsing; the install token (admin) sees it.
3. .jfif and .jpe upload as images with a thumbnail and pending OCR.
4. A text file renamed .msi is refused 400 with the real reason (not "Unsupported file type");
   a truly unknown extension keeps the old wording; the shared error shape holds.
5. #600: firmware.extract_text_for_row's failure path logs instead of raising NameError.
6. #601: the uploader-source zip holds constructicon_uploader/api.py; a tree without it answers 503
   instead of an empty zip; compose/Dockerfile carry the mount/copy; the download page names
   "Set Install Token...".

No executables or installer-shaped binaries are created: the .msi case is a text file.
Exits 1 if any check fails.
"""

import base64
import datetime
import io
import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("CAPTION_DISABLED", "1")

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("types602-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = Path(HERE).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, ingest, object_types, paths, policy, users  # noqa: E402
_testenv.assert_isolated()
from core.object_types import firmware, vpptoken  # noqa: E402
from web import app as webapp  # noqa: E402
from web.routes import files as files_routes  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None  # no OCR/caption threads: this test is about types

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
admin = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)
anon = TestClient(webapp.app, base_url=BASE, follow_redirects=False)

# a viewer with a real session
VIEWER_PW = "t602-" + secrets.token_urlsafe(14)
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("viewer602", VIEWER_PW, "viewer")
viewer = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
r = viewer.post("/api/auth/login", json={"username": "viewer602", "password": VIEWER_PW}, headers=SAME)
check("fixture: the viewer signs in", r.status_code == 200, r.text[:150])


def token_file(org, days, token):
    """A fake .vpptoken: base64 of JSON, the shape ABM produces. Never a real credential."""
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days, hours=1)
    body = {"token": token, "expDate": exp.strftime("%Y-%m-%dT%H:%M:%S+0000"), "orgName": org}
    return base64.b64encode(json.dumps(body).encode())


def upload(client, name, data, **kw):
    return client.post("/api/upload", files={"file": (name, data, "application/octet-stream")}, **kw)


# ---- 1. .vpptoken ---------------------------------------------------------------------------------
print("--- 1. .vpptoken: org + expiry only, the token nowhere ---")
SECRET = "SECRET-" + secrets.token_hex(24)
SECRET2 = "SECRET-" + secrets.token_hex(24)
raw_ok = token_file("Acme Test Corporation", 20, SECRET)
raw_expired = token_file("Long Gone Ltd", -40, SECRET2)
raw_far = token_file("Far Future Inc", 300, "SECRET-" + secrets.token_hex(24))
raw_bad = b"this is not a token file at all, just words"
raw_notvpp = base64.b64encode(json.dumps({"hello": "world"}).encode())

r = upload(admin, "sToken_for_Acme (1).vpptoken", raw_ok)
check(".vpptoken uploads (200)", r.status_code == 200, r.text[:200])
ITEM = r.json()
SLUG = ITEM["slug"]
check("it is typed vpptoken", ITEM["media_type"] == "vpptoken", ITEM.get("media_type"))
check("the type is restricted and registered as such",
      object_types.OBJECT_TYPES["vpptoken"].restricted and "vpptoken" in object_types.restricted_types())
check("no OCR, no caption, no thumbnail",
      not object_types.OBJECT_TYPES["vpptoken"].ocr_capable and not object_types.OBJECT_TYPES["vpptoken"].caption_capable
      and object_types.OBJECT_TYPES["vpptoken"].thumbnail_source == object_types.ThumbnailSource.NONE)

row = db.get_by_slug(SLUG)
stats = (row["type_metadata"] or {}).get(vpptoken.STATS_KEY) or {}
check("type_metadata holds exactly parsed/org/expires", set(stats) == {"parsed", "org", "expires"}, stats)
check("org name read", stats.get("org") == "Acme Test Corporation", stats)
exp_days = (stats["expires"] - datetime.datetime.now(datetime.timezone.utc).timestamp()) / 86400
check("expiry read (~20 days out)", 19 < exp_days < 21.5, exp_days)
check("extracted_text is empty (not searchable by content)", (row.get("extracted_text") or "") == "", row.get("extracted_text"))
check("ocr_status is not pending (no OCR for this type)", row.get("ocr_status") in (None, "", "skipped", "done"), row.get("ocr_status"))
check("no embedding was computed", not row.get("embedding"))
stored = paths.storage_dir() / row["stored_filename"]
check("sanity: the stored file DOES hold the token (so the grep below means something)",
      SECRET.encode() in base64.b64decode(stored.read_bytes()))

props = vpptoken.get_properties(row)
check("properties: organization + soon status", props.get("Organization") == "Acme Test Corporation"
      and "expires soon" in props.get("Status", "") and "days left" in props["Status"], props)
check("properties: the first one (admin list hint) names org and status",
      next(iter(props.values())).startswith("Acme Test Corporation:"), props)
check("status ok / soon / expired / unknown wording",
      vpptoken.expiry_status(stats["expires"] + 100 * 86400)[0] == "ok"
      and vpptoken.expiry_status(stats["expires"])[0] == "soon"
      and vpptoken.expiry_status(stats["expires"] - 86400 * 30)[0] == "expired"
      and vpptoken.expiry_status(None)[0] == "unknown")

r = upload(admin, "expired.vpptoken", raw_expired)
EXP_SLUG = r.json()["slug"]
exp_props = vpptoken.get_properties(db.get_by_slug(EXP_SLUG))
check("an expired token reads EXPIRED", r.status_code == 200 and "EXPIRED" in exp_props.get("Status", ""), exp_props)
r = upload(admin, "far.vpptoken", raw_far)
check("a far-off token reads plain 'expires' (not soon)", r.status_code == 200
      and "soon" not in vpptoken.get_properties(db.get_by_slug(r.json()["slug"])).get("Status", ""))

# the leak grep: everywhere the token could end up
haystacks = {}
haystacks["POST /api/upload response"] = r_ok = json.dumps(ITEM)
haystacks["GET /api/image/<slug>"] = admin.get(f"/api/image/{SLUG}").text
haystacks["GET /object/<slug>"] = admin.get(f"/object/{SLUG}").text
haystacks["GET /api/search (by org)"] = admin.get("/api/search", params={"q": "Acme"}).text
haystacks["GET /api/search (by the token)"] = admin.get("/api/search", params={"q": SECRET}).text
haystacks["GET /api/restricted"] = admin.get("/api/restricted").text
haystacks["GET /admin"] = admin.get("/admin").text
haystacks["GET /api/audit-log"] = admin.get("/api/audit-log").text
haystacks["properties()"] = json.dumps(props)
haystacks["preview"] = str(object_types.OBJECT_TYPES["vpptoken"].preview_fn(
    object_types.PreviewContext(item=ITEM, media_url=f"/f/{SLUG}", thumb_url=None, page_url=None, file_path=stored)))
con = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
dump = []
for (tbl,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    for rec in con.execute(f'SELECT * FROM "{tbl}"').fetchall():
        dump.append(f"{tbl}: {rec!r}")
con.close()
haystacks["every row of every table (incl. audit_log, change log)"] = "\n".join(dump)
dbfile = Path(os.environ["CONSTRUCTICON_DB_PATH"])
haystacks["raw DB file bytes"] = "".join(p.read_bytes().decode("latin-1") for p in dbfile.parent.glob(dbfile.name + "*"))

from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)
with actor.acting_as(actor.ACTOR_MCP):
    haystacks["MCP constructicon_get"] = json.dumps(mcp.constructicon_get(SLUG), default=str)
    haystacks["MCP constructicon_search"] = json.dumps(mcp.constructicon_search("Acme"), default=str)
    haystacks["MCP constructicon_list_restricted"] = json.dumps(mcp.constructicon_list_restricted(), default=str)
    haystacks["MCP constructicon_explain/get_related"] = json.dumps(mcp.constructicon_get_related(SLUG), default=str)
for where, text in haystacks.items():
    check(f"token value absent from: {where}", SECRET not in text and SECRET2 not in text, f"found in {where}")
    # the base64 of the whole file is equally forbidden outside the stored file
    check(f"raw file blob absent from: {where}", raw_ok.decode() not in text, f"found in {where}")
check("(the object page and admin list do show the org and status)",
      "Acme Test Corporation" in haystacks["GET /object/<slug>"] and "Acme Test Corporation" in haystacks["GET /api/restricted"],
      "the facts are the point")

# a file that won't parse is kept, opaque, with a note
for name, data in (("opaque.vpptoken", raw_bad), ("notvpp.vpptoken", raw_notvpp)):
    r = upload(admin, name, data)
    check(f"unparseable {name}: kept (200), not refused", r.status_code == 200 and r.json()["media_type"] == "vpptoken",
          r.text[:160])
    row = db.get_by_slug(r.json()["slug"])
    p = vpptoken.get_properties(row)
    check(f"unparseable {name}: restricted, parsed False, note shown",
          object_types.is_restricted(row) and row["type_metadata"][vpptoken.STATS_KEY] == {"parsed": False}
          and "Couldn't read" in p.get("Note", ""), p)

# ---- 2. restricted behaviour ----------------------------------------------------------------------
print("--- 2. restricted: 404 at every door for a viewer and for anonymous ---")


def is_404(r):
    try:
        body = r.json()
    except ValueError:
        return False
    return r.status_code == 404 and body.get("ok") is False and body.get("error", {}).get("code") == "not_found"


DOORS = [f"/object/{SLUG}", f"/api/image/{SLUG}", f"/api/image/{SLUG}/revisions", f"/api/image/{SLUG}/similar",
         f"/f/{SLUG}", f"/f/{SLUG}/thumb"]
for who, c in (("viewer", viewer), ("anonymous", anon)):
    for path in DOORS:
        r = c.get(path)
        # an anonymous page request is turned to /login before the item is looked at: either way no item
        ok = is_404(r) or (who == "anonymous" and r.status_code in (302, 401) and SECRET not in r.text)
        check(f"{who} GET {path} -> no item", ok, f"{r.status_code} {r.text[:100]}")
    r = c.get("/api/search", params={"q": "Acme"})
    check(f"{who} search does not list it", SLUG not in r.text, r.status_code)
    r = c.get("/api/restricted")
    check(f"{who} cannot read the restricted list", r.status_code in (401, 403) and SLUG not in r.text, r.status_code)
for path in DOORS[:2] + [f"/f/{SLUG}"]:
    r = admin.get(path)
    check(f"install token (admin) GET {path} -> 200", r.status_code == 200, r.status_code)
r = viewer.post(f"/api/image/{SLUG}", data={"display_name": "x"}, headers=SAME)
check("viewer write refused", r.status_code in (403, 404), r.status_code)
rows = [db.get_by_slug(SLUG)]
check("never exported: filter_exportable drops it", policy.filter_exportable(rows) == [])
check("browsing hides it: the browse SQL clause excludes the type",
      "'vpptoken'" in policy.sql_browse_clause("capture_events."), policy.sql_browse_clause("capture_events."))

# ---- 3. .jfif / .jpe ------------------------------------------------------------------------------
print("--- 3. .jfif and .jpe are JPEGs ---")
buf = io.BytesIO()
Image.new("RGB", (32, 24), (200, 40, 40)).save(buf, "JPEG")
for ext in (".jfif", ".jpe"):
    r = upload(admin, f"photo{ext}", buf.getvalue() + secrets.token_bytes(4))
    check(f"{ext} upload 200", r.status_code == 200, r.text[:200])
    j = r.json()
    check(f"{ext} is typed image", j["media_type"] == "image", j.get("media_type"))
    t = admin.get(f"/f/{j['slug']}/thumb")
    check(f"{ext} has a thumbnail (200 image/jpeg)", t.status_code == 200 and t.headers["content-type"].startswith("image/"),
          f"{t.status_code} {t.headers.get('content-type')}")
    f = admin.get(f"/f/{j['slug']}")
    check(f"{ext} is served as image/jpeg", f.headers["content-type"].startswith("image/jpeg"), f.headers.get("content-type"))
    # OCR ran or was queued like a .jpg's (here tesseract may be absent, so "failed" is the same
    # best-effort outcome a .jpg gets): what matters is the type took the OCR path at all
    check(f"{ext} takes the OCR path like a .jpg", db.get_by_slug(j["slug"])["ocr_status"] in ("pending", "done", "failed"),
          db.get_by_slug(j["slug"])["ocr_status"])
check("the upload picker accepts them", {".jfif", ".jpe"} <= set(object_types.accepted_extensions()))

# ---- 4. content mismatch --------------------------------------------------------------------------
print("--- 4. a supported extension with the wrong contents says why ---")
r = upload(admin, "setup.msi", b"just some text, definitely not an installer package\n")
body = r.json()
check(".msi that isn't one: 400", r.status_code == 400, r.status_code)
check("...names the real reason", "isn't a valid Windows Installer package" in body.get("detail", "")
      and ".msi" in body["detail"], body.get("detail"))
check("...not the 'unsupported' wording", "Unsupported file type" not in body.get("detail", ""), body.get("detail"))
check("...keeps the shared error shape (ok false, error.code bad_request, detail)",
      body.get("ok") is False and body.get("error", {}).get("code") == "bad_request"
      and body["error"].get("message") == body["detail"], body)
leftover = [p.name for p in paths.storage_dir().iterdir() if p.is_file() and p.suffix == ".msi"]
check("...and nothing is left in storage", not leftover, leftover)
for ext, noun in ((".deb", "Debian package"), (".pkg", "macOS installer package"), (".exe", "Windows executable")):
    r = upload(admin, f"x{ext}", b"plain text pretending\n")
    check(f"{ext} mismatch mentions {noun!r}", r.status_code == 400 and noun in r.json().get("detail", ""),
          r.json().get("detail"))
r = upload(admin, "mystery.xyz", b"abc")
check("a truly unknown extension keeps the old wording", r.status_code == 400
      and r.json()["detail"] == "Unsupported file type: .xyz", r.json().get("detail"))
check("mismatch_reason falls back for an unclaimed extension",
      object_types.mismatch_reason("a.zzz") == "Unsupported file type: .zzz")
mcp_out = None
with actor.acting_as(actor.ACTOR_MCP):
    try:
        mcp_out = mcp.constructicon_upload(filename="setup.msi", content_base64=base64.b64encode(b"text").decode())
    except Exception as e:  # the tool wrapper turns AppError into a result; a raise is also fine
        mcp_out = {"error": {"message": str(e)}}
check("the MCP upload gives the same reason", "valid Windows Installer package" in json.dumps(mcp_out, default=str), mcp_out)

# ---- 5. #600 firmware log -------------------------------------------------------------------------
print("--- 5. #600: firmware's extraction failure logs, never NameError ---")
check("firmware.log is a logger", isinstance(firmware.log, logging.Logger), type(getattr(firmware, "log", None)))
records = []


class _Grab(logging.Handler):
    def emit(self, record):
        records.append(record)


h = _Grab(level=logging.WARNING)
firmware.log.addHandler(h)
orig_stats = firmware._stats_for
firmware._stats_for = lambda row: (_ for _ in ()).throw(RuntimeError("forced failure"))
try:
    out = firmware.extract_text_for_row({"filename": "board.hex", "slug": "fw-600"})
except NameError as e:
    out = f"NameError: {e}"
finally:
    firmware._stats_for = orig_stats
    firmware.log.removeHandler(h)
check("the failure path returns '' (degrades, does not raise)", out == "", out)
check("...and logged a warning naming the site", any("extract_text_for_row" in r.getMessage() for r in records),
      [r.getMessage() for r in records])

# ---- 6. #601 uploader source zip ------------------------------------------------------------------
print("--- 6. #601: the uploader-source zip is not empty ---")
r = admin.get("/downloads/constructicon-uploader-source.zip")
check("zip download 200", r.status_code == 200 and r.headers["content-type"] == "application/zip", r.status_code)
names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
check("zip holds constructicon_uploader/api.py",
      "constructicon-uploader-source/constructicon_uploader/api.py" in names, names[:8])
check("zip holds run.py and the README too", {"constructicon-uploader-source/run.py", "constructicon-uploader-source/README.md"} <= set(names))
check("no __pycache__ inside it", not any("__pycache__" in n for n in names))
real_dir = files_routes.DESKTOP_APP_DIR
for label, d in (("an empty directory", Path(tempfile.mkdtemp(prefix="types602-empty-"))),
                 ("a missing directory", Path(tempfile.gettempdir()) / "types602-nope")):
    files_routes.DESKTOP_APP_DIR = d
    try:
        r = admin.get("/downloads/constructicon-uploader-source.zip")
    finally:
        files_routes.DESKTOP_APP_DIR = real_dir
    check(f"{label}: 503 uploader_source_missing, not an empty zip",
          r.status_code == 503 and r.json().get("error", {}).get("code") == "uploader_source_missing", f"{r.status_code} {r.text[:120]}")
compose = (ROOT / "docker-compose.yml.example").read_text()
check("compose mounts ./desktop_app read-only into the web service", "./desktop_app:/app/desktop_app:ro" in compose)
check("Dockerfile bakes a fallback copy", "COPY desktop_app /app/desktop_app" in (ROOT / "Dockerfile").read_text())
page = admin.get("/account")
check("the download page (/account) tells you to Set Install Token...",
      page.status_code == 200 and "Set Install Token" in page.text, page.status_code)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
