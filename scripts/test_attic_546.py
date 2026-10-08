#!/usr/bin/env python3
"""Self-contained check for the imagerepo-attic cleanup (#546, code half).

Throwaway SQLite DB (CONSTRUCTICON_DB_PATH is set before core is imported), real `db.init_db()`,
the real FastAPI app through TestClient. No server, no network:

    python scripts/test_attic_546.py

Prints "all checks passed", or "FAILED: <names>" listing every failing check by name.
"""
import glob
import os
import re
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("attic546-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from core import card_rules, cards, curation_queue, curator, curator_needs, db, markdown_render, storage  # noqa: E402
from web import shapes  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def code_files():
    """Every Python, Jinja, JS and shell file a symbol could be called from (not this test, not docs)."""
    pats = ["core/**/*.py", "web/**/*.py", "web/**/*.html", "web/**/*.js", "mcp_server/**/*.py",
            "scripts/**/*.py", "scripts/**/*.js", "scripts/**/*.sh", "*.py"]
    out = set()
    for p in pats:
        out.update(glob.glob(os.path.join(ROOT, p), recursive=True))
    me = os.path.abspath(__file__)
    return sorted(f for f in out if os.path.abspath(f) != me and "__pycache__" not in f)


def refs(word, files):
    rx = re.compile(r"(?<![\w])" + re.escape(word) + r"(?![\w])")
    return [os.path.relpath(f, ROOT) for f in files if rx.search(open(f, encoding="utf-8", errors="ignore").read())]


db.init_db()
FILES = code_files()

# ---- 1. no "No project yet" client badge anywhere -------------------------------------------
cards_js = read("web", "static", "js", "cards.js")
check("cards.js has no 'No project yet' text", "No project yet" not in cards_js)
check("cards.js has no client badge or per-client colour helper",
      "client-badge" not in cards_js and "colorFor" not in cards_js and "CLIENT_COLORS" not in cards_js)
check("cards.js keeps the amber unfiled lamp", "Not filed into a project yet" in cards_js and "#BA7517" in cards_js)
check("no template, script or Python file renders 'No project yet'",
      not refs("No project yet", [f for f in FILES if not f.endswith("test_live_bugs_563.py")]),
      refs("No project yet", [f for f in FILES if not f.endswith("test_live_bugs_563.py")]))

db.insert_content("att1", "tester", "youtube", external_url="https://example.com/att1", content_description="Filed-ish item")
row = db.get_by_slug("att1")
public = shapes._to_public(row)
check("item payload has no 'No project yet' text", "No project yet" not in repr(public))
http = _testenv.client(__import__("web.app", fromlist=["app"]).app)
for path in ("/", "/unfiled"):
    r = http.get(path)
    check(f"GET {path} renders without 'No project yet'", r.status_code == 200 and "No project yet" not in r.text, f"{r.status_code}")
check("client badge CSS is gone", "client-badge" not in read("web", "static", "style.css")
      and "client-badge" not in read("web", "static", "css", "cards.css"))

# ---- 2. every deleted symbol is gone, and nothing refers to it ------------------------------
DELETED = {
    db: ["add_test_client", "count_table_rows", "count_project_links_by_type", "list_recent_posts", "list_project_ancestors"],
    cards: ["card_json", "STACK_UNDER"],
    card_rules: ["ACTIVITIES", "HOME_SOURCES", "CARD_PROVENANCE"],
    curation_queue: ["cached_counts", "invalidate_cache", "DEDUPE_PAIRS"],
    curator_needs: ["count_active_needs"],
    curator: ["_get_related_projects"],
    storage: ["normalize_avatar", "AVATAR_SIZE"],
    markdown_render: ["to_text"],
    shapes: ["_project_has_tag"],
}
for mod, names in DELETED.items():
    for n in names:
        check(f"{mod.__name__}.{n} is gone", not hasattr(mod, n))
        left = refs(n, FILES)
        check(f"no code refers to {n}", not left, left)
# names the issue lists that were already deleted before this PR: they must stay deleted
for n in ("unmark_hobby", "set_hobby_status", "delete_project"):
    check(f"db.{n} stays gone", not hasattr(db, n))
check("check_layering no longer allow-lists add_test_client", "add_test_client" not in read("scripts", "check_layering.py"))
# template context that nothing read
pages_py = read("web", "routes", "pages.py")
check("project page no longer builds the unused 'ancestors' / PROJECT_STATUSES context",
      "list_project_ancestors" not in pages_py and '"ancestors"' not in pages_py and "PROJECT_STATUSES" not in pages_py)
# the one dead-looking thing deliberately kept: a stale snooze call must still be refused, not silently become a dismiss
r = http.post("/api/curator/needs/dismiss", data={"nudge_key": "x", "snooze_until": "123"})
check("dismiss with snooze_until still answers 400 (kept on purpose)", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

# ---- 3. retired one-shot scripts are gone -----------------------------------------------------
for f in ("seed_test_data.py", "backfill_thumbnails.py"):
    check(f"{f} is deleted", not os.path.exists(os.path.join(ROOT, f)))
    left = refs(f[:-3], FILES) + [x for x in ("CLAUDE.md", "README.md") if f in read(x)]
    check(f"nothing refers to {f}", not left, left)

# ---- 4. redact confirmation tells the truth ---------------------------------------------------
detail = read("web", "templates", "object_detail.html")
m = re.search(r"confirm\('(Remove the image file\?[^']*)'\)", detail)
check("redact confirm text found", bool(m))
text = (m.group(1) if m else "").lower()
check("redact confirm no longer mentions tickets or clients", "ticket" not in text and "client" not in text, text)
check("redact confirm still says the file is held, not deleted", "held" in text and "recover" in text, text)

# ---- 5. dead CSS is gone; classes built from strings are kept ---------------------------------
css = re.sub(r"/\*.*?\*/", "", read("web", "static", "style.css") + read("web", "static", "css", "cards.css"), flags=re.S)  # comments stripped
DEAD_CSS = ["login-shell", "login-box", "online-avatar", "presence-dot", "avatar-menu", "avatar-wrap", "token-row",
            "token-reveal", "ticket-pill", "project-compact-card", "project-grid", "project-card-featured",
            "project-status-pill", "pending-decision-head", "curator-nudge-item", "curator-snooze-group",
            "gallery-thumb", "gallery-body", "breadcrumb-nav", "upload-layout", "drop-overlay", "client-dot",
            "client-name", "client-badge", "danger-zone", "cropper-stage", "audio-tag-row"]
for c in DEAD_CSS:
    check(f".{c} is gone from the stylesheets", not re.search(r"\." + re.escape(c) + r"(?![\w-])", css))
    users = [f for f in FILES if not f.endswith(("test_live_bugs_563.py", "check_layering.py"))]
    check(f"nothing uses the class {c}", not refs(c, users), refs(c, users))
for c in ("audit-log-status-success", "audit-log-status-error", "audit-log-status-other"):
    check(f".{c} kept (admin.html builds it from the HTTP status class)", "." + c in css)
check("admin.html still builds audit-log-status-${...}", "audit-log-status-${" in read("web", "templates", "admin.html"))
opens, closes = css.count("{"), css.count("}")
check("stylesheets still have balanced braces", opens == closes, f"{opens} open, {closes} close")

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
