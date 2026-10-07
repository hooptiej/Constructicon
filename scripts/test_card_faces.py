#!/usr/bin/env python3
"""Self-contained check for #596: the card face overhaul (text box, status, stat, footer) and the
synopsis / flavor / write-up-lead plumbing behind it.

Throwaway SQLite DB and storage (scripts/_testenv.py), built by the real `db.init_db()`; the real
FastAPI app through TestClient and the MCP tool functions called directly. Covers:
  - markdown_render.lead / clamp: headings, italic editorial notes, production notes and lists are
    skipped, paragraphs never joined across a heading, the result is clamped; blank -> "";
  - card_rules.validate_card_text: leave / clear / clean / too long / nothing passed;
  - the text box resolution order: synopsis, else the cached write-up lead, else the description,
    else empty; never whereabouts or provenance on any face (card, hobby, file);
  - set / clear / undo of synopsis + flavor through the web route AND the MCP tool, for a card and
    for a hobby; each write is one change-log row and its undo leaves the whole DB as it was;
  - the write-up lead cache: refreshed by items.update (the body edit and its undo restore both),
    by cards.update(writeup_slug=...), cleared by deleting the write-up (undo restores it), and
    filled by the writeup_lead_596 migration;
  - the per-kind zones: action "Applies to: <parent>." + "Inside · <parent>", typed links "Built for:
    X.", family footer "<CODE> · family card" + member count, hobby project list + group-code line
    + "Hobby · active" + "N projects" + "<CODE> · group card", file "Stacked · <card>";
  - the item-grid payload carries `stacked` and no provenance; pages render 200 with no
    provenance on any face; scripts/check_layering.py passes.
No server needed:

    python scripts/test_card_faces.py

Exits 1 if any check fails.
"""

import io
import json
import os
import re
import sqlite3
import subprocess
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("card-faces-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, card_rules, cards, db, hobbies, ingest, items, markdown_render, paths  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None  # no background threads in a unit check

FAILS = []
PROVENANCE_WORDS = ("Created", "Hooptie", "Have it", "On the bench", "Documented", "Found")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code
    return None


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def snapshot():
    """Every table but the audit/change log, every non-BLOB column."""
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                              "AND name NOT IN ('audit_log') ORDER BY name"):
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({t})") if (r["type"] or "").upper() != "BLOB"]
            out[t] = sorted(json.dumps(dict(r), sort_keys=True, default=str)
                            for r in c.execute(f"SELECT {', '.join(cols)} FROM {t}"))
        return out
    finally:
        c.close()


def diff(a, b):
    return {t: (len(a[t]), len(b.get(t, []))) for t in a if a[t] != b.get(t)}


def ops_of(batch_id):
    return [(r["op"], r["actor"]) for r in q("SELECT op, actor FROM audit_log WHERE batch_id = ? AND op IS NOT NULL "
                                             "ORDER BY id", batch_id)]


def undo_ok(name, batch_id, before):
    cards.undo(batch_id)
    after = snapshot()
    check(f"undo {name}: database exactly as before", after == before, diff(before, after))


def mk(slug, filename=None):
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 10)).save(buf, "PNG")
    (paths.storage_dir() / sf).write_bytes(buf.getvalue())
    db.insert_upload(slug, filename or f"{slug}.png", sf, "tester", media_type="image")
    return slug


def face_strings(face):
    """Every string a face shows (zones only), for the no-provenance checks."""
    keys = ("title", "dates", "type_line", "rel_lines", "text", "notes", "flavor", "zone", "stat", "foot")
    out = []
    for k in keys:
        v = face.get(k)
        out += v if isinstance(v, list) else [v or ""]
    return " | ".join(str(x) for x in out)


def no_provenance(name, face):
    s = face_strings(face)
    check(f"{name}: no provenance / credit / whereabouts on the face", not any(w in s for w in PROVENANCE_WORDS), s)


# ---- 1. pure helpers -------------------------------------------------------------------------
BODY = """# Clod-a-Pede -- the build

*The owner's own account, recorded 2026-10-01 (written up by Claude from conversation).*

Reconstructed by Claude from the project's own files, not from memory.

Clod-a-Pede started as **three RC trucks** and ended up as one: two Clodbusters for the
running gear, a [King Hauler](https://example.com) for the looks.

## The chassis

Stretched to 39 inches.

- a list item that is not a paragraph
"""
lead = markdown_render.lead(BODY, 320)
check("lead: first real paragraph, Markdown stripped (no #, **, link syntax)",
      lead.startswith("Clod-a-Pede started as three RC trucks") and "**" not in lead and "](" not in lead, lead)
check("lead: italic editorial note and production note skipped",
      "owner's own account" not in lead and "Reconstructed" not in lead, lead)
check("lead: never crosses a heading", "Stretched" not in lead and "list item" not in lead, lead)
check("lead: blank / heading-only body -> ''", markdown_render.lead("", 320) == "" and markdown_render.lead("# Only a title\n", 320) == "")
long = markdown_render.lead("word " * 400, 320)
check("lead: clamped to the limit with an ellipsis", len(long) <= 321 and long.endswith("…"), len(long))
check("clamp keeps line breaks, leaves short text alone",
      markdown_render.clamp("a\nb", 10) == "a\nb" and markdown_render.clamp("short", 10) == "short")

check("validate_card_text: ... leaves a field out", card_rules.validate_card_text(synopsis="x") == {"synopsis": "x"})
check("validate_card_text: blank / None clear",
      card_rules.validate_card_text(synopsis="  ", flavor=None) == {"synopsis": None, "flavor": None})
check("validate_card_text: whitespace cleaned, synopsis keeps one paragraph per line",
      card_rules.validate_card_text(synopsis=" a  b \n\n c ", flavor=" one \n line ")
      == {"synopsis": "a b\nc", "flavor": "one line"})
check("validate_card_text: too long / nothing -> bad_card_text",
      code_of(card_rules.validate_card_text, synopsis="x" * (card_rules.SYNOPSIS_MAX + 1)) == "bad_card_text"
      and code_of(card_rules.validate_card_text, flavor="x" * (card_rules.FLAVOR_MAX + 1)) == "bad_card_text"
      and code_of(card_rules.validate_card_text) == "bad_card_text")

# ---- 2. fixture: a hobby, a thing with a write-up, an action part, a link, a family, files ----
db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
from web.shapes import _card_items  # noqa: E402
from mcp_server import server  # noqa: E402

client = TestClient(webapp.app)

H = hobbies.create("R/C Adventures").data["hobby"]
hobbies.set_group_code(H["id"], "RCA")
clod = cards.create("Clod-a-Pede", description="Three RC trucks became one.", kind="thing").data["card"]
lua = cards.create("Clodapede Lua", description="Radio scripts.", kind="action", parent=clod["id"]).data["card"]
canopy = cards.create("Canopy", kind="thing").data["card"]
fam = cards.create("AlienWhoop", kind="family").data["card"]
for c in (clod, lua, fam):
    hobbies.add_card(H["id"], c["id"])
cards.link(canopy["id"], clod["id"], "built_for")
cards.add_to_family(fam["id"], canopy["id"])
cards.set_whereabouts(clod["id"], "have_it", "On the bench")
cards.set_provenance(clod["id"], "created", "Hooptie J")
photo = mk("clodphoto", "IMG_8903.jpeg")
from core import membership  # noqa: E402
membership.add_files(clod["id"], [photo], **membership.UI_EFFECTS)
clod = db.get_project(clod["id"])
writeup = clod["writeup_slug"]

# ---- 3. resolution order ---------------------------------------------------------------------
f = cards.card_face(clod["id"])
check("no synopsis, blank write-up: the face shows the description",
      f["text_source"] == "description" and f["text"] == "Three RC trucks became one.", f["text_source"])
no_provenance("thing face", f)
check("thing face: status box = the stage, stat = files stacked + nested, footer = <CODE> · <hobby>",
      f["zone"] == "In progress" and f["stat"] == "1 stacked · 1 nested" and f["foot"] == "RCA · R/C Adventures",
      (f["zone"], f["stat"], f["foot"]))
check("thing face: provenance / facts keys are gone", "provenance" not in f and "facts" not in f, sorted(f))

before = snapshot()
res = items.update(writeup, type_metadata={"body": BODY})
row = db.get_project(clod["id"])
check("write-up save refreshes the cached lead in the same change-log row",
      row["writeup_lead"] == lead and ops_of(res.batch_id) == [("item_update", "owner-ui")],
      (row["writeup_lead"], ops_of(res.batch_id)))
f = cards.card_face(clod["id"])
check("with a write-up: the face shows its lead", f["text_source"] == "writeup" and f["text"] == lead)
undo_ok("write-up body edit (body + lead)", res.batch_id, before)
check("undo restored the empty lead", db.get_project(clod["id"])["writeup_lead"] is None)
items.update(writeup, type_metadata={"body": BODY})

# web route: set synopsis + flavor
before = snapshot()
r = client.post(f"/api/projects/{clod['id']}/text", data={"synopsis": "Two Clodbusters and a King Hauler.\nSteers both axles.",
                                                        "flavor": "Very nearly named Clod-a-Pete."})
body = r.json()
check("web set text: 200, synopsis + flavor stored", r.status_code == 200 and body.get("synopsis", "").startswith("Two")
      and body.get("flavor") == "Very nearly named Clod-a-Pete." and body.get("face_text_source") == "synopsis", body)
check("web set text: one change-log row, actor owner-ui", ops_of(body.get("batch_id")) == [("set_card_text", "owner-ui")],
      ops_of(body.get("batch_id")))
f = cards.card_face(clod["id"])
check("face: synopsis first (one paragraph per line), flavor set",
      f["text"] == "Two Clodbusters and a King Hauler.\nSteers both axles." and f["flavor"] == "Very nearly named Clod-a-Pete.")
html = client.get(f"/project/{clod['slug']}").text
head = html[html.find('class="detail-header-card"'):]
head = head[:head.find("</a>")]
check("project page: header card shows synopsis + flavor, no provenance / whereabouts",
      "Steers both axles." in head and "cx-flavor" in head and "Very nearly named" in head
      and not any(w in head for w in ("Created", "Hooptie J", "Have it", "On the bench")), head[-600:])
check("project page: ABOUT group lists the synopsis", 'data-group="about"' in html and "Two Clodbusters and a King Hauler." in html)
r = client.post(f"/api/projects/{clod['id']}/text", data={"flavor": "Same synopsis, new flavor."})
check("web set text: an omitted field is left alone",
      r.status_code == 200 and r.json()["synopsis"].startswith("Two") and r.json()["flavor"] == "Same synopsis, new flavor.")
cards.undo(r.json()["batch_id"])
undo_ok("web set text", body["batch_id"], before)
r = client.post(f"/api/projects/{clod['id']}/text", data={"synopsis": "x" * 700})
check("web set text: too long -> 422 bad_card_text, nothing written",
      r.status_code == 422 and r.json()["error"]["code"] == "bad_card_text" and snapshot() == before, r.text[:200])

# MCP: set, then clear back to the lead
out = server.constructicon_set_card_text(card=clod["slug"], synopsis="An MCP synopsis.", flavor="An MCP flavor.")
check("MCP set text: ok, actor mcp", out.get("ok") and ops_of(out["batch_id"]) == [("set_card_text", "mcp")], out)
f = cards.card_face(clod["id"])
check("MCP set text: the face follows", f["text"] == "An MCP synopsis." and f["flavor"] == "An MCP flavor.")
out2 = server.constructicon_set_card_text(card=clod["slug"], clear_synopsis=True, clear_flavor=True)
f = cards.card_face(clod["id"])
check("MCP clear: face falls back to the write-up lead, no flavor",
      out2.get("ok") and f["text_source"] == "writeup" and f["flavor"] == "", (out2, f["text_source"]))
cards.undo(out2["batch_id"])
check("undo of the clear restores the MCP synopsis", cards.card_face(clod["id"])["text"] == "An MCP synopsis.")
cards.undo(out["batch_id"])
check("undo of the set: back to the lead", cards.card_face(clod["id"])["text_source"] == "writeup")
bad = server.constructicon_set_card_text(card=clod["slug"], hobby=H["slug"], synopsis="x")
check("MCP: card AND hobby -> error", bad.get("ok") is False, bad)
bad = server.constructicon_set_card_text(card=clod["slug"], flavor="x" * 200)
check("MCP: too long -> bad_card_text", bad.get("ok") is False and bad["error"]["code"] == "bad_card_text", bad)
gp = server.constructicon_get_project(clod["slug"])
check("MCP get_project exposes synopsis / flavor / writeup_lead / face_text_source",
      gp.get("writeup_lead") == lead and gp.get("face_text_source") == "writeup" and "synopsis" in gp and "flavor" in gp)

# write-up slug cleared -> description; write-up deleted -> lead cleared, undo restores
before = snapshot()
res = cards.update(clod["id"], writeup_slug=None)
f = cards.card_face(clod["id"])
check("writeup_slug cleared: lead cleared, face shows the description",
      db.get_project(clod["id"])["writeup_lead"] is None and f["text_source"] == "description")
undo_ok("clearing the write-up", res.batch_id, before)
res = items.delete([writeup])
check("deleting the write-up clears its card's lead", db.get_project(clod["id"])["writeup_lead"] is None)
undo_ok("deleting the write-up", res.batch_id, before)
check("undo restored the lead", db.get_project(clod["id"])["writeup_lead"] == lead)

# migration backfill
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("UPDATE projects SET writeup_lead = NULL")
c.commit()
c.close()
with db.transaction():
    db._mig_writeup_lead_596()
check("writeup_lead_596 migration fills the cache from the write-up", db.get_project(clod["id"])["writeup_lead"] == lead)
check("writeup_lead_596 is registered", "writeup_lead_596" in [n for n, _ in db.MIGRATIONS])

# ---- 4. per-kind zones --------------------------------------------------------------------------
f = cards.card_face(lua["id"])
check("action nested in a thing: 'Applies to: <parent>.' first, status 'Inside · <parent>'",
      f["rel_lines"][:1] == ["Applies to: Clod-a-Pede."] and f["zone"] == "Inside · Clod-a-Pede",
      (f["rel_lines"], f["zone"]))
check("action: description in the text box", f["text"] == "Radio scripts.")
f = cards.card_face(canopy["id"])
check("typed link: 'Built for: X.'", f["rel_lines"] == ["Built for: Clod-a-Pede."], f["rel_lines"])
check("thing with no hobby, files or text: empty footer / stat / text",
      f["foot"] == "" and f["stat"] == "" and f["text"] == "", (f["foot"], f["stat"]))
f = cards.card_face(fam["id"])
check("family: '<CODE> · family card', member count", f["foot"] == "RCA · family card" and f["stat"] == "1 member",
      (f["foot"], f["stat"]))
f = cards.card_face(clod["id"])
check("incoming link ('Made for this') is not a face line", not any("Made for" in x for x in f["rel_lines"]), f["rel_lines"])
check("stat counts nested cards too", f["stat"] == "1 stacked · 1 nested", f["stat"])

# hobby face + hobby text
hrow = db.get_hobby(H["id"])
projects = db.list_projects_for_hobby(H["id"])
hf = cards.hobby_card_face(hrow, projects, {p["id"]: db.list_project_items(p["id"]) for p in projects})
check("hobby face: project list + group-code line, zones per the mockup",
      hf["notes"][0].startswith("Projects: ") and "Clod-a-Pede" in hf["notes"][0]
      and hf["notes"][1] == "Every card in this hobby carries its group code, RCA."
      and hf["zone"] == "Hobby · active" and hf["stat"] == "3 projects" and hf["foot"] == "RCA · group card"
      and hf["type_line"] == "Hobby · Active", (hf["notes"], hf["zone"], hf["stat"], hf["foot"]))
no_provenance("hobby face", hf)
many = cards._project_list_line([f"Project number {i}" for i in range(30)])
check("hobby project list clamps with '+N more'", re.search(r", \+\d+ more\.$", many) is not None
      and len(many) < cards.FACE_PROJECT_LIST_MAX + 40, many)
before = snapshot()
r = client.post(f"/api/hobby/{H['slug']}/text", data={"synopsis": "Everything with wheels.", "flavor": "Mostly Tamiya."})
check("web hobby text: 200, one change-log row", r.status_code == 200
      and ops_of(r.json()["batch_id"]) == [("hobby_text", "owner-ui")], r.text[:300])
hf = cards.hobby_card_face(db.get_hobby(H["id"]), projects, {p["id"]: [] for p in projects})
check("hobby face: synopsis first, then the list; flavor", hf["text"] == "Everything with wheels."
      and hf["flavor"] == "Mostly Tamiya." and hf["notes"][0].startswith("Projects: "))
page = client.get(f"/hobby/{H['slug']}").text
check("hobby page: 200, card shows the synopsis and the group-code line",
      "Everything with wheels." in page and "carries its group code, RCA." in page)
undo_ok("web hobby text (settings row inserted)", r.json()["batch_id"], before)
out = server.constructicon_set_card_text(hobby=H["slug"], synopsis="Via MCP.")
check("MCP hobby text", out.get("ok") and hobbies.text_fields(db.get_hobby(H["id"]))["synopsis"] == "Via MCP.", out)
cards.undo(out["batch_id"])
check("MCP hobby text undone", snapshot() == before)

# file faces
pile = cards.file_stacks(clod["id"], thumb_fn=lambda r: None)
fc = next(c for p in pile for c in p["cards"] if c["slug"] == photo)
check("file face (pile): 'Stacked · <card>', footer names the card, stat = extension",
      fc["zone"] == "Stacked · Clod-a-Pede" and fc["foot"] == "Clod-a-Pede" and fc["stat"] == "JPEG", fc)
no_provenance("file face", fc)
payload = _card_items([db.get_by_slug(photo)])[0]
check("item-grid payload: `stacked` in, provenance out", payload.get("stacked") == "Clod-a-Pede" and "provenance" not in payload,
      sorted(payload))

# pages
for path in ("/", "/unfiled", f"/project/{lua['slug']}", f"/project/{fam['slug']}", f"/hobby/{H['slug']}"):
    r = client.get(path)
    faces = re.findall(r'<a class="cx .*?</a>', r.text, flags=re.S)
    leaked = [x for x in faces if "cx-prov" in x or "Hooptie J" in x or "On the bench" in x]
    check(f"GET {path}: 200, no provenance on any of its {len(faces)} faces", r.status_code == 200 and not leaked,
          (r.status_code, leaked[:1]))

r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_layering.py")], capture_output=True, text=True)
check("check_layering passes", r.returncode == 0, r.stdout[-300:])

ctx.__exit__(None, None, None)
print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
