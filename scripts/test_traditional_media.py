#!/usr/bin/env python3
"""Self-contained check for physical-piece fields on items (#425).

Throwaway SQLite DB (CONSTRUCTICON_DB_PATH is set before core is imported), real `db.init_db()`,
the real FastAPI app through TestClient, no server and no network:

    python scripts/test_traditional_media.py

Covers: the fields save through POST /api/image/{slug} and render in the PHYSICAL PIECE group,
escaping of hostile values, validation of date made, medium suggestions (most used first),
the group's visibility rule (a per-hobby setting since #562, with undo), the in-app capture
guide, and the date-made effect on the item's effective date.
"""
import os
import sys
import tempfile
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("tradmedia-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # native cairo is not needed here

from core import db, hobbies, physical_piece, timeline  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths

FAILS = []
SECTION = 'aria-labelledby="dp-h-physical"'  # the rendered group (the page JS also mentions the name)


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()


def make_item(slug, **kw):
    db.insert_content(slug, "tester", "youtube", external_url="https://example.com/" + slug,
                      content_description=kw.pop("title", slug), **kw)
    return slug


# ---- pure helpers -------------------------------------------------------------------------
check("parse year", physical_piece.parse_date_made("2009") == (2009, 7, 1))
check("parse year-month", physical_piece.parse_date_made("2009-06") == (2009, 6, 15))
check("parse full date", physical_piece.parse_date_made("2009-06-14") == (2009, 6, 14))
check("reject junk", all(physical_piece.parse_date_made(v) is None
                         for v in ("", "june 2009", "2009-13", "2009-02-30", "99", None, 2009)))
try:
    physical_piece.clean_fields({"date_made": "last tuesday"})
    check("clean_fields rejects bad date", False)
except ValueError:
    check("clean_fields rejects bad date", True)
c = physical_piece.clean_fields({"medium": "  ink   on paper ", "dimensions": "x" * 500, "other": 1, "date_made": ""})
check("clean trims and collapses", c["medium"] == "ink on paper")
check("clean caps length", len(c["dimensions"]) == physical_piece.MAX_LEN)
check("clean passes other keys and keeps empty", c["other"] == 1 and c["date_made"] == "")

# ---- date made feeds the effective date ----------------------------------------------------
base = {"timestamp": 1_700_000_000.0, "content_date": 1_600_000_000.0, "source_modified_at": 1_500_000_000.0}
made = physical_piece.date_made_epoch({"date_made": "2009-06-14"})
check("effective date: date made beats content_date", timeline.resolve_item_date({**base, "type_metadata": {"date_made": "2009-06-14"}}) == made)
check("effective date: unset leaves content_date", timeline.resolve_item_date({**base, "type_metadata": {}}) == base["content_date"])
check("effective date: bad value ignored", timeline.resolve_item_date({**base, "type_metadata": {"date_made": "soon"}}) == base["content_date"])
check("effective date: hand-set timeline date still wins",
      timeline.resolve_item_date({**base, "display_date_override": 42.0, "type_metadata": {"date_made": "2009"}}) == 42.0)
check("effective date: no type_metadata key at all", timeline.resolve_item_date(dict(base)) == base["content_date"])
check("has_real_date counts date made", timeline.has_real_date({"timestamp": 1.0, "type_metadata": {"date_made": "2009"}}))
check("has_real_date false without", not timeline.has_real_date({"timestamp": 1.0, "type_metadata": {}}))
check("date made is local noon of that day",
      timeline.epoch_to_local(made).strftime("%Y-%m-%d %H:%M") == "2009-06-14 12:00")

# ---- medium suggestions --------------------------------------------------------------------
for i, m in enumerate(["ink on paper", "ink on paper", "ink  on paper", "watercolor", "watercolor", "acrylic", "<b>x</b>\"&'", "", "   "]):
    make_item(f"m{i}", type_metadata={"medium": m})
make_item("nomedium", type_metadata={"other": 1})
sugg = physical_piece.medium_suggestions(db)
check("suggestions: most used first (whitespace-collapsed)", sugg[:2] == ["ink on paper", "watercolor"] and set(sugg[2:]) == {"acrylic", "<b>x</b>\"&'"}, sugg)
check("suggestions: distinct and non-empty", len(sugg) == len(set(sugg)) and all(s.strip() for s in sugg))
check("suggestions: capped", len(physical_piece.medium_suggestions(db, limit=2)) == 2)

# ---- HTTP: save + render + escape ----------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402

client = _testenv.client(webapp.app)
slug = make_item("piece1", title="A dragon")
page = client.get(f"/object/{slug}")
check("page renders", page.status_code == 200, page.status_code)
check("hidden when nothing set and not in the hobby", SECTION not in page.text)

import json  # noqa: E402


def save(s, **fields):
    return client.post(f"/api/image/{s}", data={"type_metadata": json.dumps(fields)})


hostile = '<script>alert(1)</script>"&\''
r = save(slug, medium="ink on paper", dimensions="8.5 x 11 in", date_made="2009-06", original_location=hostile)
check("save ok", r.status_code == 200, (r.status_code, r.text[:200]))
tm = db.get_by_slug(slug)["type_metadata"]
check("fields stored in type_metadata", tm["medium"] == "ink on paper" and tm["dimensions"] == "8.5 x 11 in"
      and tm["date_made"] == "2009-06" and tm["original_location"] == hostile.strip())
page = client.get(f"/object/{slug}")
html = page.text
check("group shows once any field is set", SECTION in html and "PHYSICAL PIECE" in html)
check("values rendered", "ink on paper" in html and "8.5 x 11 in" in html and "2009-06" in html)
check("hostile value escaped everywhere", "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html)
check("medium input wired to datalist", 'list="physical-medium-suggest"' in html and 'id="physical-medium-suggest"' in html)
check("datalist carries earlier values, escaped", '<option value="ink on paper">' in html and "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html)
check("effective date follows date made", "Jun 15, 2009" in html or "June 15, 2009" in html or "2009" in html.split("Effective date")[1][:200])
check("save does not clobber other keys", save(slug, dimensions="A4").status_code == 200
      and db.get_by_slug(slug)["type_metadata"]["medium"] == "ink on paper")
check("clearing a field", save(slug, original_location="").status_code == 200
      and db.get_by_slug(slug)["type_metadata"]["original_location"] == "")

bad = save(slug, date_made="last tuesday")
check("bad date made is a 400 with a message", bad.status_code == 400 and "date_made" in bad.text, (bad.status_code, bad.text[:120]))
check("bad save changed nothing", db.get_by_slug(slug)["type_metadata"]["date_made"] == "2009-06")
check("non-text value is a 400", save(slug, medium=["a"]).status_code == 400)
check("page's edit form has all four inputs",
      all(f'id="physical-{k}"' in html for k in ("medium", "dimensions", "date_made", "original_location")))
check("JS registers the group", "DP.register('physical'" in html)

# ---- visibility via a hobby's "shows physical-piece fields" setting (#562) -----------------
other = make_item("piece2", title="Empty piece")
pid = db._create_project("Drawings")["id"]
db._add_item_to_project(pid, other)
check("not shown for a project outside the hobby", SECTION not in client.get(f"/object/{other}").text)
tag_id = hobbies.create("Traditional Media").data["hobby"]["id"]
hobbies.add_card(tag_id, pid)
check("#562: the hobby's NAME no longer switches it on", SECTION not in client.get(f"/object/{other}").text)
r = client.post(f"/api/hobby/{tag_id}/physical-piece", data={"enabled": "true"})
check("the setting saves (editor route)", r.status_code == 200 and r.json()["shows_physical_piece"] is True
      and r.json()["batch_id"], (r.status_code, r.text[:200]))
html2 = client.get(f"/object/{other}").text
check("shown for an item in a flagged hobby's project, all four blank",
      SECTION in html2 and html2.count("&mdash;") >= 4)
again = client.post(f"/api/hobby/{tag_id}/physical-piece", data={"enabled": "true"})
check("setting it again writes nothing", again.status_code == 200 and again.json()["batch_id"] is None)

# ---- the hobby page: SETTINGS group + the in-app capture guide link -------------------------
hp = client.get("/hobby/traditional-media")
check("hobby page renders", hp.status_code == 200, hp.status_code)
check("hobby page links the in-app capture guide", 'href="/guides/capture-physical-piece"' in hp.text
      and "github.com" not in hp.text)
check("hobby page shows the setting", 'id="hobby-physical-piece-input"' in hp.text and "Shown on its items" in hp.text)
guide = client.get("/guides/capture-physical-piece")
check("the guide is served by the app", guide.status_code == 200 and "<h1>Capturing a physical piece</h1>" in guide.text,
      guide.status_code)
check("an unknown guide is a 404", client.get("/guides/nope").status_code == 404)
other_hobby = hobbies.create("Model Railways").data["hobby"]
hp2 = client.get(f"/hobby/{other_hobby['slug']}").text
check("an unflagged hobby has no guide link", "/guides/capture-physical-piece" not in hp2 and "Not shown" in hp2)

# ---- undo switches it back off ---------------------------------------------------------------
from core import cards  # noqa: E402
cards.undo(r.json()["batch_id"])
check("undo turns the setting off again", not hobbies.shows_physical_piece(tag_id)
      and SECTION not in client.get(f"/object/{other}").text)

print("FAILED: %s" % FAILS if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
