#!/usr/bin/env python3
"""Self-contained check for #547 step 4: the always-on drawers live in base.html.

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient. Run
anywhere the requirements are installed:

    python scripts/test_drawers_base_547.py

Covers:
- no page template includes the header or the Upload / Blog / Export drawers itself (base.html does);
- every HTML page route is rendered here (the route table is walked, so a new page that isn't listed
  fails), and for the admin token each page carries the header, each drawer and its trigger exactly
  once (home: the hover pop-out instead of the Upload drawer, never both) and the full nav tab set;
- /curator and /captions/review now carry the Upload, Blog and Export drawers behind their tabs (the
  bug this step fixes: their tabs clicked a trigger with no drawer behind it);
- an editor and a viewer see every tab; the ones above their role are greyed (#779), never missing,
  and the drawer markup above their role is not sent;
- the opt-outs: ?embed=1 (the Admin drawer's iframe) has no header, drawers or rail; anonymous
  /login, /setup and /logout get the header with "Sign in" and none of the role-gated drawers;
- no element id appears twice on any page, and every inline <script> carries the header's nonce.
Exits 1 if any check fails.
"""
import io
import os
import re
import sys
import types
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("drawers547-")
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine: the SVG thumbnail can't be drawn, everything else runs
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from pathlib import Path  # noqa: E402
from core import actor, cards, db, hobbies, users  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
users.limiter.reset()
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from web import app as webapp  # noqa: E402

BASE = "http://testserver"
SAME = {"Origin": BASE}
TEMPLATES = Path(ROOT) / "web" / "templates"

# --- the templates themselves --------------------------------------------------------------------
print("--- no page includes the always-on pieces itself ---")
SHELL = ["_header.html", "_upload_drawer.html", "_blog_builder.html", "_export_builder.html",
         "_gallery_drawer.html", "_nav_tabs.html", "_processing_drawer.html", "_brand_drawer.html",
         "_hobbies_drawer.html", "_nudges_drawer.html", "_admin_drawer.html"]
INC = re.compile(r"""\{%-?\s*include\s+["']([^"']+)["']""")
for p in sorted(TEMPLATES.glob("*.html")):
    if p.name == "base.html":
        continue
    text = p.read_text(encoding="utf-8")
    hits = [name for name in INC.findall(text) if name in SHELL]
    if p.name == "home.html":
        # its one sanctioned override: the hover pop-out in base.html's upload_drawer block
        ok = hits == ["_gallery_drawer.html"] and re.search(
            r"\{%\s*block upload_drawer\s*%\}\{%\s*include \"_gallery_drawer.html\"\s*%\}\{%\s*endblock\s*%\}", text)
        check("home.html: only the _gallery_drawer override, inside {% block upload_drawer %}", bool(ok), hits)
    elif not p.name.startswith("_"):
        check(f"{p.name}: includes none of the shell partials", not hits, hits)
base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
for part in SHELL:
    if part == "_gallery_drawer.html":
        continue
    check(f"base.html includes {part} exactly once", len(re.findall(r'include "' + re.escape(part) + '"', base)) == 1)
check("base.html: the header and the three drawers render BEFORE the page body",
      base.index('include "_export_builder.html"') < base.index("{% block body %}")
      and base.index('include "_header.html"') < base.index('include "_upload_drawer.html"')
      < base.index('include "_blog_builder.html"') < base.index('include "_export_builder.html"'))

# --- fixtures --------------------------------------------------------------------------------------
anon = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
r = anon.get("/setup")
SETUP_PAGE = r
check("anonymous /setup renders while there are no users", r.status_code == 200, r.status_code)

PW = {}
NAMES = {"admin": "dr_admin", "editor": "dr_editor", "viewer": "dr_viewer"}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for role, name in NAMES.items():
        PW[role] = "dr-pass-" + name + "-123456"
        users.create_user(name, PW[role], role)

token = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)


def sign_in(role):
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": NAMES[role], "password": PW[role]}, headers=SAME)
    check(f"{role} signs in", r.status_code == 200, r.text[:200])
    return c


SESS = {role: sign_in(role) for role in NAMES}


def png_bytes(seed):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (seed * 40 % 256, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


r = token.post("/api/upload", files={"file": ("drawers.png", png_bytes(1), "image/png")},
               data={"description": "drawers 547"}, headers=SAME)
check("fixture: an item", r.status_code == 200, r.text[:200])
SLUG = r.json()["slug"]
with actor.acting_as(actor.ACTOR_SCRIPT):
    CARD = cards.create("Drawers Card").data["card"]
    HOBBY = hobbies.create("Drawers Hobby").data["hobby"]

# Every HTML page, by route pattern -> a concrete URL. A page route missing here fails below.
PAGES = {
    "/": "/",
    "/project/{slug}": f"/project/{CARD['slug']}",
    "/unfiled": "/unfiled",
    "/hobbies": "/hobbies",
    "/hobby/{slug}": f"/hobby/{HOBBY['slug']}",
    "/brand": "/brand",
    "/wallpaper": "/wallpaper",
    "/gallery/user/{uploader}": f"/gallery/user/{NAMES['admin']}",
    "/gallery/source/{label}": "/gallery/source/no-such-label-547",  # the "says so" 404 page
    "/object/{slug}": f"/object/{SLUG}",
    "/admin": "/admin",
    "/guides/{guide}": "/guides/capture-physical-piece",
    "/curator": "/curator",
    "/captions/review": "/captions/review",
    "/login": "/login",
    "/logout": "/logout",
    "/account/password": "/account/password",
}
# Not rendered for a signed-in user here: /setup is 404 once a user exists (rendered anonymously
# above); the redirects render nothing.
NOT_PAGES = {"/setup", "/upload", "/gallery", "/image/{slug}"}

print("\n--- every HTML page route is covered ---")
from fastapi.routing import APIRoute  # noqa: E402
from starlette.responses import HTMLResponse  # noqa: E402
from web import roles as web_roles  # noqa: E402


page_routes = set()
for rt in web_roles.flat_routes(webapp.app.routes):
    if not isinstance(rt, APIRoute) or "GET" not in rt.methods:
        continue
    if rt.path.startswith(("/api/", "/f/")) or rt.path in ("/healthz",):
        continue
    if rt.response_class is HTMLResponse or issubclass(getattr(rt.response_class, "__mro__", [object])[0], HTMLResponse) \
            or rt.path in NOT_PAGES:
        page_routes.add(rt.path)
missing = sorted(page_routes - set(PAGES) - NOT_PAGES)
check(f"every GET page route is in this test's list ({len(page_routes)} routes)", len(page_routes) >= 15 and not missing,
      missing or len(page_routes))

# --- what a page must carry -----------------------------------------------------------------------
TAB_IDS = ["curator", "export", "brand", "admin", "upload", "processing", "blog", "hobbies", "wallpaper"]
TAB_ROLE = {"export": "admin", "admin": "admin", "upload": "editor", "blog": "editor"}
DRAWER_ROOT = {"upload": "upload-drawer", "blog": "blog-builder", "export": "export-builder"}
TRIGGER = {"upload": "upload-trigger", "blog": "blog-builder-trigger", "export": "export-builder-trigger"}
RANK = {"viewer": 1, "editor": 2, "admin": 3}

SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
COMMENT = re.compile(r"<!--.*?-->", re.S)
ID_ATTR = re.compile(r"""\sid\s*=\s*["']([^"']+)["']""")
INLINE = re.compile(r"<script\b([^>]*)>", re.I)
NONCE_ATTR = re.compile(r"""\snonce\s*=\s*["']([^"']*)["']""", re.I)
NONCE_SRC = re.compile(r"'nonce-([A-Za-z0-9_-]+)'")


def ids_of(html):
    """Element ids in the markup (not in scripts, whose strings build ids at run time, or comments)."""
    return Counter(ID_ATTR.findall(COMMENT.sub("", SCRIPT_BLOCK.sub("", html))))


def tabs_of(html):
    return Counter(re.findall(r'data-nav-tab="([^"]+)"', html))


def locked_tabs(html):
    return set(re.findall(r'data-nav-tab="([^"]+)">\s*<button[^>]*nav-tab-locked', html))


def common_checks(name, r):
    html = r.text
    ids = ids_of(html)
    dup = sorted(k for k, n in ids.items() if n > 1)
    check(f"{name}: no element id appears twice", not dup, dup[:10])
    m = NONCE_SRC.search(r.headers.get("content-security-policy", ""))
    inline = [a for a in INLINE.findall(html) if "src=" not in a]
    bad = [a for a in inline if not m or (NONCE_ATTR.search(a) or [None, None])[1] != m.group(1)]
    check(f"{name}: {len(inline)} inline script(s), each with the header's nonce", not bad, len(bad))
    return ids


def full_page_checks(name, r, role):
    html = r.text
    ids = common_checks(name, r)
    check(f"{name}: the header once", ids["user-chip"] == 1 and html.count('class="app-header"') == 1, ids["user-chip"])
    tabs = tabs_of(html)
    check(f"{name}: the full nav tab set, each once", all(tabs[t] == 1 for t in TAB_IDS) and len(tabs) == len(TAB_IDS),
          dict(tabs))
    want_locked = {t for t, need in TAB_ROLE.items() if RANK[role] < RANK[need]}
    check(f"{name}: tabs above {role} are greyed, the rest live", locked_tabs(html) == want_locked,
          sorted(locked_tabs(html)))
    home = name.split()[-1] == "/"
    for d, root in DRAWER_ROOT.items():
        allowed = RANK[role] >= RANK[TAB_ROLE[d]]
        if d == "upload" and home:
            check(f"{name}: the hover pop-out instead of the Upload drawer",
                  (ids["gallery-drawer"] == (1 if allowed else 0)) and ids["upload-drawer"] == 0,
                  (ids["gallery-drawer"], ids["upload-drawer"]))
        else:
            check(f"{name}: {d} drawer {'once' if allowed else 'not sent'}", ids[root] == (1 if allowed else 0), ids[root])
        check(f"{name}: {d} trigger {'once' if allowed else 'not sent'}",
              ids[TRIGGER[d]] == (1 if allowed else 0), ids[TRIGGER[d]])
    check(f"{name}: the processing tab once", ids["proc-tab"] == 1, ids["proc-tab"])


print("\n--- admin (install token): every page ---")
for pattern, url in PAGES.items():
    r = token.get(url)
    want = 404 if pattern == "/gallery/source/{label}" else 200
    check(f"token GET {url}: {want}", r.status_code == want, r.status_code)
    if r.status_code in (200, 404) and "text/html" in r.headers.get("content-type", ""):
        full_page_checks(f"token GET {url}", r, "admin")

print("\n--- the bug this step fixes: /curator and /captions/review ---")
for url in ("/curator", "/captions/review"):
    html = token.get(url).text
    ids = ids_of(html)
    for d in ("upload", "blog", "export"):
        check(f"{url}: the {d} tab has its trigger AND its drawer",
              tabs_of(html)[d] == 1 and ids[TRIGGER[d]] == 1 and ids[DRAWER_ROOT[d]] == 1,
              (tabs_of(html)[d], ids[TRIGGER[d]], ids[DRAWER_ROOT[d]]))

print("\n--- signed-in sessions: editor and viewer see every tab, greyed above their role ---")
for role in ("admin", "editor", "viewer"):
    for url in ("/", "/curator", "/captions/review", f"/object/{SLUG}", "/account/password", "/logout"):
        r = SESS[role].get(url)
        check(f"{role} GET {url}: 200", r.status_code == 200, r.status_code)
        if r.status_code == 200:
            full_page_checks(f"{role} GET {url}", r, role)
r = SESS["editor"].get("/admin")
check("editor /admin: refused (admin page), not rendered", r.status_code == 403, r.status_code)

print("\n--- opt-outs ---")
r = token.get("/admin?embed=1")
check("/admin?embed=1: 200", r.status_code == 200, r.status_code)
ids = common_checks("/admin?embed=1", r)
check("/admin?embed=1: no header, drawers, triggers or nav rail",
      not any(ids[k] for k in ["user-chip", *DRAWER_ROOT.values(), *TRIGGER.values(), "proc-tab", "gallery-drawer"])
      and not tabs_of(r.text) and 'class="app-header"' not in r.text)
users.limiter.reset()
for name, r in (("anonymous /setup", SETUP_PAGE), ("anonymous /login", anon.get("/login")),
                ("anonymous /logout", anon.get("/logout"))):
    check(f"{name}: 200", r.status_code == 200, r.status_code)
    ids = common_checks(name, r)
    check(f"{name}: the header with Sign in", ids["user-chip"] == 1 and "Sign in" in r.text)
    check(f"{name}: none of the role-gated drawers or triggers",
          not any(ids[k] for k in [*DRAWER_ROOT.values(), *TRIGGER.values(), "gallery-drawer"]),
          {k: ids[k] for k in [*DRAWER_ROOT.values(), *TRIGGER.values()] if ids[k]})

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
