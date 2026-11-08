#!/usr/bin/env python3
"""#851 "Tabs on the edge": a four-tab rail that fits any screen, and the full /admin page.

Throwaway SQLite DB and storage (scripts/_testenv.py), the real FastAPI app through TestClient, real
sessions for an admin, an editor and a viewer (made like scripts/test_nav_tabs_roles_779.py), plus the
install token. No server, no network:

    python scripts/test_tab_rail_admin_851.py

Owner decisions, 2026-11-08 (the spec this checks):
  1. The rail keeps FOUR tabs, in this order: left = Curator, Admin; right = Upload, Processing. The
     narrow merged rail lists the right side first, so Upload leads.
  2. The Admin tab is a plain link to the full /admin page (no popout drawer, no ?embed=1 mode).
  3. /admin answers 200 to a viewer, an editor and an admin. Its APIs keep their own roles.
  4. A Tools group at the top (Hobbies, Blog, Wallpaper, Brand, Export): each row clicks its drawer's
     hidden trigger (Wallpaper links to /wallpaper); Blog and Export need an editor and show greyed
     (the one #779 pattern) below it, never hidden.
  5. Every admin section is a collapsed-by-default <details>; a non-admin gets a greyed row ("Users
     (admins only)") that explains on click, WITHOUT the section's markup or the page's admin scripts.
  6. The rail CSS never grows past the visible screen (100dvh, with a 100vh fallback), and short
     screens get compact icon-only tabs.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import os
import re
import secrets
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("tab-rail-admin-851-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from fastapi.testclient import TestClient  # noqa: E402

from core import actor, db, install_config, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from web import roles as web_roles  # noqa: E402

FAILS = []
BASE = "http://testserver"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def rails(html):
    """{side: [tab ids in order]} from the rendered rail lists, plus the order the lists appear in."""
    out, order = {}, []
    for side, body in re.findall(r'<ul class="[^"]*\bnav-rail-(left|right)\b[^"]*"[^>]*>(.*?)</ul>', html, re.S):
        out[side] = re.findall(r'data-nav-tab="([^"]+)"', body)
        order.append(side)
    return out, order


def tab_li(html, tab):
    m = re.search(rf'<li class="nav-tab-item" data-nav-tab="{tab}">(.*?)</li>', html, re.S)
    return m.group(1) if m else ""


def tool_row(html, key):
    """The opening tag and the whole row (up to its closing tag) of one Tools row."""
    m = re.search(rf'<(button|a)\b[^>]*data-admin-tool="{key}"[^>]*>(.*?)</\1>', html, re.S)
    if not m:
        return "", ""
    return m.group(0)[: m.group(0).index(">") + 1], m.group(0)


db.init_db()
users.limiter.reset()

# ---- users and their clients --------------------------------------------------------------------
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("tr_admin", "admin"), ("tr_editor", "editor"), ("tr_viewer", "viewer")):
        PW[name] = "tr-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
CLIENTS = {"token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for role, name in (("admin", "tr_admin"), ("editor", "tr_editor"), ("viewer", "tr_viewer")):
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    check(f"{role} signs in", r.status_code == 200, r.text[:200])
    CLIENTS[role] = c

# ---- 1. the rail: four tabs, right side first in the markup ---------------------------------------
print("--- 1. the rail ---")
for who, c in CLIENTS.items():
    for path in ("/", "/admin", "/curator", "/unfiled", "/wallpaper"):
        r = c.get(path)
        check(f"{who} {path}: 200", r.status_code == 200, r.status_code)
        if r.status_code != 200:
            continue
        got, order = rails(r.text)
        check(f"{who} {path}: left rail is exactly Curator, Admin", got.get("left") == ["curator", "admin"], got.get("left"))
        check(f"{who} {path}: right rail is exactly Upload, Processing", got.get("right") == ["upload", "processing"], got.get("right"))
        check(f"{who} {path}: the right list comes first (Upload leads the narrow merged rail)", order == ["right", "left"], order)
        check(f"{who} {path}: exactly four tabs", len(re.findall(r'data-nav-tab="', r.text)) == 4,
              len(re.findall(r'data-nav-tab="', r.text)))
        check(f"{who} {path}: no Hobbies / Blog / Wallpaper / Brand / Export tab",
              not re.search(r'data-nav-tab="(hobbies|blog|wallpaper|brand|export)"', r.text))
        li = tab_li(r.text, "admin")
        check(f"{who} {path}: the Admin tab is a plain link to /admin, not a drawer or a locked button",
              re.search(r'<a class="nav-tab[^"]*" href="/admin"', li) is not None
              and "data-drawer-trigger" not in li and "nav-tab-locked" not in li and "aria-disabled" not in li, li[:300])
        if path == "/admin":
            check(f"{who} /admin: its own tab says so (aria-current=page)", 'aria-current="page"' in li, li[:300])
        else:
            check(f"{who} {path}: the Admin tab is not marked current", 'aria-current="page"' not in li)
        if who == "viewer":
            li = tab_li(r.text, "upload")
            check(f"viewer {path}: Upload is the only greyed tab",
                  "nav-tab-locked" in li and r.text.count("nav-tab-locked\"") == 1, r.text.count("nav-tab-locked"))
        else:
            check(f"{who} {path}: no greyed tab", 'class="nav-tab nav-tab-locked"' not in r.text)

# ---- 2. no admin drawer anywhere ------------------------------------------------------------------
print("--- 2. the Admin drawer is gone ---")
check("the template file _admin_drawer.html is deleted", not os.path.exists(os.path.join(ROOT, "web", "templates", "_admin_drawer.html")))
stale = []
for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "web", "templates")):
    for fn in files:
        if fn.endswith(".html"):
            txt = open(os.path.join(dirpath, fn), encoding="utf-8").read()
            if re.search(r"_admin_drawer|admin-drawer|admin_drawer", txt):
                stale.append(fn)
check("no template mentions the admin drawer", not stale, stale)
css = read("web", "static", "style.css")
check("no CSS for the admin drawer is left", not re.search(r"\.admin-drawer", css))
pages_src = read("web", "routes", "pages.py")
check("pages.py: /admin takes no embed parameter and passes no embed flag",
      "embed: int" not in pages_src and "bool(embed)" not in pages_src and '"embed"' not in pages_src)
base_html = read("web", "templates", "base.html")
check("base.html: no embed opt-out and no admin drawer include", "{% if not embed %}" not in base_html and "admin_drawer" not in base_html)
for who in ("token", "admin", "editor", "viewer"):
    for path in ("/", "/admin", "/admin?embed=1", "/curator", "/hobbies", "/wallpaper", "/unfiled"):
        r = CLIENTS[who].get(path)
        if r.status_code == 200:
            check(f"{who} {path}: no admin-drawer markup", "admin-drawer" not in r.text and "admin-drawer-frame" not in r.text)
r = CLIENTS["token"].get("/admin?embed=1")
check("/admin?embed=1 is ignored: the full page with its rails and header",
      r.status_code == 200 and "nav-rails" in r.text and 'class="app-header"' in r.text)

# ---- 3. /admin opens to every role; the APIs do not ------------------------------------------------
print("--- 3. /admin is a viewer door, its APIs keep their roles ---")
ROUTE_ROLE = {}
for rt in web_roles.flat_routes(webapp.app.routes):
    for m in (getattr(rt, "methods", None) or []):
        ROUTE_ROLE[(m, getattr(rt, "path", ""))] = web_roles.label_of(rt)
check("route table: GET /admin is viewer", ROUTE_ROLE.get(("GET", "/admin")) == "viewer", ROUTE_ROLE.get(("GET", "/admin")))
for who in ("token", "admin", "editor", "viewer"):
    r = CLIENTS[who].get("/admin")
    check(f"{who}: GET /admin is 200", r.status_code == 200, r.status_code)
anon = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
r = anon.get("/admin")
check("anonymous: GET /admin still goes to sign-in (302)", r.status_code == 302 and "/login" in r.headers.get("location", ""),
      (r.status_code, r.headers.get("location")))
# Nothing was loosened: the admin APIs the sections call still answer 403 below admin.
ADMIN_GETS = ["/api/users", "/api/settings", "/api/audit-log", "/api/admin/storage-stats", "/api/redacted",
              "/api/restricted", "/api/trash", "/api/install-config", "/api/backups", "/api/captions/defaults"]
for path in ADMIN_GETS:
    check(f"route table: GET {path} is still admin", ROUTE_ROLE.get(("GET", path)) == "admin", ROUTE_ROLE.get(("GET", path)))
    for who in ("editor", "viewer"):
        r = CLIENTS[who].get(path)
        check(f"{who}: GET {path} is still 403", r.status_code == 403, r.status_code)
for m, path in (("POST", "/api/backup"), ("POST", "/api/delete-all"), ("POST", "/api/trash/empty"), ("POST", "/api/settings"),
                ("POST", "/api/users"), ("POST", "/api/install-config")):
    check(f"route table: {m} {path} is still admin", ROUTE_ROLE.get((m, path)) == "admin", ROUTE_ROLE.get((m, path)))

# ---- 4. the Tools group ----------------------------------------------------------------------------
print("--- 4. the Tools group ---")
TOOLS = {  # key: (label, trigger id or None, href or None, role the tool needs or None)
    "hobbies": ("Hobbies", "hobbies-drawer-trigger", None, None),
    "blog": ("Blog", "blog-builder-trigger", None, "editor"),
    "wallpaper": ("Wallpaper", None, "/wallpaper", None),
    "brand": ("Brand", "brand-drawer-trigger", None, None),
    "export": ("Export", "export-builder-trigger", None, "editor"),
}
RANK = {"viewer": 1, "editor": 2, "admin": 3, "token": 3}
ART = {"viewer": "a", "editor": "an", "admin": "an", "token": "an"}  # the article before the user's role in the message
for who, c in CLIENTS.items():
    html = c.get("/admin").text
    tools_at = html.find('data-admin-section="tools"')
    first_section = html.find('data-admin-section="pending"')
    check(f"{who}: the Tools group is first, ahead of every other section", 0 <= tools_at < first_section, (tools_at, first_section))
    check(f"{who}: the Tools group is open by default", re.search(r'<details[^>]*data-admin-section="tools"[^>]*\bopen\b', html) is not None)
    keys_in_order = re.findall(r'data-admin-tool="([^"]+)"', html)
    check(f"{who}: five tools, in the owner's order", keys_in_order == list(TOOLS), keys_in_order)
    for key, (label, trigger, href, need) in TOOLS.items():
        tag, row = tool_row(html, key)
        locked = need is not None and RANK[who] < RANK[need]
        if locked:
            msg = (re.search(r'data-locked-message="([^"]*)"', tag) or [None, ""])[1]
            check(f"{who}: {key} row is greyed (the one #779 pattern), not hidden",
                  'class="admin-tool-row role-locked"' in tag and 'aria-disabled="true"' in tag
                  and f'data-locked-role="{need}"' in tag and f'title="{label} (editors and admins)"' in tag, tag[:300])
            check(f"{who}: {key} row names the role it needs, in its own words",
                  "(editors and admins)" in row and label in row, row[:300])
            check(f"{who}: {key} row's click message names it, the role, the user's own role and who to ask",
                  msg.startswith(f"{label} needs the {need} role") and f"{ART[who]} {who}." in msg and "Ask an admin" in msg, msg)
            check(f"{who}: {key} locked row carries no trigger and no link",
                  "data-tool-trigger" not in tag and 'href="' not in tag, tag[:300])
        else:
            check(f"{who}: {key} row is live", tag != "" and "role-locked" not in tag and "aria-disabled" not in tag, tag[:300])
            if href:
                check(f"{who}: {key} is a link to {href}", tag.startswith("<a ") and f'href="{href}"' in tag, tag[:300])
            else:
                check(f"{who}: {key} clicks the hidden trigger {trigger}", f'data-tool-trigger="{trigger}"' in tag, tag[:300])
                n = len(re.findall(rf'\bid="{re.escape(trigger)}"', html))
                check(f"{who}: the trigger {trigger} is on the page exactly once (the row cannot be a dead click)", n == 1, n)
    # the drawers behind the greyed rows are not sent
    if RANK[who] < RANK["editor"]:
        for marker in ('id="blog-builder"', 'id="blog-builder-trigger"', 'id="export-builder"', 'id="export-builder-trigger"'):
            check(f"{who}: {marker} not sent (rows are greyed, markup left off)", marker not in html)
    else:
        for marker in ('id="blog-builder"', 'id="export-builder"'):
            check(f"{who}: {marker} sent", marker in html)
    for marker in ('id="brand-drawer"', 'id="hobbies-drawer"'):
        check(f"{who}: {marker} sent to every role", marker in html, marker)

# ---- 5. the admin sections ---------------------------------------------------------------------------
print("--- 5. the admin sections ---")
SECTIONS = {  # key: (heading, markup only that section carries)
    "redacted": ("Redacted items", 'id="admin-redacted-list"'),
    "trash": ("Trash", 'id="admin-empty-trash-btn"'),
    "restricted": ("Sensitive items", 'id="admin-restricted-list"'),
    "provenance": ("Provenance options", 'id="prov-items-card"'),
    "vocab": ("Word lists", 'id="vocab-items-brand_role"'),
    "tags": ("Tags", 'id="admin-tags-tree"'),
    "install": ("Install", 'id="admin-install-fields"'),
    "users": ("Users", 'id="admin-users-rows"'),
    "apikeys": ("API keys", 'id="admin-apikey-input-youtube_data_api_key"'),
    "caption-tuning": ("Caption tuning", 'id="admin-caption-run"'),
    "activity": ("Recent activity", 'id="admin-audit-log-container"'),
    "storage": ("Storage", 'id="admin-storage-content"'),
    "maintenance": ("Maintenance", 'id="admin-delete-all-btn"'),
}
COUNTS = {"redacted": "admin-redacted-count", "trash": "admin-trash-count", "restricted": "admin-restricted-count",
          "tags": "admin-tags-count", "users": "admin-users-count"}
# Calls only an admin's script makes: not one of these strings may reach a lower role's page.
ADMIN_API_STRINGS = ["/api/users", "/api/audit-log", "/api/admin/storage-stats", "/api/redacted",
                     "/api/restricted", "/api/trash", "/api/install-config", "/api/backup", "/api/delete-all",
                     "/api/captions/defaults", "/api/captions/test", "/api/provenance-options", "/api/tags/tree",
                     "/api/vocab/"]
ADMIN_SCRIPTS = ["/static/js/admin-tags.js", "/static/js/admin-vocab.js", "API_KEY_SETTINGS"]
for who, c in CLIENTS.items():
    html = c.get("/admin").text
    is_admin = RANK[who] >= RANK["admin"]
    check(f"{who}: the page title is still Admin", "<title>Admin — " in html and '<h1 class="page-title">Admin</h1>' in html)
    check(f"{who}: the disclosure script is sent", "/static/js/admin-sections.js" in html)
    pend = re.search(r'<details[^>]*data-admin-section="pending"[^>]*>', html)
    check(f"{who}: 'Needs your input' (just a link to the Curator) is there for every role, collapsed",
          pend is not None and "open" not in pend.group(0).split("data-admin-section")[1], pend and pend.group(0))
    check(f"{who}: 'Needs your input' carries its link", 'id="admin-curator-link"' in html)
    for key, (heading, marker) in SECTIONS.items():
        det = re.search(rf'<details[^>]*data-admin-section="{key}"[^>]*>', html)
        locked = re.search(rf'<button[^>]*data-admin-locked="{key}"[^>]*>(.*?)</button>', html, re.S)
        if is_admin:
            check(f"{who}: section {key} is a collapsed <details> (no open attribute)",
                  det is not None and not re.search(r"\sopen\b", det.group(0)), det and det.group(0))
            check(f"{who}: section {key} keeps its heading in the summary line",
                  re.search(rf'<summary[^>]*>\s*<h2[^>]*>{re.escape(heading)}\b', html) is not None)
            check(f"{who}: section {key} carries its markup", marker in html, marker)
            check(f"{who}: section {key} is not drawn as a locked row", locked is None)
            if key in COUNTS:
                check(f"{who}: section {key} keeps its count span in the summary",
                      re.search(rf'<summary[^>]*>\s*<h2[^>]*>[^<]*<span id="{COUNTS[key]}"', html) is not None)
        else:
            tag = (re.search(rf'<button[^>]*data-admin-locked="{key}"[^>]*>', html) or [""])[0]
            msg = (re.search(r'data-locked-message="([^"]*)"', tag) or [None, ""])[1]
            check(f"{who}: section {key} is a greyed row labelled with the role it needs",
                  locked is not None and 'class="admin-locked-row role-locked"' in tag and 'aria-disabled="true"' in tag
                  and f"{heading} <span" in locked.group(0) and "(admins only)" in locked.group(1), locked and locked.group(0)[:300])
            check(f"{who}: section {key} row's click message names it, the role, the user's role, who to ask",
                  msg.startswith(f"{heading} needs the admin role") and f"{ART[who]} {who}." in msg and "Ask an admin" in msg, msg)
            check(f"{who}: section {key}: no details element, and its markup is not sent", det is None and marker not in html)
    if is_admin:
        for s in ADMIN_SCRIPTS + ["/api/users", "/api/settings", "/api/audit-log", "/api/admin/storage-stats"]:
            check(f"{who}: the admin script block is sent ({s})", s in html, s)
        check(f"{who}: the setup / restart notices are there for an admin", 'id="hobby-label-mcp-notice"' in html
              and (('id="install-setup-banner"' in html) == install_config.setup_needed()))
    else:
        for s in ADMIN_API_STRINGS + ADMIN_SCRIPTS:
            check(f"{who}: the page never mentions {s} (no admin API is called, no 403 noise)", s not in html, s)
        # /api/settings is the one admin API a shell drawer names: the editor's Export builder reads it only
        # `canPublish ? fetch(...)` (admins), unchanged by #851. It must not come from the admin page itself.
        check(f"{who}: /api/settings appears only in the Export builder's guarded call, not from the admin page",
              html.count("/api/settings") == (1 if who == "editor" else 0), html.count("/api/settings"))
        for ident in ("hobby-label-mcp-notice", "install-setup-banner", "users-setup-banner"):
            check(f"{who}: the admin-only notice {ident} is not sent", ident not in html)
        # every inline <script> is only the shell's: none of the admin page's own giant block
        check(f"{who}: the admin page's own script block is not sent", "const API_KEY_SETTINGS" not in html
              and "loadStorageStats" not in html)
    inline = re.findall(r"<script\b([^>]*)>", html)
    bare = [a for a in inline if "src=" not in a and 'nonce="' not in a]
    check(f"{who}: every inline script on /admin carries a nonce", not bare, bare[:3])

# ---- 6. CSS: fits any screen, compact on short ones, Upload first --------------------------------------
print("--- 6. the rail CSS ---")
m = re.search(r"\n\.nav-rail \{([^}]*)\}", css)
rule = m.group(1) if m else ""
check("css: .nav-rail is capped at 100dvh with a 100vh fallback line before it",
      re.search(r"max-height:\s*100vh;\s*max-height:\s*100dvh", rule) is not None, rule)
check("css: .nav-rail scrolls (overflow-y: auto) rather than running off the screen", "overflow-y: auto" in rule, rule)
narrow = next((b for b in re.findall(r"@media \(max-width: 760px\) \{(.*?)\n\}\n", css, re.S) if ".nav-rails {" in b), "")
check("css: the merged narrow rail (.nav-rails) is also capped at 100dvh with a 100vh fallback",
      re.search(r"\.nav-rails \{[^}]*max-height:\s*100vh;\s*max-height:\s*100dvh;\s*overflow-y:\s*auto", narrow) is not None, narrow[:300])
check("css: inside the merged rail each list is a plain box (its own cap and scrolling are off)",
      re.search(r"\.nav-rails \.nav-rail \{[^}]*max-height:\s*none;[^}]*overflow:\s*visible", narrow) is not None)
check("css: no stale max-height:100vh-only rail rule is left", not re.search(r"max-height:\s*100vh;(?!\s*max-height:\s*100dvh)", css[css.find(".nav-rail {"):css.find(".nav-rail {") + 3000]))
compact = next((b for b in re.findall(r"@media \(max-height: 560px\) \{(.*?)\n\}\n", css, re.S) if ".nav-tab-label" in b), "")
check("css: a short-screen (max-height: 560px) block exists", compact != "")
check("css: short screens hide the tab label visually (1px, clipped) but keep it in the markup",
      re.search(r"\.nav-tab-label \{[^}]*width:\s*1px;[^}]*height:\s*1px;[^}]*clip-path", compact) is not None, compact[:400])
check("css: short screens use less padding and a smaller gap", re.search(r"\.nav-tabs-list \{\s*gap:\s*6px", compact) is not None
      and re.search(r"\.nav-tab \{\s*padding:\s*8px 7px", compact) is not None, compact[:300])
check("css: short screens keep the badge (shrunk, not removed)", re.search(r"\.nav-tab-badge \{[^}]*min-width:\s*14px", compact) is not None
      and "display: none" not in compact)
nav_rules = re.findall(r"\.nav-(?:tab|rail)[^{]*\{[^}]*\}", css)
check("css: no opacity on any rail tab rule (owner: no transparency on tabs)", not any("opacity" in r for r in nav_rules))
tabs_html = read("web", "templates", "_nav_tabs.html")
check("template: the right side is written first, so Upload leads the narrow rail",
      re.search(r'\{%-?\s*for side in \["right", "left"\]', tabs_html) is not None)
check("css: nothing reorders the rails (no order: on .nav-rail*)", not re.search(r"\.nav-rail[^{]*\{[^}]*\border:", css))
# The tab's height budget on a short phone: icon-only tab = 8+20+8 padding + 2x3 border = 42px, so even
# four in one rail with 6px gaps is 4*42 + 3*6 = 186px: it fits a 330px landscape phone with room to spare.
check("css: compact tab geometry leaves room (4 tabs * 42px + 3 * 6px gaps < 330px)", 4 * 42 + 3 * 6 < 330)

# ---- 7. the page's own CSS and script wiring ---------------------------------------------------------------
print("--- 7. disclosure CSS and script ---")
check("css: .admin-grid is a plain stack (no dense multi-column grid)", re.search(r"\.admin-grid \{\s*display:\s*flex;\s*flex-direction:\s*column", css) is not None
      and "grid-auto-flow: dense" not in css)
check("css: sections have a summary row, a body and a chevron",
      all(s in css for s in (".admin-section-summary", ".admin-section-body", ".admin-section[open] > .admin-section-summary::before",
                              ".admin-locked-row", ".admin-tool-row")))
js = read("web", "static", "js", "admin-sections.js")
code = re.sub(r"//[^\n]*", "", js)  # the code without its comments
check("script: localStorage is read and written only inside try/catch",
      re.search(r"try\s*\{[^}]*localStorage\.getItem", code) is not None
      and re.search(r"try\s*\{[^}]*localStorage\.setItem", code) is not None
      and code.count("localStorage") == 2)
check("script: remembers on toggle, opens a #hash section, clicks the hidden trigger and says so if it is missing",
      "'toggle'" in js and "hashchange" in js and "trigger.click()" in js and "can't open here" in js)
check("script: no inline handlers, no eval", not re.search(r"\bon[a-z]+\s*=\s*[\"']|\beval\(|new Function", js))

print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nALL PASS")
sys.exit(1 if FAILS else 0)
