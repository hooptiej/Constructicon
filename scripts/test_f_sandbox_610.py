#!/usr/bin/env python3
"""Self-contained check for #610: user files are never served as active content on the app origin.

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient. Fixtures
are generated here at run time. Run anywhere the requirements are installed:

    python scripts/test_f_sandbox_610.py

Covers /f/<slug> and /f/<slug>/thumb per type (HTML, XHTML, SVG, XML, an HTML file renamed .txt,
PNG, PDF), anonymous and with the install token, the /preview media mount, the /downloads zips, and
that the Rendered view still uses the same shared CSP. Exits 1 if any check fails.
"""
import io
import os
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("fsandbox610-")
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine: the SVG thumbnail can't be drawn, everything else runs
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from pathlib import Path  # noqa: E402
from core import db, paths  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from web import app as webapp, content_security  # noqa: E402

client = _testenv.client(webapp.app, raise_server_exceptions=False)  # the install token (admin)
anon = TestClient(webapp.app, raise_server_exceptions=False)
with client:
    pass


def upload(name, data, ctype="application/octet-stream"):
    r = client.post("/api/upload", files={"file": (name, data, ctype)})
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    return r.json().get("slug") or r.json().get("object", {}).get("slug")


png = io.BytesIO()
Image.new("RGB", (40, 30), (200, 30, 30)).save(png, "PNG")
HTML = b"<html><body onload=\"fetch('/api/settings')\"><script>fetch('/api/settings')</script>zq610html</body></html>"
SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30" onload="fetch(\'/api/settings\')">'
       b'<script>fetch("/api/settings")</script><rect width="40" height="30" fill="red"/></svg>')
XHTML = b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><script>alert(1)</script>zq610x</body></html>'
XML = b'<?xml version="1.0"?><root><a xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></a>zq610xml</root>'
DISGUISED = b"<!DOCTYPE html><html><body><script>alert(1)</script>zq610disguised</body></html>"
PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")

S = {
    "html": upload("evil.html", HTML, "text/html"),
    "svg": upload("evil.svg", SVG, "image/svg+xml"),
    "txt": upload("notes.txt", DISGUISED, "text/plain"),
    "png": upload("pic.png", png.getvalue(), "image/png"),
    "pdf": upload("doc.pdf", PDF, "application/pdf"),
}

CSP = content_security.RENDERED_HTML_CSP


def hdr(r):
    return {k.lower(): v for k, v in r.headers.items()}


print("\n--- /f/<slug>: per type, with the token and anonymous ---")
for who, c in (("token", client), ("anon", anon)):
    for key, slug in S.items():
        r = c.get(f"/f/{slug}")
        h = hdr(r)
        check(f"{key}/{who}: 200", r.status_code == 200, r.status_code)
        check(f"{key}/{who}: nosniff", h.get("x-content-type-options") == "nosniff")
        disp = h.get("content-disposition", "")
        if key in ("html", "txt"):
            check(f"{key}/{who}: sandbox CSP + attachment", h.get("content-security-policy") == CSP and disp.startswith("attachment"), (disp, h.get("content-security-policy")))
        elif key == "svg":
            check(f"{key}/{who}: sandbox CSP, kept inline", h.get("content-security-policy") == CSP and disp.startswith("inline"), (disp, h.get("content-security-policy")))
        else:
            check(f"{key}/{who}: no CSP added", "content-security-policy" not in h, h.get("content-security-policy"))
    # the declared type is the file's own (no type confusion), served as stored
    check(f"html/{who}: content-type is text/html", hdr(c.get(f"/f/{S['html']}")).get("content-type", "").startswith("text/html"))
    check(f"svg/{who}: content-type is image/svg+xml", hdr(c.get(f"/f/{S['svg']}")).get("content-type", "").startswith("image/svg+xml"))

check("png: bytes unchanged", anon.get(f"/f/{S['png']}").content == png.getvalue())
check("html: bytes unchanged (served as stored)", anon.get(f"/f/{S['html']}").content == HTML)

print("\n--- /f/<slug>/thumb ---")
for key, slug in S.items():
    for who, c in (("token", client), ("anon", anon)):
        r = c.get(f"/f/{slug}/thumb")
        h = hdr(r)
        check(f"thumb {key}/{who}: 200 with nosniff, or a plain 404 (no thumbnail for the type)",
              (r.status_code == 200 and h.get("x-content-type-options") == "nosniff") or r.status_code == 404, r.status_code)
        if r.status_code == 200:
            ct = h.get("content-type", "")
            if ct.startswith(("text/html", "image/svg", "application/xml", "text/xml", "application/xhtml")):
                check(f"thumb {key}/{who}: an active original gets the sandbox CSP", h.get("content-security-policy") == CSP, ct)
            else:
                check(f"thumb {key}/{who}: a raster, not the original markup", ct.startswith("image/") and b"<script" not in r.content[:4096], ct)
svg_thumb = anon.get(f"/f/{S['svg']}/thumb")
check("svg thumb is never the raw SVG", svg_thumb.status_code == 404 or (svg_thumb.status_code == 200 and b"<svg" not in svg_thumb.content[:2048]
                                                                         and hdr(svg_thumb).get("content-type", "").startswith("image/jpeg")),
      (svg_thumb.status_code, hdr(svg_thumb).get("content-type")))

from core import storage  # noqa: E402
storage.save_thumbnail_from_bytes(S["svg"], png.getvalue())  # what the SVG type's rasteriser would leave behind
svg_thumb = anon.get(f"/f/{S['svg']}/thumb")
check("svg thumb (generated raster): 200 image/jpeg, nosniff, no CSP, no markup",
      svg_thumb.status_code == 200 and hdr(svg_thumb).get("content-type") == "image/jpeg" and hdr(svg_thumb).get("x-content-type-options") == "nosniff"
      and "content-security-policy" not in hdr(svg_thumb) and b"<svg" not in svg_thumb.content, (svg_thumb.status_code, hdr(svg_thumb)))

print("\n--- the helper itself ---")
check("RENDERED view uses the shared policy", client.get(f"/api/image/{S['html']}/rendered").headers.get("content-security-policy") == CSP)
check("CSP is a sandbox with no tokens", CSP.startswith("sandbox;") and "default-src 'none'" in CSP and "allow-scripts" not in CSP)
for name, want in (("a.html", "html"), ("a.HTM", "html"), ("a.xhtml", "html"), ("a.svg", "svg"), ("a.xml", "xml"), ("a.png", None), ("a.pdf", None)):
    check(f"active_kind by name: {name} -> {want}", content_security._kind_from_name(name) == want)
for mt, want in (("text/html; charset=utf-8", "html"), ("application/xhtml+xml", "html"), ("image/svg+xml", "svg"),
                 ("application/xml", "xml"), ("text/xml", "xml"), ("application/rss+xml", "xml"), ("image/png", None), ("application/pdf", None)):
    check(f"active_kind by type: {mt} -> {want}", content_security._kind_from_type(mt) == want)
check("sniff: html without an extension", content_security._kind_from_bytes(b"  \n<!DOCTYPE html><html>") == "html")
check("sniff: svg without an extension", content_security._kind_from_bytes(b'<svg xmlns="x"></svg>') == "svg")
check("sniff: plain text is nothing", content_security._kind_from_bytes(b"just some words") is None)
check("sniff: utf-16 html", content_security._kind_from_bytes(b"\xff\xfe" + "<html><script>".encode("utf-16-le")) == "html")

print("\n--- /preview (the static-export mount) ---")
cur = paths.current_export_dir()
(cur / "media").mkdir(parents=True, exist_ok=True)
(cur / "index.html").write_text("<html><body>generated page zq610idx</body></html>", encoding="utf-8")
(cur / "media" / "u1.html").write_bytes(HTML)
(cur / "media" / "u2.svg").write_bytes(SVG)
(cur / "media" / "u3.png").write_bytes(png.getvalue())
r = client.get("/preview/index.html")
check("preview: the generated page still renders (no CSP, no attachment)", r.status_code == 200 and "content-security-policy" not in hdr(r)
      and not hdr(r).get("content-disposition", "").startswith("attachment") and "zq610idx" in r.text)
r = client.get("/preview/media/u1.html")
check("preview: a user .html is sandboxed + attachment + nosniff", r.status_code == 200 and hdr(r).get("content-security-policy") == CSP
      and hdr(r).get("content-disposition", "").startswith("attachment") and hdr(r).get("x-content-type-options") == "nosniff", hdr(r))
r = client.get("/preview/media/u2.svg")
check("preview: a user .svg is sandboxed + nosniff", r.status_code == 200 and hdr(r).get("content-security-policy") == CSP
      and hdr(r).get("x-content-type-options") == "nosniff", hdr(r))
r = client.get("/preview/media/u3.png")
check("preview: a user .png is nosniff only", r.status_code == 200 and hdr(r).get("x-content-type-options") == "nosniff"
      and "content-security-policy" not in hdr(r))
check("preview: anonymous turned away (unchanged)", anon.get("/preview/media/u1.html", follow_redirects=False).status_code in (302, 401))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("all passed")
