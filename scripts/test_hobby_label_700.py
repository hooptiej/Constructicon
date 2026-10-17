#!/usr/bin/env python3
"""#700 "A hobby by any other name": the install's word for a hobby (Admin > Install).

Throwaway SQLite DB and storage (scripts/_testenv.py), built by the real `db.init_db()`; the real
FastAPI app through TestClient (install token = admin; a signed-in viewer and editor for the role
checks); the MCP server module imported in-process (default label) and in a child process (with a
label set). No server, no network:

    python scripts/test_hobby_label_700.py

  1. Default label: the hobby pages, nav, drawer and project page read exactly as before ("Hobbies",
     "— Hobby —", "Convert to hobby"), /hobbies and /hobby/<slug> serve 200 with no redirect, the
     links point at them, and the page carries data-hobby-label with the default words.
  2. Validation: reserved words / route collisions, singular = plural, bad characters, too short /
     long, a lone singular, the crossed hobby/hobbies words, non-text: all 400 bad_hobby_label, and
     nothing is written.
  3. Department / Departments through the real admin route: one undoable change-log row; every page
     says Department(s) (titles, nav, drawer, buttons, card face, project page, guide, 404 text);
     /departments and /department/<slug> serve; /hobbies and /hobby/<slug>?q 308-redirect to them
     (query kept); the API paths are untouched; card_rules.HOBBY_LABEL follows; the admin page
     carries the warning text and, once an MCP has recorded an older label, the restart notice.
  4. A second rename (Team / Teams) redirects the previous label's URLs too; undoing it brings
     Department back and /teams keeps redirecting (known slugs survive an undo); clearing both
     labels is the default again and every earlier label's URLs redirect to /hobbies.
  5. Roles: a viewer and an editor get 403 on GET and POST /api/install-config (admin only).
  6. MCP: with the default label the tool list equals the snapshot (scripts/data/
     mcp_tool_names_700.json) and every description is the docstring as before; building the
     registrations with Department renames exactly the hobby tools (and their descriptions), keeps
     every other name; in a child process with the label set, tools/list shows the renamed names,
     not the old ones, and an old name still answers tools/call (hidden alias), parameters still
     `hobby`.
  7. web/hobby_paths.resolve edge cases, and every first segment of the route table is reserved.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import asyncio
import inspect
import json
import os
import re
import secrets
import subprocess
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("hobby-label-700-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from fastapi.testclient import TestClient  # noqa: E402

from core import card_rules, cards, db, hobbies, install_config, users  # noqa: E402
from core import hobby_label as hl  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp, hobby_paths  # noqa: E402

FAILS = []
BASE = "http://testserver"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def err_code(r):
    try:
        return (r.json().get("error") or {}).get("code")
    except ValueError:
        return None


def visible(html):
    """The page's text without tags, comments, scripts and attributes (what a reader sees)."""
    html = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", " ", html, flags=re.S | re.I)
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


db.init_db()
tok = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)


def save(body):
    return tok.post("/api/install-config", json=body, headers=SAME)


# ---- fixtures: one hobby with one card in it ------------------------------------------------------
HOBBY = hobbies.create("Rocketry").data["hobby"]
card_row = cards.create("Big Booster", kind="thing").data["card"]
hobbies.add_card(HOBBY["id"], card_row["id"])
CARD_SLUG = card_row["slug"]
HSLUG = HOBBY["slug"]
check("fixture: a hobby and a card in it", any(p["id"] == card_row["id"] for p in db.list_projects_for_hobby(HOBBY["id"])))

# ---- 1. the default label -------------------------------------------------------------------------
print("--- 1. default ---")
r = tok.get("/hobbies")
check("default: /hobbies 200 (no redirect)", r.status_code == 200, r.status_code)
check("default: index title and heading say Hobbies",
      "<title>Hobbies — " in r.text and '<h1 class="page-title">Hobbies</h1>' in r.text)
check("default: + New hobby button", "+ New hobby</button>" in r.text)
check("default: nav rail label Hobbies", '<span class="nav-tab-label">Hobbies</span>' in r.text)
check("default: drawer heading Hobbies", "<h2>Hobbies</h2>" in r.text)
check("default: <html data-hobby-label> carries the default words",
      "&#34;one&#34;: &#34;Hobby&#34;" in r.text and "&#34;many&#34;: &#34;Hobbies&#34;" in r.text
      and "&#34;is_default&#34;: true" in r.text, r.text[:200])
check("default: hobby-label.js loaded", "/static/js/hobby-label.js?v=" in r.text)
r = tok.get(f"/hobby/{HSLUG}")
check("default: /hobby/<slug> 200", r.status_code == 200, r.status_code)
check("default: hobby page title '<name> — Hobby —'", f"<title>Rocketry — Hobby — " in r.text)
check("default: breadcrumb links /hobbies", '<a href="/hobbies">Hobbies</a>' in r.text)
check("default: card face type line 'Hobby · Active'", "Hobby · Active" in r.text)
check("default: 'Add a project to this hobby'", "Add a project to this hobby" in r.text)
r = tok.get(f"/project/{CARD_SLUG}")
check("default: project page 'Convert to hobby' and the hobby link",
      "Convert to hobby</button>" in r.text and f'<a href="/hobby/{HSLUG}">Rocketry</a>' in r.text)
check("default: /departments is just a 404", tok.get("/departments").status_code == 404)
check("default: card_rules.HOBBY_LABEL is Hobby", card_rules.HOBBY_LABEL == "Hobby")
pub = tok.get("/api/install-config").json()
check("default: install config lists both label fields with Hobby/Hobbies fallbacks",
      {"hobby_label", "hobby_label_plural"} <= {f["key"] for f in pub["fields"]}
      and pub["fallbacks"]["hobby_label"] == "Hobby" and pub["fallbacks"]["hobby_label_plural"] == "Hobbies"
      and pub["hobby_label"]["current"]["is_default"] is True)

# ---- 2. validation ----------------------------------------------------------------------------------
print("--- 2. validation ---")
BAD = [
    ({"hobby_label": "Admin", "hobby_label_plural": "Admins"}, "reserved word (admin route)"),
    ({"hobby_label": "Project", "hobby_label_plural": "Projects"}, "collides with /project"),
    ({"hobby_label": "Tag", "hobby_label_plural": "Tags"}, "reserved word (tags)"),
    ({"hobby_label": "Upload", "hobby_label_plural": "Uploaders"}, "collides with /upload"),
    ({"hobby_label": "Sheep", "hobby_label_plural": "Sheep"}, "singular = plural slug"),
    ({"hobby_label": "Fish", "hobby_label_plural": "FISH"}, "same slug, other case"),
    ({"hobby_label": "Dept<x>", "hobby_label_plural": "Depts"}, "bad characters"),
    ({"hobby_label": "Dept/x", "hobby_label_plural": "Depts"}, "a slash"),
    ({"hobby_label": "9 Lives", "hobby_label_plural": "Lives"}, "starts with a digit"),
    ({"hobby_label": "A", "hobby_label_plural": "As"}, "too short"),
    ({"hobby_label": "D" * 40, "hobby_label_plural": "Ds"}, "too long"),
    ({"hobby_label": "Department"}, "a lone singular (plural unset)"),
    ({"hobby_label_plural": "Departments"}, "a lone plural"),
    ({"hobby_label": "Hobbies", "hobby_label_plural": "Hobbieses"}, "crossed: singular hobbies"),
    ({"hobby_label": "Thing", "hobby_label_plural": "Hobby"}, "crossed: plural hobby"),
    ({"hobby_label": 5, "hobby_label_plural": "Fives"}, "not text"),
]
cfg_before = db.get_conn().execute("SELECT key, value FROM install_config ORDER BY key").fetchall()
cfg_before = [tuple(x) for x in cfg_before]
for body, why in BAD:
    r = save(body)
    check(f"refused: {why} -> 400 bad_hobby_label", r.status_code == 400 and err_code(r) == "bad_hobby_label",
          f"{r.status_code} {r.text[:160]}")
cfg_after = [tuple(x) for x in db.get_conn().execute("SELECT key, value FROM install_config ORDER BY key").fetchall()]
check("refusals wrote nothing", cfg_before == cfg_after, f"{cfg_before} -> {cfg_after}")
check("rules: validate_pair keeps the words, title-cases the first letter",
      hl.validate_pair("area of study", "areas of study") == ("Area of study", "Areas of study"))
check("rules: slugs drop accents and punctuation", hl.slugify("R&D Área") == "r-d-area")
r = save({"hobby_label": "Hobby", "hobby_label_plural": "Hobbies"})
check("saving the default words stores nothing (the default is 'not set')",
      r.status_code == 200 and r.json().get("batch_id") is None, r.text[:200])

# ---- 3. Department / Departments --------------------------------------------------------------------
print("--- 3. Department ---")
r = save({"hobby_label": "Department", "hobby_label_plural": "Departments"})
ok = r.status_code == 200
B1 = r.json().get("batch_id") if ok else None
check("set Department/Departments through POST /api/install-config", ok and B1, r.text[:200])
crow = [(x["op"], x["actor"]) for x in db.get_change_rows(batch_id=B1)]
check("one change-log row, op install_config_update, actor token", crow == [("install_config_update", "token")], crow)
check("the answer's config says Department, not default",
      r.json()["config"]["hobby_label"]["current"]["one"] == "Department"
      and r.json()["config"]["hobby_label"]["current"]["is_default"] is False)
check("card_rules.HOBBY_LABEL follows (Department)", card_rules.HOBBY_LABEL == "Department")

r = tok.get("/departments")
check("/departments 200", r.status_code == 200, r.status_code)
txt = visible(r.text)
check("/departments: title, heading, button and nav say Department(s)",
      "<title>Departments — " in r.text and '<h1 class="page-title">Departments</h1>' in r.text
      and "+ New department</button>" in r.text and '<span class="nav-tab-label">Departments</span>' in r.text)
check("/departments: no visible 'hobby'/'hobbies' word left", not re.search(r"\bhobb(y|ies)\b", txt, re.I),
      re.findall(r".{30}\bhobb(?:y|ies)\b.{30}", txt, re.I)[:3])
check("/departments: data-hobby-label carries Department",
      "&#34;one&#34;: &#34;Department&#34;" in r.text and "&#34;index_url&#34;: &#34;/departments&#34;" in r.text)
r = tok.get(f"/department/{HSLUG}")
check("/department/<slug> 200", r.status_code == 200, r.status_code)
txt = visible(r.text)
check("department page: title, breadcrumb, card face, labels",
      "<title>Rocketry — Department — " in r.text and '<a href="/departments">Departments</a>' in r.text
      and "Department · Active" in r.text and "Add a project to this department" in r.text
      and "All departments" in r.text)
check("department page: no visible 'hobby' word left", not re.search(r"\bhobb(y|ies)\b", txt, re.I),
      re.findall(r".{30}\bhobb(?:y|ies)\b.{30}", txt, re.I)[:3])
r = tok.get(f"/project/{CARD_SLUG}")
check("project page: 'Convert to department', Departments row, link to /department/<slug>",
      "Convert to department</button>" in r.text and "<dt>Departments</dt>" in r.text
      and f'<a href="/department/{HSLUG}">Rocketry</a>' in r.text)
check("project page: no visible 'hobby' word left", not re.search(r"\bhobb(y|ies)\b", visible(r.text), re.I),
      re.findall(r".{30}\bhobb(?:y|ies)\b.{30}", visible(r.text), re.I)[:3])
r = tok.get("/")
check("home: nav and drawer say Departments, no visible 'hobby' word",
      '<span class="nav-tab-label">Departments</span>' in r.text
      and not re.search(r"\bhobb(y|ies)\b", visible(r.text), re.I),
      re.findall(r".{30}\bhobb(?:y|ies)\b.{30}", visible(r.text), re.I)[:3])
r = tok.get("/guides/capture-physical-piece")
check("guide: relabelled (department, no visible hobby)", r.status_code == 200 and "department" in visible(r.text).lower()
      and not re.search(r"\bhobb(y|ies)\b", visible(r.text), re.I))
r = tok.get("/department/no-such-one")
check("unknown department: 404 'department not found'", r.status_code == 404 and "department not found" in r.text, r.text[:120])

r = tok.get("/hobbies")
check("/hobbies -> 308 /departments", r.status_code == 308 and r.headers["location"] == "/departments",
      f"{r.status_code} {r.headers.get('location')}")
r = tok.get(f"/hobby/{HSLUG}?rev=all&x=1")
check("/hobby/<slug>?q -> 308 /department/<slug>?q", r.status_code == 308
      and r.headers["location"] == f"/department/{HSLUG}?rev=all&x=1", f"{r.status_code} {r.headers.get('location')}")
r = tok.get("/hobbies", follow_redirects=True)
check("following the redirect lands on the Departments page", r.status_code == 200 and "<h1 class=\"page-title\">Departments</h1>" in r.text)
anon = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
r = anon.get("/hobbies")
check("anonymous: /hobbies -> 308 /departments too (then the sign-in gate)", r.status_code == 308
      and r.headers["location"] == "/departments")
r = anon.get("/departments")
check("anonymous: /departments -> the sign-in page", r.status_code in (302, 303) and "/login" in r.headers.get("location", "")
      or r.status_code == 302 and "/setup" in r.headers.get("location", ""), f"{r.status_code} {r.headers.get('location')}")
check("API paths unchanged: GET /api/hobbies 200", tok.get("/api/hobbies").status_code == 200)
check("API paths unchanged: GET /api/hobby/<slug> 200 with field 'slug'",
      tok.get(f"/api/hobby/{HSLUG}").status_code == 200 and tok.get(f"/api/hobby/{HSLUG}").json().get("slug") == HSLUG)
check("API not redirected: /api/departments is a 404", tok.get("/api/departments").status_code == 404)
faces = tok.get("/api/hobbies/html")
check("drawer fragment: card links /department/<slug>", f'/department/{HSLUG}' in faces.text)

r = tok.get("/admin")
check("admin page carries the warning text (immediate / relaunch / unchanged)",
      install_config.HOBBY_LABEL_WARNING["immediate"].split(":")[0] in r.text
      and "Restart the MCP\n" not in r.text and "MCP clients such as Claude must reconnect" in r.text
      and "constructicon_create_hobby becomes constructicon_create_&lt;word&gt;" in r.text
      and "JSON field names" in r.text)
check("admin: no MCP recorded yet -> the restart notice is hidden", 'id="hobby-label-mcp-notice" role="status" hidden' in r.text)
install_config.record_mcp_started_label(hl.DEFAULT)
st = install_config.hobby_label_status()
check("an MCP that started with Hobby/Hobbies -> mcp_needs_restart True", st["mcp_needs_restart"] is True, st)
r = tok.get("/admin")
check("admin: the persistent restart notice shows, naming both labels",
      'id="hobby-label-mcp-notice" role="status" hidden' not in r.text
      and 'still named for "Hobby / Hobbies", but this install now says "Department / Departments"' in r.text)
install_config.record_mcp_started_label(install_config.hobby_label())
check("after the MCP restarts with Department -> no restart needed",
      install_config.hobby_label_status()["mcp_needs_restart"] is False)

# ---- 4. a second rename, undo, clear --------------------------------------------------------------
print("--- 4. Team, undo, clear ---")
r = save({"hobby_label": "Team", "hobby_label_plural": "Teams"})
B2 = r.json().get("batch_id")
check("rename to Team/Teams", r.status_code == 200 and B2)
check("/teams 200", tok.get("/teams").status_code == 200)
r = tok.get("/departments")
check("previous label: /departments -> 308 /teams", r.status_code == 308 and r.headers["location"] == "/teams")
r = tok.get(f"/department/{HSLUG}")
check("previous label: /department/<slug> -> 308 /team/<slug>", r.status_code == 308
      and r.headers["location"] == f"/team/{HSLUG}")
r = tok.get("/hobbies")
check("default paths: /hobbies -> 308 /teams", r.status_code == 308 and r.headers["location"] == "/teams")
check("known label slugs remembered", install_config.known_label_slugs()
      == [("hobby", "hobbies"), ("department", "departments"), ("team", "teams")], install_config.known_label_slugs())
r = tok.post(f"/api/changes/{B2}/undo", headers=SAME)
check("undo the Team rename (generic undo)", r.status_code == 200, r.text[:200])
check("undo: the label is Department again", install_config.hobby_label().one == "Department")
check("undo: /departments 200 again", tok.get("/departments").status_code == 200)
r = tok.get("/teams")
check("undo: /teams still redirects (-> /departments)", r.status_code == 308 and r.headers["location"] == "/departments")
r = save({"hobby_label": "", "hobby_label_plural": ""})
check("clear both labels -> default", r.status_code == 200 and install_config.hobby_label().is_default)
check("cleared: no label rows left", not db.get_conn().execute(
    "SELECT 1 FROM install_config WHERE key IN ('hobby_label', 'hobby_label_plural')").fetchall())
check("cleared: /hobbies 200 again", tok.get("/hobbies").status_code == 200)
for old in ("/departments", "/teams"):
    r = tok.get(old)
    check(f"cleared: {old} -> 308 /hobbies", r.status_code == 308 and r.headers["location"] == "/hobbies")
r = tok.get(f"/team/{HSLUG}")
check("cleared: /team/<slug> -> 308 /hobby/<slug>", r.status_code == 308 and r.headers["location"] == f"/hobby/{HSLUG}")
r = tok.get(f"/hobby/{HSLUG}")
check("cleared: the hobby page reads as before", r.status_code == 200 and "<title>Rocketry — Hobby — " in r.text)
check("cleared: card_rules.HOBBY_LABEL is Hobby again", card_rules.HOBBY_LABEL == "Hobby")

# ---- 5. roles ---------------------------------------------------------------------------------------
print("--- 5. roles ---")
ADMIN_PW = "a7-" + secrets.token_urlsafe(12)
users.create_first_admin("boss700", ADMIN_PW)
for role in ("viewer", "editor"):
    pw = "r7-" + secrets.token_urlsafe(12)
    users.create_user(f"{role}700", pw, role)
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": f"{role}700", "password": pw}, headers=SAME)
    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', c.get("/").text)
    h = {**SAME, "X-CSRF-Token": csrf.group(1) if csrf else ""}
    check(f"{role} signs in", r.status_code == 200, r.text[:120])
    check(f"{role}: GET /api/install-config -> 403", c.get("/api/install-config").status_code == 403)
    r = c.post("/api/install-config", json={"hobby_label": "Club", "hobby_label_plural": "Clubs"}, headers=h)
    check(f"{role}: POST /api/install-config -> 403 forbidden", r.status_code == 403 and err_code(r) == "forbidden",
          f"{r.status_code} {r.text[:120]}")
    check(f"{role}: label unchanged", install_config.hobby_label().is_default)
    check(f"{role}: can still read the pages (/hobbies 200)", c.get("/hobbies").status_code == 200)

# ---- 6. MCP -----------------------------------------------------------------------------------------
print("--- 6. MCP ---")
SNAP = json.load(open(os.path.join(ROOT, "scripts", "data", "mcp_tool_names_700.json"), encoding="utf-8"))["names"]
from mcp_server import server  # noqa: E402  (label is the default now)
listed = asyncio.run(server.mcp.list_tools())
names = sorted(t.name for t in listed)
check("MCP default: tools/list equals the snapshot exactly", names == sorted(SNAP),
      f"extra {sorted(set(names) - set(SNAP))} missing {sorted(set(SNAP) - set(names))}")
funcs = {n: f for n, f in server._WRAPPED_TOOLS.items() if n not in server._HIDDEN_ALIASES}
check("MCP default: every description is the docstring (indentation removed, #746)",
      all(t.description == inspect.cleandoc(funcs[t.name].__doc__ or "") for t in listed))
check("MCP default: earlier labels' names are hidden aliases (not listed)",
      "constructicon_create_department" in server._HIDDEN_ALIASES and "constructicon_list_teams" in server._HIDDEN_ALIASES
      and "constructicon_create_department" not in names)
DEPT = hl.Label("Department", "Departments")
regs = {n: server.tool_registrations(f.__name__, f.__doc__, DEPT) for n, f in funcs.items()}
renamed = sorted(r[0][0] for r in regs.values())
expect = sorted(n.replace("_hobbies", "_departments").replace("_hobby", "_department") for n in SNAP)
check("MCP Department: names = the snapshot with hobby->department, nothing else changed", renamed == expect,
      sorted(set(renamed) ^ set(expect)))
check("MCP Department: e.g. create/list/card_department/update/unmark",
      {"constructicon_create_department", "constructicon_list_departments", "constructicon_card_department",
       "constructicon_update_department", "constructicon_unmark_department"} <= set(renamed))
check("MCP Department: #746's retired hobby names are hidden under every label",
      {"constructicon_add_project_to_department", "constructicon_set_department_code",
       "constructicon_add_project_to_hobby"} <= server._HIDDEN_ALIASES)
desc = regs["constructicon_create_hobby"][0][1] or ""
check("MCP Department: create's description says department, no stray 'hobby' word",
      "epartment" in desc and not re.search(r"(?<![`\w/'\"-])hobb(y|ies)(?![`\w=:/'\"-])", desc, re.I),
      re.findall(r".{20}hobb(?:y|ies).{20}", desc, re.I)[:3])
check("MCP Department: tool names mentioned in descriptions are renamed too",
      not any(re.search(r"constructicon_\w*hobb", (r[0][1] or "")) for r in regs.values()))
check("MCP Department: the old names become hidden aliases",
      regs["constructicon_create_hobby"][1:] and regs["constructicon_create_hobby"][1][0] == "constructicon_create_hobby"
      and regs["constructicon_create_hobby"][1][2] is True)
check("MCP Department: a tool without 'hobby' in its name keeps it and gets no alias",
      [x[0] for x in regs["constructicon_search"]] == ["constructicon_search"])

# a real MCP process start: the label set in the DB, then tools/list and a call through an old name
save({"hobby_label": "Department", "hobby_label_plural": "Departments"})
child = r'''
import asyncio, json, sys, types
sys.path.insert(0, sys.argv[1])
try:
    import cairosvg
except Exception:
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")
from mcp_server import server
tools = asyncio.run(server.mcp.list_tools())
res = asyncio.run(server.mcp.call_tool("constructicon_list_hobbies", {}))
schema = next(t for t in tools if t.name == "constructicon_card_department").input_schema
print(json.dumps({"names": sorted(t.name for t in tools), "started": [server.STARTED_LABEL.one, server.STARTED_LABEL.many],
                  "alias_call_error": bool(getattr(res, "is_error", False)), "params": sorted(schema.get("properties", {})),
                  "desc": next(t.description for t in tools if t.name == "constructicon_list_departments")}))
'''
p = subprocess.run([sys.executable, "-c", child, ROOT], capture_output=True, text=True, timeout=300, env=os.environ.copy())
try:
    out = json.loads(p.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    out = {}
check("MCP process with Department: it started with Department/Departments",
      out.get("started") == ["Department", "Departments"], (p.stdout[-300:], p.stderr[-600:]))
check("MCP process: tools/list shows the renamed tools, not the old names",
      "constructicon_create_department" in out.get("names", []) and "constructicon_create_hobby" not in out.get("names", [])
      and sorted(out.get("names", [])) == expect)
check("MCP process: an old name still answers (hidden alias)", out.get("alias_call_error") is False, out)
check("MCP process: parameter names stay 'hobby'", "hobby" in out.get("params", []), out.get("params"))
check("MCP process: the listed description says departments", "epartments" in out.get("desc", ""))
save({"hobby_label": "", "hobby_label_plural": ""})

# ---- 7. path resolution edge cases and reserved words ------------------------------------------------
print("--- 7. paths ---")
known = [("hobby", "hobbies"), ("team", "teams")]
D = hl.Label("Department", "Departments")
check("resolve: /departments rewrites to /hobbies", hobby_paths.resolve("/departments", D, known) == ("rewrite", "/hobbies"))
check("resolve: /departments/ (trailing slash) too", hobby_paths.resolve("/departments/", D, known) == ("rewrite", "/hobbies"))
check("resolve: /department/x rewrites", hobby_paths.resolve("/department/x", D, known) == ("rewrite", "/hobby/x"))
check("resolve: /department alone is left alone", hobby_paths.resolve("/department", D, known) == (None, None))
check("resolve: /departments/x is left alone", hobby_paths.resolve("/departments/x", D, known) == (None, None))
check("resolve: /hobby alone is left alone", hobby_paths.resolve("/hobby", D, known) == (None, None))
check("resolve: /teams redirects to /departments", hobby_paths.resolve("/teams", D, known) == ("redirect", "/departments"))
check("resolve: default label leaves /hobbies alone", hobby_paths.resolve("/hobbies", hl.DEFAULT, known) == (None, None))
check("resolve: /project/x untouched", hobby_paths.resolve("/project/x", D, known) == (None, None))
firsts = set()
for route in webapp.app.routes:
    path = getattr(route, "path", "")
    seg = path.strip("/").split("/")[0]
    if not seg.startswith("{"):
        firsts.add(seg)
missing = sorted(s for s in firsts if s not in hl.RESERVED_SLUGS and s not in ("hobby", "hobbies"))
check("every first segment of the route table is a reserved label slug", not missing, missing)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
