#!/usr/bin/env python3
"""Self-contained check for #542: one answer to "what is this item called?" (core/item_title.py).

Throwaway SQLite DB and storage (scripts/_testenv.py), built by the real `db.init_db()`; the real
FastAPI app through TestClient, the MCP tool functions called directly and the real static export.
Covers:
  - the order: display_name -> content_description -> filename -> slug, and `description` (the
    uploader's note) NEVER a title, on a renamed item, a captioned item, a note-only item and a
    bare one;
  - a renamed item shows its display_name on every converted surface: the item card payload, the
    search result shape, the object page, the project grid, the project timeline, a pile, the blog
    entry payload, the MCP get / search / project items, the revision and curation-queue labels and
    the static export (alt text);
  - an item with only an uploader note never shows the note as its title on any of them;
  - a static check: no hand-rolled `display_name or filename`-style chain outside
    core/item_title.py (python, Jinja and JS under core/, web/ and mcp_server/).
No server needed:

    python scripts/test_item_title_542.py

Exits 1 if any check fails.
"""

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("item-title-542-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent
sys.path.insert(0, str(ROOT))

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, blog, cards, curation_queue, db, ingest, item_title, items, membership, paths, revisions, site_export  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths

ingest.run_in_thread = lambda fn, *a: None  # no background threads in a unit check

FAILS = []
NOTE = "Uploader note: migrated from the old Wix site, 12 files"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def mk(slug, filename, description=""):
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 10)).save(buf, "PNG")
    (paths.storage_dir() / sf).write_bytes(buf.getvalue())
    db.insert_upload(slug, filename, sf, "tester", description=description, media_type="image")
    return slug


# ---- 1. the function itself ------------------------------------------------------------------
base = {"slug": "s1", "filename": "f.png", "content_description": "cap", "display_name": "Name", "description": NOTE}
check("title_of: display_name wins", item_title.title_of(base) == "Name")
check("title_of: then content_description", item_title.title_of({**base, "display_name": None}) == "cap")
check("title_of: then filename", item_title.title_of({**base, "display_name": "", "content_description": None}) == "f.png")
check("title_of: then slug", item_title.title_of({**base, "display_name": None, "content_description": "",
                                                  "filename": None}) == "s1")
check("title_of: description is never a title",
      item_title.title_of({"slug": "s1", "filename": None, "description": NOTE}) == "s1")
check("items.py no longer carries its own copy", not hasattr(items, "title_of"))

# ---- 2. fixture ------------------------------------------------------------------------------
db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

from web import app as webapp  # noqa: E402
from web.shapes import _card_items, _to_content_public, _to_object_detail, _to_public  # noqa: E402
from mcp_server import server  # noqa: E402

client = _testenv.client(webapp.app)

RENAMED = mk("renamedone", "IMG_0001.png", description=NOTE)
CAPTIONED = mk("captioned1", "IMG_0002.png", description=NOTE)
NOTEONLY = mk("noteonly01", "IMG_0003.png", description=NOTE)
items.update(RENAMED, display_name="Tension Biped, one leg standing", content_description="A walker on a bench")
items.update(CAPTIONED, content_description="Caption wins over filename")
NAME = "Tension Biped, one leg standing"

proj = cards.create("Biped", kind="thing").data["card"]
membership.add_files(proj["id"], [RENAMED, CAPTIONED, NOTEONLY], **membership.UI_EFFECTS)
proj = db.get_project(proj["id"])
rows = {s: db.get_by_slug(s) for s in (RENAMED, CAPTIONED, NOTEONLY)}

# ---- 3. every surface: renamed item ----------------------------------------------------------
card = _card_items([rows[RENAMED]])[0]
check("item card payload: display_name", card["display_name"] == NAME, card["display_name"])
check("item shapes: _to_public / _to_object_detail / _to_content_public",
      _to_public(rows[RENAMED])["display_name"] == NAME and _to_object_detail(rows[RENAMED])["display_name"] == NAME
      and _to_content_public(rows[RENAMED])["title"] == NAME)
r = client.get("/api/search", params={"query": "IMG_0001"})
hit = next((x for x in r.json() if x["slug"] == RENAMED), None) if r.status_code == 200 else None
check("search result shape: display_name", hit is not None and hit["display_name"] == NAME, (r.status_code, hit))
r = client.get(f"/api/image/{RENAMED}")
check("GET /api/image/<slug>: display_name", r.status_code == 200 and r.json()["display_name"] == NAME, r.status_code)
r = client.get(f"/object/{RENAMED}")
check("object page: <title> and heading", r.status_code == 200 and f"<title>{NAME}" in r.text
      and NAME in r.text.split('id="object-title-text"')[1][:200], r.status_code)
r = client.get(f"/project/{proj['slug']}")
page = r.text
check("project page: 200", r.status_code == 200, r.status_code)
check("project grid: the rename shows", f'"title": "{NAME}"' in page or NAME in page)
tl = re.findall(r'"title":\s*"([^"]*)"', page)
check("project timeline: the rename is a marker title", NAME in tl, tl[:8])
pile_titles = [c["title"] for p in cards.file_stacks(proj["id"], thumb_fn=lambda r: None) for c in p["cards"]]
check("pile (stack face): the rename", NAME in pile_titles, pile_titles)
entry = blog.create("Biped post").data["entry"]
blog.set_items(entry["id"], [(RENAMED, ""), (NOTEONLY, "")])
r = client.get(f"/api/blog-entries/{entry['slug']}")
bt = {i["slug"]: i["title"] for i in r.json()["items"]} if r.status_code == 200 else {}
check("blog entry payload: the rename", bt.get(RENAMED) == NAME, (r.status_code, bt))
check("MCP get", server.constructicon_get(RENAMED)["display_name"] == NAME)
check("MCP search", any(x["slug"] == RENAMED and x["display_name"] == NAME for x in server.constructicon_search(query="IMG_0001")))
check("MCP get_project items", any(i["slug"] == RENAMED and i["display_name"] == NAME
                                   for i in server.constructicon_get_project(proj["slug"])["items"]))
check("revision label", revisions._display(rows[RENAMED]) == NAME)
q = curation_queue._file_question_item({"id": 1, "row": rows[RENAMED], "kind": "item_supersedes", "options": []})
check("curation queue question: file_title", q["file_title"] == NAME, q["file_title"])

out = TMP / "site" if isinstance(TMP, Path) else Path(str(TMP)) / "site"
res = site_export.build_site({"project_slugs": [proj["slug"]], "blog_entry_slugs": [entry["slug"]]}, out_dir=out)
proj_html = (out / "projects" / f"{proj['slug']}.html").read_text()
blog_html = next((out / "blog").glob("*.html"), None)
check("static export, project page: alt text is the rename", f'alt="{NAME}"' in proj_html, res.get("warnings"))
check("static export, blog page: alt text is the rename", blog_html is not None and f'alt="{NAME}"' in blog_html.read_text())

# ---- 4. caption and filename steps, and the note never a title -------------------------------
check("captioned item: caption beats filename on card and project grid",
      _card_items([rows[CAPTIONED]])[0]["display_name"] == "Caption wins over filename"
      and _to_content_public(rows[CAPTIONED])["title"] == "Caption wins over filename")
n = rows[NOTEONLY]
surfaces = {
    "card": _card_items([n])[0]["display_name"],
    "_to_public": _to_public(n)["display_name"],
    "_to_object_detail": _to_object_detail(n)["display_name"],
    "_to_content_public (project grid)": _to_content_public(n)["title"],
    "pile": next(c["title"] for p in cards.file_stacks(proj["id"], thumb_fn=lambda r: None) for c in p["cards"] if c["slug"] == NOTEONLY),
    "MCP get": server.constructicon_get(NOTEONLY)["display_name"],
    "blog entry": bt.get(NOTEONLY),
    "revision label": revisions._display(n),
}
for where, got in surfaces.items():
    check(f"note-only item, {where}: title is the filename, never the note", got == "IMG_0003.png", got)
check("note-only item, project page timeline: the note is not a title",
      NOTE not in [t for t in re.findall(r'"title":\s*"([^"]*)"', client.get(f"/project/{proj['slug']}").text)])
check("note-only item, static export: the note is not the alt text", f'alt="{NOTE}"' not in proj_html)

# ---- 5. static check: no hand-rolled chain outside the canonical module ----------------------
OPERAND = r"[\w.]*(?:\.get\(|\[)?[\"']?"   # row.get("filename"), row["filename"], item.filename, filename
CHAIN = re.compile(
    r"(?:display_name|content_description|filename|description)[\"'\]\)]*\s*(?:\bor\b|\|\|)\s*"
    + OPERAND + r"(?:display_name|content_description|filename|slug)\b")
SKIP = {"core/item_title.py"}
hits = []
for top in ("core", "web", "mcp_server"):
    for p in (ROOT / top).rglob("*"):
        if p.suffix not in (".py", ".html", ".js") or "__pycache__" in p.parts or p.name.endswith(".min.js"):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel in SKIP:
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "{#", "*")) or "stored_filename" in line and "display_name" not in line:
                continue
            if CHAIN.search(line):
                hits.append(f"{rel}:{i}: {stripped[:110]}")
check("no hand-rolled title chain outside core/item_title.py", not hits, "\n      ".join([""] + hits))
check("the chain detector still catches the old shapes (it can fail)",
      all(CHAIN.search(s) for s in (
          'x = row.get("display_name") or row["filename"] or row["slug"]',
          "{% set alt = item.display_name or item.filename or item.slug %}",
          "const l = o.display_name || o.content_description || o.slug;",
          'title = r.get("content_description") or r.get("description") or r.get("filename")',
          'name = c["display_name"] or c["filename"]')))

r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_layering.py")], capture_output=True, text=True)
check("check_layering passes", r.returncode == 0, r.stdout[-300:])

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
