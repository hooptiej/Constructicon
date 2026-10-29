#!/usr/bin/env python3
"""#779 (the tab part): a permission never makes a nav tab invisible.

Throwaway SQLite DB and storage (scripts/_testenv.py), the real FastAPI app through TestClient, real
sessions for an admin, an editor and a viewer (users made like scripts/test_ownership_sensitive.py),
plus the install token. No server, no network:

    python scripts/test_nav_tabs_roles_779.py

  1. Every role sees all nine tabs, each exactly once, on its usual side (#774 still holds).
  2. Which tabs are locked (greyed) mirrors the server's own route roles: Export and Admin are
     admin-only, Upload and Blog editor-and-up (their writes), the rest viewer. So: admin and the
     token none; editor Export + Admin; viewer Export + Admin + Upload + Blog. And the mapping
     itself is checked against the route table (web/roles.label_of on the real routes).
  3. A locked tab is a .nav-tab-locked button, aria-disabled, with no drawer trigger, id or badge;
     its title / aria-label carry the role ("Admin (admins only)", "Upload (editors and admins)");
     its data-locked-message names the tab, the role it needs, the user's own role and "Ask an
     admin" (what the click shows: the rail script reads that attribute, never an empty note).
  4. The restricted drawer's markup is absent for that user: no admin iframe / drawer, no export
     builder, no blog builder, no upload drawer or home upload pop-out, and none of their header
     triggers; an allowed user still gets all of them.
  5. Admins: no locked tab anywhere, every drawer present (as before #779).
  6. The rail script handles locked tabs (click, Escape, the note element) and style.css greys them
     with a filter, never opacity.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import os
import re
import secrets
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("nav-tabs-roles-779-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from fastapi.testclient import TestClient  # noqa: E402

from core import actor, db, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from web import roles as web_roles  # noqa: E402

FAILS = []
BASE = "http://testserver"
SAME = {"Origin": BASE}

SIDES = {
    "left": ["curator", "export", "brand", "admin"],
    "right": ["upload", "processing", "blog", "hobbies", "wallpaper"],
}
MIN_ROLE = {"export": "admin", "admin": "admin", "upload": "editor", "blog": "editor"}
LABEL = {"export": "Export", "admin": "Admin", "upload": "Upload", "blog": "Blog"}
NEED = {"admin": "admins only", "editor": "editors and admins"}
LOCKED = {
    "admin": set(),
    "token": set(),
    "editor": {"export", "admin"},
    "viewer": {"export", "admin", "upload", "blog"},
}
# Markup each restricted drawer sends (its partial's own ids) and the header trigger for it.
DRAWER_MARKUP = {
    "admin": ['id="admin-drawer"', 'id="admin-drawer-frame"', 'id="admin-drawer-trigger"'],
    "export": ['id="export-builder"', 'id="export-builder-trigger"'],
    "blog": ['id="blog-builder"', 'id="blog-builder-trigger"'],
    "upload": ['id="upload-drawer"', 'id="gallery-drawer"', 'id="upload-trigger"', 'id="upload-form"',
               "/static/js/ingest.js"],
}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def tab_li(html, tab):
    m = re.search(rf'<li class="nav-tab-item" data-nav-tab="{tab}">(.*?)</li>', html, re.S)
    return m.group(1) if m else ""


def rails(html):
    out = {}
    for side, body in re.findall(r'<ul class="[^"]*\bnav-rail-(left|right)\b[^"]*"[^>]*>(.*?)</ul>', html, re.S):
        out[side] = re.findall(r'data-nav-tab="([^"]+)"', body)
    return out


db.init_db()
users.limiter.reset()

# ---- 0. the mapping mirrors the server's route roles --------------------------------------------
print("--- route roles behind each gated tab ---")
ROUTE_ROLE = {}
for r in web_roles.flat_routes(webapp.app.routes):
    for m in (getattr(r, "methods", None) or []):
        ROUTE_ROLE[(m, getattr(r, "path", ""))] = web_roles.label_of(r)
check("server: GET /admin is admin", ROUTE_ROLE.get(("GET", "/admin")) == "admin", ROUTE_ROLE.get(("GET", "/admin")))
for path in ("/api/export/build", "/api/export/publish"):
    check(f"server: POST {path} is admin", ROUTE_ROLE.get(("POST", path)) == "admin", ROUTE_ROLE.get(("POST", path)))
for path in ("/api/export/config", "/api/export/targets"):
    check(f"server: GET {path} is admin", ROUTE_ROLE.get(("GET", path)) == "admin", ROUTE_ROLE.get(("GET", path)))
for path in ("/api/upload", "/api/content", "/api/blog-entries"):
    check(f"server: POST {path} is editor", ROUTE_ROLE.get(("POST", path)) == "editor", ROUTE_ROLE.get(("POST", path)))
for m, path in (("GET", "/api/curator/queue"), ("GET", "/api/brand-assets"), ("GET", "/api/hobbies/html"),
                ("GET", "/wallpaper"), ("GET", "/api/processing")):
    check(f"server: {m} {path} is viewer (its tab has no minimum)", ROUTE_ROLE.get((m, path)) == "viewer",
          ROUTE_ROLE.get((m, path)))

# ---- users and their clients --------------------------------------------------------------------
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("nt_admin", "admin"), ("nt_editor", "editor"), ("nt_viewer", "viewer")):
        PW[name] = "nt-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
CLIENTS = {"token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for role, name in (("admin", "nt_admin"), ("editor", "nt_editor"), ("viewer", "nt_viewer")):
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    check(f"{role} signs in", r.status_code == 200, r.text[:200])
    CLIENTS[role] = c

PAGES = ["/", "/unfiled", "/hobbies", "/curator", "/wallpaper"]
for who, c in CLIENTS.items():
    print(f"--- as {who} ---")
    role_word = who if who != "token" else "admin"
    for path in PAGES:
        r = c.get(path)
        check(f"{who} {path}: 200", r.status_code == 200, r.status_code)
        if r.status_code != 200:
            continue
        html = r.text
        # 1. all nine tabs, once each, on their side
        check(f"{who} {path}: rails unchanged by role", rails(html) == SIDES, rails(html))
        # 2. exactly the expected tabs are locked
        locked_now = {t for t in MIN_ROLE if "nav-tab-locked" in tab_li(html, t)}
        check(f"{who} {path}: locked tabs = {sorted(LOCKED[who])}", locked_now == LOCKED[who], sorted(locked_now))
        check(f"{who} {path}: only gated tabs can be locked",
              html.count('class="nav-tab nav-tab-locked"') == len(LOCKED[who]), html.count("nav-tab-locked"))
        for t in MIN_ROLE:
            li = tab_li(html, t)
            if t in LOCKED[who]:
                # 3. locked: labelled, explained, inert
                need = NEED[MIN_ROLE[t]]
                check(f"{who} {path}: {t} title+aria-label carry the role",
                      f'title="{LABEL[t]} ({need})"' in li and f'aria-label="{LABEL[t]} ({need})"' in li, li[:300])
                check(f"{who} {path}: {t} is aria-disabled with no trigger, id or badge",
                      'aria-disabled="true"' in li and "data-drawer-trigger" not in li
                      and " id=" not in li and "nav-tab-badge" not in li, li[:300])
                m = re.search(r'data-locked-message="([^"]*)"', li)
                msg = m.group(1) if m else ""
                check(f"{who} {path}: {t} message names it, the role, the user's role and who to ask",
                      msg.startswith(f"{LABEL[t]} needs the {MIN_ROLE[t]} role") and f"{'an' if role_word[0] in 'aeiou' else 'a'} {role_word}." in msg
                      and "Ask an admin" in msg, msg)
                # 4. its drawer's markup is not on the page
                for marker in DRAWER_MARKUP[t]:
                    check(f"{who} {path}: {t} markup absent ({marker})", marker not in html)
            else:
                check(f"{who} {path}: {t} is a working tab", "nav-tab-locked" not in li and li != "", li[:200])
        # allowed drawers are still sent (home has the upload pop-out instead of the upload drawer)
        for t in set(MIN_ROLE) - LOCKED[who]:
            markers = DRAWER_MARKUP[t]
            if t == "upload":
                markers = ['id="gallery-drawer"'] if path == "/" else ['id="upload-drawer"', 'id="upload-trigger"']
            if t in ("blog", "export") and path in ("/curator", "/unfiled", "/wallpaper"):
                # those pages never included the blog/export builders (unchanged by #779)
                markers = [mk for mk in markers if "trigger" in mk]
            for marker in markers:
                check(f"{who} {path}: allowed {t} markup present ({marker})", marker in html)

# ---- 5. admins: nothing greyed anywhere ----------------------------------------------------------
for who in ("admin", "token"):
    r = CLIENTS[who].get("/")
    check(f"{who}: no locked tab on /", 'class="nav-tab nav-tab-locked"' not in r.text
          and "data-locked-message" not in r.text)

# ---- 6. script and CSS --------------------------------------------------------------------------
tpl = open(os.path.join(ROOT, "web", "templates", "_nav_tabs.html"), encoding="utf-8").read()
check("script: locked tabs open a note from data-locked-message",
      "querySelectorAll('.nav-tab-locked')" in tpl and "dataset.lockedMessage" in tpl
      and "note.textContent" in tpl and "'Escape'" in tpl)
css = open(os.path.join(ROOT, "web", "static", "style.css"), encoding="utf-8").read()
m = re.search(r"\.nav-tab-locked \{([^}]*)\}", css)
rule = m.group(1) if m else ""
check("css: locked tab is greyscale, not translucent", "grayscale(1)" in rule and "opacity" not in rule, rule)

print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
