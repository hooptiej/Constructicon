#!/usr/bin/env python3
"""Self-contained check for #703 ("Raw deal"): the code preview's "Open raw" shows the source as text.

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient, real
sessions for an admin, two editors and a viewer (test_ownership_sensitive's pattern), fixtures made here:

    python scripts/test_raw_view_703.py

Covers `/f/<slug>/raw`:
- a .py upload -> 200, `text/plain; charset=utf-8`, inline, nosniff, the sandbox CSP, the original name
  (plain and RFC 5987), the bytes unchanged; a UTF-16 source transcoded to UTF-8;
- HTML disguised as .py (and an SVG-looking .js) -> never text/html, the CSP on, shown as text;
- a binary "code" file and one over the size cap -> the download fallback (what /f/<slug> serves);
- a type without a raw view (a .txt) -> exactly /f/<slug>'s answer;
- roles and policy: a viewer gets it, anonymous is sent to sign in, a sensitive item is 404 for an
  editor who isn't its uploader and 200 for the uploader and the admin, a redacted one 404 / 410;
- the code preview's link: "Open raw" -> /f/<slug>/raw in a new tab when the file is text, absent for a
  binary file and for .html (Download stays), and the contract helper (`object_types.raw_text_codec`).
Exits 1 if any check fails.
"""
import os
import re
import secrets
import sys
import types
from urllib.parse import quote

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("raw703-")
os.environ["CAPTION_DISABLED"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine; nothing here draws an SVG
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, object_types, storage, users  # noqa: E402
from core.object_types import code as code_type  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp, content_security  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}
CSP = content_security.RENDERED_HTML_CSP


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def new_client():
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False, raise_server_exceptions=False)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


db.init_db()
users.limiter.reset()

PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("rw_admin", "admin"), ("rw_ed1", "editor"), ("rw_ed2", "editor"), ("rw_view", "viewer")):
        PW[name] = "rw-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
C = {"anonymous": new_client(),
     "token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False, raise_server_exceptions=False)}
for key, name in (("A", "rw_admin"), ("E1", "rw_ed1"), ("E2", "rw_ed2"), ("V", "rw_view")):
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    check(f"{key} signs in", r.status_code == 200, r.text[:200])
    C[key] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in ("A", "E1", "E2", "V") else dict(SAME)) for k, c in C.items()}


def upload(name, data, who="token", sensitive=False, ctype="application/octet-stream"):
    fields = {"description": f"raw703 {name}"}
    if sensitive:
        fields["sensitive"] = "true"
    r = C[who].post("/api/upload", files={"file": (name, data, ctype)}, data=fields, headers=H[who])
    assert r.status_code == 200, (name, r.status_code, r.text[:300])
    return r.json().get("slug") or r.json().get("object", {}).get("slug")


tok = C["token"]

# ---- 1. a plain .py ------------------------------------------------------------------------------
print("--- 1. a .py upload ---")
PY = b"#!/usr/bin/env python3\n# raw703 sample\nprint('hello, raw')\n"
S = {"py": upload("blink_led.py", PY)}
check("the .py is a code item", db.get_by_slug(S["py"])["media_type"] == "code", db.get_by_slug(S["py"])["media_type"])
r = tok.get(f"/f/{S['py']}/raw")
h = r.headers
check("raw .py: 200", r.status_code == 200, r.status_code)
check("raw .py: text/plain; charset=utf-8", h.get("content-type") == "text/plain; charset=utf-8", h.get("content-type"))
check("raw .py: inline with the original name", h.get("content-disposition") == 'inline; filename="blink_led.py"',
      h.get("content-disposition"))
check("raw .py: nosniff", h.get("x-content-type-options") == "nosniff", dict(h))
check("raw .py: the sandbox CSP", h.get("content-security-policy") == CSP, h.get("content-security-policy"))
check("raw .py: the bytes unchanged", r.content == PY, r.content[:80])
check("raw .py: no app CSP / framing headers (it's under /f)", "x-frame-options" not in h, dict(h))
f = tok.get(f"/f/{S['py']}")
check("/f .py is still the download (unchanged by #703)", f.headers.get("content-disposition", "").startswith("attachment;"),
      f.headers.get("content-disposition"))

S["spaced"] = upload("my sketch (v2).ino", b"void setup() {}\nvoid loop() {}\n")
r = tok.get(f"/f/{S['spaced']}/raw")
check("raw .ino with spaces: RFC 5987 name", r.status_code == 200
      and r.headers.get("content-disposition") == "inline; filename*=utf-8''" + quote("my sketch (v2).ino"),
      r.headers.get("content-disposition"))

U16 = "// raw703 utf16 Malmö\nint x = 1;\n"
S["u16"] = upload("wide.c", "﻿".encode("utf-16-le") + U16.encode("utf-16-le"))
r = tok.get(f"/f/{S['u16']}/raw")
check("raw UTF-16 .c: 200 text/plain; charset=utf-8", r.status_code == 200
      and r.headers.get("content-type") == "text/plain; charset=utf-8", (r.status_code, r.headers.get("content-type")))
check("raw UTF-16 .c: transcoded to UTF-8", r.content.decode("utf-8").lstrip("﻿") == U16, r.content[:60])
check("raw UTF-16 .c: nosniff + CSP", r.headers.get("x-content-type-options") == "nosniff"
      and r.headers.get("content-security-policy") == CSP)

# ---- 2. markup in disguise -----------------------------------------------------------------------
print("--- 2. markup in disguise ---")
EVIL = b"<!DOCTYPE html><html><body><script>alert(document.cookie)</script></body></html>\n"
S["evil"] = upload("innocent.py", EVIL)
check("the #610 sniff calls it HTML", content_security.active_kind(storage.path_for(db.get_by_slug(S["evil"])["stored_filename"]),
                                                                    "innocent.py") == "html")
for who in ("token", "V"):
    r = C[who].get(f"/f/{S['evil']}/raw")
    ct = r.headers.get("content-type", "")
    check(f"HTML-as-.py raw/{who}: 200, text/plain, never text/html", r.status_code == 200 and ct == "text/plain; charset=utf-8"
          and "html" not in ct, (r.status_code, ct))
    check(f"HTML-as-.py raw/{who}: the sandbox CSP + nosniff", r.headers.get("content-security-policy") == CSP
          and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
SVGJS = b"// <svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>\nvar s = 1;\n"
S["svgjs"] = upload("draw.js", SVGJS)
r = tok.get(f"/f/{S['svgjs']}/raw")
check("SVG-looking .js raw: text/plain + CSP + nosniff", r.headers.get("content-type") == "text/plain; charset=utf-8"
      and r.headers.get("content-security-policy") == CSP and r.headers.get("x-content-type-options") == "nosniff",
      dict(r.headers))

# ---- 3. download fallbacks -----------------------------------------------------------------------
print("--- 3. download fallbacks ---")
BIN = bytes(range(256)) * 1200  # 300 KB with NULs and every control byte: not text
S["bin"] = upload("firmware.c", BIN)
check("binary .c is a code item", db.get_by_slug(S["bin"])["media_type"] == "code", db.get_by_slug(S["bin"])["media_type"])
bin_path = storage.path_for(db.get_by_slug(S["bin"])["stored_filename"])
check("contract: binary .c has no raw codec", object_types.raw_text_codec(object_types.get_object_type("code"), bin_path) is None)
r = tok.get(f"/f/{S['bin']}/raw")
f = tok.get(f"/f/{S['bin']}")
# What /f/<slug> answers for a .c depends on the platform's mimetypes table (Linux: text/x-csrc, a download;
# Windows' registry may say text/plain), so the fallback is checked as "exactly /f/<slug>'s answer".
check("binary .c raw: 200, exactly /f/<slug>'s answer (disposition, type, bytes)", r.status_code == 200
      and r.headers.get("content-disposition") == f.headers.get("content-disposition")
      and r.headers.get("content-type") == f.headers.get("content-type") and r.content == f.content == BIN,
      (r.headers.get("content-disposition"), r.headers.get("content-type"), f.headers.get("content-type")))
check("binary .c raw: not the raw-text view (no sandbox CSP added)", "content-security-policy" not in r.headers, dict(r.headers))
if os.name != "nt":
    check("binary .c raw (Linux): a download under its name", r.headers.get("content-disposition") == 'attachment; filename="firmware.c"',
          r.headers.get("content-disposition"))
check("binary .c raw: nosniff", r.headers.get("x-content-type-options") == "nosniff")

py_path = storage.path_for(db.get_by_slug(S["py"])["stored_filename"])
check("contract: the .py's raw codec is utf-8", object_types.raw_text_codec(object_types.get_object_type("code"), py_path) == "utf-8")
saved = code_type.RAW_MAX_BYTES
code_type.RAW_MAX_BYTES = len(PY) - 1  # the cap, without writing a 32 MB fixture
try:
    check("contract: over the cap -> no raw codec", code_type.raw_text_codec(py_path) is None)
    r = tok.get(f"/f/{S['py']}/raw")
    check("over the cap: raw falls back to the download", r.status_code == 200
          and r.headers.get("content-disposition", "").startswith("attachment;"), r.headers.get("content-disposition"))
finally:
    code_type.RAW_MAX_BYTES = saved

S["txt"] = upload("notes.txt", b"plain words\n", ctype="text/plain")
txt_spec = object_types.get_object_type(db.get_by_slug(S["txt"])["media_type"])
check("contract: a type without raw_text_fn has no raw view", txt_spec.raw_text_fn is None
      and object_types.raw_text_codec(txt_spec, py_path) is None, txt_spec.key)
r, f = tok.get(f"/f/{S['txt']}/raw"), tok.get(f"/f/{S['txt']}")
check("a .txt's raw = exactly what /f serves", r.status_code == f.status_code == 200 and r.content == f.content
      and r.headers.get("content-disposition") == f.headers.get("content-disposition")
      and r.headers.get("content-type") == f.headers.get("content-type"), (r.headers.get("content-disposition"), f.headers.get("content-disposition")))
check("contract: raw_url", object_types.raw_url("abc") == "/f/abc/raw")

# ---- 4. roles and policy -------------------------------------------------------------------------
print("--- 4. roles and policy ---")
check("viewer: 200", C["V"].get(f"/f/{S['py']}/raw").status_code == 200)
r = C["anonymous"].get(f"/f/{S['py']}/raw")
check("anonymous: sent to sign in (302 /login), no bytes", r.status_code == 302 and "/login" in r.headers.get("location", "")
      and PY not in r.content, (r.status_code, r.headers.get("location")))
check("missing slug: 404", tok.get("/f/no-such-slug-703/raw").status_code == 404)
S["priv"] = upload("secret_tool.py", b"API = 'not really'\n", who="E1", sensitive=True)
check("private (sensitive) .py: E2 (another editor) 404", C["E2"].get(f"/f/{S['priv']}/raw").status_code == 404)
check("private .py: viewer 404", C["V"].get(f"/f/{S['priv']}/raw").status_code == 404)
check("private .py: E2 gets the same 404 as /f", C["E2"].get(f"/f/{S['priv']}").status_code == 404)
check("private .py: the uploader (E1) 200", C["E1"].get(f"/f/{S['priv']}/raw").status_code == 200)
check("private .py: admin 200 and token 200", C["A"].get(f"/f/{S['priv']}/raw").status_code == 200
      and tok.get(f"/f/{S['priv']}/raw").status_code == 200)
conn = db.get_conn()
try:
    hows = [r["how"] for r in conn.execute("SELECT how FROM item_access_log WHERE slug = ?", (S["priv"],))]
finally:
    conn.close()
check("private .py: raw opens are in the view log (as 'file')", "file" in hows, hows)
S["red"] = upload("redact_me.py", b"x = 1\n")
r = C["A"].post(f"/api/image/{S['red']}/redact", headers=H["A"])
check("redact a .py (200)", r.status_code == 200, r.text[:200])
check("redacted .py raw: viewer 404, admin 410", C["V"].get(f"/f/{S['red']}/raw").status_code == 404
      and C["A"].get(f"/f/{S['red']}/raw").status_code == 410,
      (C["V"].get(f"/f/{S['red']}/raw").status_code, C["A"].get(f"/f/{S['red']}/raw").status_code))

# ---- 5. the code preview's link ------------------------------------------------------------------
print("--- 5. the code preview's link ---")
BIGPY = "".join(f"value_{i} = {i}  # raw703 line\n" for i in range(3000)).encode()
S["bigpy"] = upload("long_table.py", BIGPY)
page = C["V"].get(f"/object/{S['bigpy']}").text
m = re.search(r'<a href="([^"]+)"([^>]*)>Open raw</a>', page)
check("big .py page: an Open raw link", m is not None, re.findall(r"code-preview-note.{0,300}", page)[:1])
if m:
    check("big .py: Open raw -> /f/<slug>/raw", m.group(1) == f"/f/{S['bigpy']}/raw", m.group(1))
    check("big .py: Open raw opens a new tab (rel=noopener)", 'target="_blank"' in m.group(2) and 'rel="noopener"' in m.group(2), m.group(2))
    r = C["V"].get(m.group(1))
    check("big .py: the link's target answers text/plain inline", r.status_code == 200
          and r.headers.get("content-type") == "text/plain; charset=utf-8"
          and r.headers.get("content-disposition", "").startswith("inline;") and r.content == BIGPY,
          (r.status_code, r.headers.get("content-type"), r.headers.get("content-disposition")))
check("big .py: Download link kept", f'<a href="/f/{S["bigpy"]}" download>Download</a>' in page)
bpage = C["V"].get(f"/object/{S['bin']}").text
check("binary .c page: no Open raw (download only)", "Open raw" not in bpage
      and (f'<a href="/f/{S["bin"]}" download>Download</a>' in bpage or "code-preview-note" not in bpage),
      re.findall(r"code-preview-note.{0,200}", bpage)[:1])
BIGHTML = ("<!DOCTYPE html><html><body>\n" + "".join(f"<p>raw703 {i}</p>\n" for i in range(3000)) + "</body></html>\n").encode()
S["bightml"] = upload("big_page.html", BIGHTML)
hpage = C["V"].get(f"/object/{S['bightml']}").text
check("big .html page: no Open raw (it has Rendered / Code), Download kept", "Open raw" not in hpage
      and "download>Download</a>" in hpage)

if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
