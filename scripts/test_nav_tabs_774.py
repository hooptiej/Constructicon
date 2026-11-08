#!/usr/bin/env python3
"""#774 "Tab-le for two": the nav tabs split across both screen edges.

Throwaway SQLite DB and storage (scripts/_testenv.py), the real FastAPI app through TestClient
(install token = admin). No server, no network:

    python scripts/test_nav_tabs_774.py

  1. NAV_TABS (the one tab list at the top of web/templates/_nav_tabs.html) puts each tab on the
     owner's side, in the owner's order. #851 (owner decision 2026-11-08) cut the rail to four:
     left = Curator, Admin; right = Upload, Processing (Hobbies, Blog, Wallpaper, Brand and Export
     moved into /admin's Tools group). Checked on the rendered HTML of several pages, so it is
     the config AS RENDERED, not a copy of it.
  2. Every tab renders exactly once per page, in exactly one rail; the ids the drawers' scripts
     look up (#proc-tab, #proc-tab-badge, #nudges-rail-badge) are unique on the page; the old
     standalone .proc-tab button is gone.
  3. Processing is a normal rail tab: a .nav-tab button with id proc-tab, idle by default, its
     accent badge; base.html includes the rails BEFORE the processing drawer's script.
  4. The RIGHT list comes before the left list in the DOM (#851: the narrow-screen merged rail shows
     them in that order, so Upload leads), and style.css has the narrow-screen merge rule and the
     right-sliding processing drawer.
  5. #851: the chrome-less /admin?embed=1 page is gone; /admin renders the rails like any page.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import os
import re
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("nav-tabs-774-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import db  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
BASE = "http://testserver"

# #851 (owner decision 2026-11-08): the rail keeps four tabs; the other five live in /admin's Tools group.
EXPECTED = {
    "left": ["curator", "admin"],
    "right": ["upload", "processing"],
}
ALL_TABS = EXPECTED["left"] + EXPECTED["right"]


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def rails(html):
    """{side: [tab ids in order]} from the rendered <ul class="... nav-rail-<side>"> lists."""
    out = {}
    for side, body in re.findall(r'<ul class="[^"]*\bnav-rail-(left|right)\b[^"]*"[^>]*>(.*?)</ul>', html, re.S):
        out[side] = re.findall(r'data-nav-tab="([^"]+)"', body)
    return out


db.init_db()
tok = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)

PAGES = ["/", "/unfiled", "/hobbies", "/curator", "/wallpaper"]
for path in PAGES:
    r = tok.get(path)
    check(f"{path}: 200", r.status_code == 200, r.status_code)
    if r.status_code != 200:
        continue
    html = r.text
    got = rails(html)
    # 1. sides and order
    check(f"{path}: left rail = {EXPECTED['left']}", got.get("left") == EXPECTED["left"], got.get("left"))
    check(f"{path}: right rail = {EXPECTED['right']}", got.get("right") == EXPECTED["right"], got.get("right"))
    # 2. each tab exactly once, in exactly one rail
    for t in ALL_TABS:
        n = len(re.findall(rf'data-nav-tab="{t}"', html))
        check(f"{path}: tab {t} rendered exactly once", n == 1, n)
    for dom_id in ("proc-tab", "proc-tab-badge", "nudges-rail-badge"):
        n = len(re.findall(rf'\bid="{dom_id}"', html))
        check(f"{path}: id {dom_id} unique", n == 1, n)
    check(f"{path}: old standalone .proc-tab button gone", 'class="proc-tab' not in html)
    # 3. processing is a rail tab, idle, accent badge; rails render before the drawer script
    m = re.search(r'<li class="nav-tab-item" data-nav-tab="processing">(.*?)</li>', html, re.S)
    li = m.group(1) if m else ""
    check(f"{path}: processing is a .nav-tab button #proc-tab, idle",
          re.search(r'<button type="button" class="nav-tab idle" id="proc-tab"', li) is not None, li[:200])
    check(f"{path}: processing badge is accent-toned",
          'class="nav-tab-badge nav-tab-badge-accent" id="proc-tab-badge"' in li)
    check(f"{path}: curator badge keeps default tone",
          'class="nav-tab-badge" id="nudges-rail-badge"' in html)
    check(f"{path}: rails render before the processing drawer script",
          0 <= html.find('id="proc-tab"') < html.find("document.getElementById('proc-tab')"))
    # 4. #851: right list first in the DOM (merged narrow rail order: Upload leads)
    check(f"{path}: right list before left list (Upload first in the narrow rail)",
          0 <= html.find('nav-rail-right') < html.find('nav-rail-left'))
    # #851: the five tabs that moved are not on the rail any more (the hobby label tab included)
    check(f"{path}: no Hobbies / Blog / Wallpaper / Brand / Export tab on the rail",
          not re.search(r'data-nav-tab="(hobbies|blog|wallpaper|brand|export)"', html))

css = open(os.path.join(ROOT, "web", "static", "style.css"), encoding="utf-8").read()
check("css: narrow screens merge into one right rail",
      re.search(r"@media \(max-width: 760px\) \{\s*\.nav-rails \{[^}]*position: fixed;[^}]*right: 0;", css) is not None)
check("css: left rail pinned left, right rail pinned right",
      ".nav-rail-left { left: 0; }" in css and ".nav-rail-right { right: 0; }" in css)
check("css: processing drawer slides from the right",
      re.search(r"\.proc-drawer \{[^}]*right: 0;[^}]*translateX\(100%\)", css) is not None)
check("css: no leftover .proc-tab rules", not re.search(r"^\.proc-tab\b", css, re.M))
# Owner (2026-10-18): no transparency on any rail tab; idle Processing stays fully opaque.
nav_rules = re.findall(r"\.nav-(?:tab|rail)[^{]*\{[^}]*\}", css)
check("css: no opacity on any rail tab rule", not any("opacity" in rule for rule in nav_rules),
      [r[:80] for r in nav_rules if "opacity" in r])

# 5. #851: no embed mode any more. /admin is a normal page with the rails (and ?embed=1 is ignored).
r = tok.get("/admin?embed=1")
check("admin: /admin?embed=1 200 (the parameter is ignored now)", r.status_code == 200, r.status_code)
check("admin: the full page, with its rails (no chrome-less mode)",
      "nav-rails" in r.text and 'id="proc-tab"' in r.text)

print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
