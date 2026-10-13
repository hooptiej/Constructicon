#!/usr/bin/env python3
"""Self-contained check for #645: browser security headers on every app response.

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient. Run
anywhere the requirements are installed:

    python scripts/test_security_headers_645.py

Covers: app pages, API answers, errors and refusals (404, anonymous 401, the sign-in redirect, the
origin guard's 403) all carry the CSP, X-Frame-Options, Referrer-Policy, nosniff, Permissions-Policy
and COOP; user files (/f) keep #610's own headers (sandbox CSP, no app CSP, no X-Frame-Options); the
HTML Rendered view keeps its sandbox CSP and no-referrer; /preview gets the light preview policy and
its media keep the sandbox; HSTS never over HTTP, over HTTPS only when CONSTRUCTICON_HSTS_SECONDS is
set; the CSP env knobs; a route opt-out; and no inline event handler is left in the templates/JS (the
CSP's script-src-attr 'none' would silently break it). Exits 1 if any check fails.
"""
import io
import os
import re
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("secheaders645-")
os.environ["CAPTION_DISABLED"] = "1"
for _k in ("CONSTRUCTICON_HSTS_SECONDS", "CONSTRUCTICON_CSP", "CONSTRUCTICON_CSP_REPORT_ONLY"):
    os.environ.pop(_k, None)
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
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from web import app as webapp, content_security, security_headers as sh  # noqa: E402

client = _testenv.client(webapp.app, raise_server_exceptions=False)  # the install token (admin)
anon = TestClient(webapp.app, raise_server_exceptions=False)
https_client = _testenv.client(webapp.app, raise_server_exceptions=False, base_url="https://testserver")
with client:
    pass


def hdr(r):
    return {k.lower(): v for k, v in r.headers.items()}


def upload(name, data, ctype="application/octet-stream"):
    r = client.post("/api/upload", files={"file": (name, data, ctype)})
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    return r.json().get("slug") or r.json().get("object", {}).get("slug")


png = io.BytesIO()
Image.new("RGB", (40, 30), (30, 160, 30)).save(png, "PNG")
SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"><script>alert(1)</script>'
       b'<rect width="40" height="30" fill="green"/></svg>')
HTML = b"<html><body><script>alert(1)</script>zq645html</body></html>"
S = {
    "png": upload("pic645.png", png.getvalue(), "image/png"),
    "svg": upload("pic645.svg", SVG, "image/svg+xml"),
    "html": upload("page645.html", HTML, "text/html"),
}
SANDBOX = content_security.RENDERED_HTML_CSP


def app_headers_ok(name, r, csp=sh.APP_CSP):
    h = hdr(r)
    check(f"{name}: CSP is the app policy", h.get("content-security-policy") == csp, h.get("content-security-policy"))
    check(f"{name}: X-Frame-Options SAMEORIGIN", h.get("x-frame-options") == "SAMEORIGIN", h.get("x-frame-options"))
    check(f"{name}: Referrer-Policy", h.get("referrer-policy") == "strict-origin-when-cross-origin", h.get("referrer-policy"))
    check(f"{name}: nosniff", h.get("x-content-type-options") == "nosniff")
    check(f"{name}: Permissions-Policy", h.get("permissions-policy") == sh.PERMISSIONS_POLICY)
    check(f"{name}: COOP same-origin", h.get("cross-origin-opener-policy") == "same-origin")
    check(f"{name}: no HSTS over HTTP", "strict-transport-security" not in h)
    check(f"{name}: opt-out marker never leaks", sh.OPT_OUT_HEADER not in h)


print("--- app pages, API answers, errors and refusals ---")
for path in ["/", "/admin", "/admin?embed=1", f"/object/{S['png']}", f"/object/{S['html']}", "/curator",
             "/api/gallery", f"/api/image/{S['png']}", "/healthz", "/api/version", "/no-such-page-645"]:
    r = client.get(path, follow_redirects=False)
    check(f"GET {path}: answered", r.status_code in (200, 404), r.status_code)
    app_headers_ok(f"GET {path}", r)
app_headers_ok("anonymous /login", anon.get("/login"))
r = anon.get("/", follow_redirects=False)
check("anonymous /: the sign-in redirect", r.status_code == 302, r.status_code)
app_headers_ok("anonymous / (302)", r)
r = anon.get("/api/gallery")
check("anonymous /api/gallery: 401", r.status_code == 401, r.status_code)
app_headers_ok("anonymous /api/gallery (401)", r)
r = client.post("/api/settings", data={"key": "x", "value": "y"}, headers={"Origin": "http://evil.example"})
check("cross-origin POST: refused by the origin guard (403)", r.status_code == 403, r.status_code)
app_headers_ok("origin guard 403", r)
r = client.get("/static/style.css")
app_headers_ok("static asset", r)

print("\n--- the CSP allows what the UI loads, and nothing broad ---")
csp = dict((d.split(" ", 1) + [""])[:2] for d in (p.strip() for p in sh.APP_CSP.split(";")))
check("frame-ancestors 'self'", csp.get("frame-ancestors") == "'self'")
check("object-src 'none'", csp.get("object-src") == "'none'")
check("base-uri 'self'", csp.get("base-uri") == "'self'")
check("form-action 'self'", csp.get("form-action") == "'self'")
check("inline handlers off (script-src-attr 'none')", csp.get("script-src-attr") == "'none'")
check("no 'unsafe-eval' anywhere", "unsafe-eval" not in sh.APP_CSP)
check("no wildcard sources", not re.search(r"(^|\s)(\*|https:|http:)(\s|;|$)", sh.APP_CSP))
check("Google Fonts allowed (base.html)", "https://fonts.googleapis.com" in csp.get("style-src", "")
      and "https://fonts.gstatic.com" in csp.get("font-src", ""))
check("YouTube embeds allowed (object page)", "https://www.youtube.com" in csp.get("frame-src", ""))
page = client.get("/").text
check("base.html still loads Google Fonts from the allowed origin", "https://fonts.googleapis.com/" in page)
ext = set(re.findall(r'<(?:script|link|img|iframe|video|audio|source)\b[^>]*\s(?:src|href)="(https?://[^/"]+)', page))
check("home page: no external origin outside the policy", ext <= {"https://fonts.googleapis.com"}, ext)

print("\n--- user files (/f) keep #610's headers ---")
r = client.get(f"/f/{S['png']}")
h = hdr(r)
check("/f png: 200", r.status_code == 200, r.status_code)
check("/f png: nosniff (from #610)", h.get("x-content-type-options") == "nosniff")
check("/f png: no app CSP (a hotlinked file)", "content-security-policy" not in h, h.get("content-security-policy"))
check("/f png: no X-Frame-Options (may be embedded elsewhere)", "x-frame-options" not in h)
check("/f png: Referrer-Policy added", h.get("referrer-policy") == "strict-origin-when-cross-origin")
r = client.get(f"/f/{S['svg']}")
h = hdr(r)
check("/f svg: keeps the sandbox CSP, exactly", h.get("content-security-policy") == SANDBOX, h.get("content-security-policy"))
check("/f svg: one CSP header, no report-only twin", r.headers.get_list("content-security-policy") == [SANDBOX]
      and "content-security-policy-report-only" not in h)
r = anon.get(f"/f/{S['png']}/thumb")
h = hdr(r)
check("/f thumb (anonymous): served, nosniff, no app CSP", r.status_code == 200 and h.get("x-content-type-options") == "nosniff"
      and "content-security-policy" not in h, r.status_code)
r = client.get(f"/f/{S['html']}")
h = hdr(r)
check("/f html: still a sandboxed download", h.get("content-security-policy") == SANDBOX
      and h.get("content-disposition", "").startswith("attachment"))

print("\n--- the HTML Rendered view keeps its own policy ---")
r = client.get(f"/api/image/{S['html']}/rendered")
h = hdr(r)
check("rendered: 200", r.status_code == 200, r.status_code)
check("rendered: its sandbox CSP, unclobbered", r.headers.get_list("content-security-policy") == [SANDBOX], h.get("content-security-policy"))
check("rendered: frame-ancestors 'self' (the object page frames it)", "frame-ancestors 'self'" in h.get("content-security-policy", ""))
check("rendered: its no-referrer kept", h.get("referrer-policy") == "no-referrer", h.get("referrer-policy"))
check("rendered: X-Frame-Options SAMEORIGIN (same-origin framing still allowed)", h.get("x-frame-options") == "SAMEORIGIN")
obj = client.get(f"/object/{S['html']}").text
check("object page: still frames the Rendered view", f"/api/image/{S['html']}/rendered" in obj)

print("\n--- /preview (the static-export mount) ---")
cur = paths.current_export_dir()
(cur / "media").mkdir(parents=True, exist_ok=True)
(cur / "index.html").write_text("<html><body>generated page zq645idx<script>1</script></body></html>", encoding="utf-8")
(cur / "media" / "m.svg").write_bytes(SVG)
r = client.get("/preview/index.html")
h = hdr(r)
check("preview page: renders", r.status_code == 200 and "zq645idx" in r.text, r.status_code)
check("preview page: the light preview CSP", h.get("content-security-policy") == sh.PREVIEW_CSP, h.get("content-security-policy"))
check("preview page: X-Frame-Options SAMEORIGIN", h.get("x-frame-options") == "SAMEORIGIN")
r = client.get("/preview/media/m.svg")
check("preview media svg: keeps the sandbox CSP", r.headers.get_list("content-security-policy") == [SANDBOX], hdr(r).get("content-security-policy"))

print("\n--- HSTS: HTTPS only, opt-in ---")
check("https, no env: no HSTS", "strict-transport-security" not in hdr(https_client.get("/healthz")))
os.environ["CONSTRUCTICON_HSTS_SECONDS"] = "31536000"
check("https + env: HSTS", hdr(https_client.get("/healthz")).get("strict-transport-security") == "max-age=31536000")
check("https + env: HSTS on a page too", hdr(https_client.get("/")).get("strict-transport-security") == "max-age=31536000")
check("https + env: HSTS on a user file too", hdr(https_client.get(f"/f/{S['png']}")).get("strict-transport-security") == "max-age=31536000")
check("http + env: still no HSTS", "strict-transport-security" not in hdr(client.get("/healthz")))
check("http + env + a forged X-Forwarded-Proto: no HSTS (not trusted, #646)",
      "strict-transport-security" not in hdr(client.get("/healthz", headers={"X-Forwarded-Proto": "https"})))
for bad in ("abc", "-5", "0", ""):
    os.environ["CONSTRUCTICON_HSTS_SECONDS"] = bad
    check(f"https + env={bad!r}: no HSTS", "strict-transport-security" not in hdr(https_client.get("/healthz")))
os.environ.pop("CONSTRUCTICON_HSTS_SECONDS", None)

print("\n--- the CSP env knobs ---")
os.environ["CONSTRUCTICON_CSP_REPORT_ONLY"] = "1"
h = hdr(client.get("/"))
check("report-only: sent as Content-Security-Policy-Report-Only", h.get("content-security-policy-report-only") == sh.APP_CSP
      and "content-security-policy" not in h)
check("report-only: X-Frame-Options still enforced", h.get("x-frame-options") == "SAMEORIGIN")
check("report-only: user file sandbox untouched", client.get(f"/f/{S['svg']}").headers.get_list("content-security-policy") == [SANDBOX])
os.environ.pop("CONSTRUCTICON_CSP_REPORT_ONLY")
os.environ["CONSTRUCTICON_CSP"] = "default-src 'self'; frame-ancestors 'none'"
check("override: the install's own policy", hdr(client.get("/")).get("content-security-policy") == "default-src 'self'; frame-ancestors 'none'")
os.environ["CONSTRUCTICON_CSP"] = "off"
h = hdr(client.get("/"))
check("off: no CSP on app pages", "content-security-policy" not in h and "content-security-policy-report-only" not in h)
check("off: X-Frame-Options still sent", h.get("x-frame-options") == "SAMEORIGIN")
os.environ.pop("CONSTRUCTICON_CSP")
check("back to default", hdr(client.get("/")).get("content-security-policy") == sh.APP_CSP)

print("\n--- a route opt-out ---")


def _opted(request):
    return sh.opt_out(PlainTextResponse("opted"), "X-Frame-Options", "content-security-policy")


def _own(request):
    return PlainTextResponse("own", headers={"Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})


def _plain(request):
    return PlainTextResponse("plain")


mini = Starlette(routes=[Route("/opted", _opted), Route("/own", _own), Route("/plain", _plain)])
mini.add_middleware(sh.SecurityHeadersMiddleware)
mc = TestClient(mini)
h = hdr(mc.get("/opted"))
check("opt-out: the named headers are left off", "x-frame-options" not in h and "content-security-policy" not in h, h)
check("opt-out: the other defaults still sent", h.get("referrer-policy") == "strict-origin-when-cross-origin"
      and h.get("x-content-type-options") == "nosniff")
check("opt-out: the marker header is stripped", sh.OPT_OUT_HEADER not in h)
h = hdr(mc.get("/own"))
check("a route's own value wins", h.get("referrer-policy") == "no-referrer" and h.get("x-frame-options") == "DENY", h)
check("... and is not duplicated", mc.get("/own").headers.get_list("x-frame-options") == ["DENY"])
check("plain route: full defaults", hdr(mc.get("/plain")).get("content-security-policy") == sh.APP_CSP)

print("\n--- no inline event handlers left (script-src-attr 'none' would break them) ---")
HANDLER = re.compile(r"""<[a-zA-Z][^<>]*?\son[a-z]+\s*=\s*["'{\\]""")
SETATTR = re.compile(r"""setAttribute\(\s*['"]on[a-z]+['"]""")
JSURL = re.compile(r"""href\s*=\s*["']\s*javascript:""", re.I)
hits = []
scan = list((Path(ROOT) / "web" / "templates").rglob("*.html")) + list((Path(ROOT) / "web" / "static" / "js").glob("*.js")) \
    + list((Path(ROOT) / "core" / "object_types").glob("*.py")) + [Path(ROOT) / "web" / "shapes.py", Path(ROOT) / "web" / "common.py"]
for p in scan:
    text = p.read_text(encoding="utf-8")
    for rx in (HANDLER, SETATTR, JSURL):
        for m in rx.finditer(text):
            hits.append(f"{p.relative_to(ROOT).as_posix()}:{text.count(chr(10), 0, m.start()) + 1}")
check(f"no inline handler, setAttribute('on...') or javascript: link in {len(scan)} files", not hits, ", ".join(hits[:10]))
check("the handler scan itself works", bool(HANDLER.search('<a href="#" onclick="x()">')) and bool(HANDLER.search("'<input onchange=\"f(this)\"'")))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
