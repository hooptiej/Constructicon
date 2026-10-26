#!/usr/bin/env python3
"""Self-contained check for #770 (grey = "I don't have it") and #773 (whereabouts `abandoned`).

Throwaway SQLite DB and storage (scripts/_testenv.py), the real `db.init_db()`, the real app through
TestClient and the MCP tool function called directly. Covers:
  - card_rules: `abandoned` is a whereabouts value with its label and help text (which tells it
    apart from stop_reason `abandoned`); WHEREABOUTS_GONE is exactly the owner's list; every value
    has a label and help; whereabouts_gone() never greys a kind whereabouts doesn't apply to;
  - validation: a bogus value is refused (bad_whereabouts) with a message listing the choices,
    `abandoned` included; abandoned can't be in_use; set via cards.set_whereabouts and via
    constructicon_update_project, and undone;
  - the face: `gone` for every gone value and the template's `cx-gone` class at full / small / mini,
    no reason tag (no whereabouts label on the face); have_it / not recorded keep color; a Stopped
    card gets `cx-stopped` + the "Stopped" tag and no grey; Stopped + gone = both; hobby and file
    faces never grey; an action / event / family with a stray whereabouts never greys;
  - pages: the home page and the card page render the grey class on a gone card; the card page's
    whereabouts picker offers "Abandoned (left it behind)" with its help.
No server needed:

    python scripts/test_gone_grey_770_773.py
"""

import os
import re
import sqlite3
import sys

import _testenv  # noqa: E402
TMP = _testenv.isolate("gone-grey-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import actor, card_rules, cards, db, ingest  # noqa: E402
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def err_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code, e.message
    return None, None


db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

from web.app import app  # noqa: E402
from web.common import templates  # noqa: E402
client = _testenv.client(app)

# ---- 1. rules --------------------------------------------------------------------------------
check("abandoned is a whereabouts value", "abandoned" in card_rules.WHEREABOUTS)
check("label 'Abandoned (left it behind)'", card_rules.WHEREABOUTS_LABELS["abandoned"] == "Abandoned (left it behind)")
help_ = card_rules.WHEREABOUTS_HELP["abandoned"]
check("help text tells it apart from stop_reason abandoned", "Stopped" in help_ and "project" in help_, help_)
check("every value has a label and help",
      all(k in card_rules.WHEREABOUTS_LABELS and card_rules.WHEREABOUTS_HELP.get(k) for k in card_rules.WHEREABOUTS))
check("gone list is the owner's", set(card_rules.WHEREABOUTS_GONE) ==
      {"sold", "gifted", "lost", "parted_out", "partial", "never_built", "abandoned"})
check("gone values are all real whereabouts", set(card_rules.WHEREABOUTS_GONE) <= set(card_rules.WHEREABOUTS))
check("have_it / None are not gone", not card_rules.whereabouts_gone("thing", "have_it")
      and not card_rules.whereabouts_gone("thing", None))
check("a kind without whereabouts never greys",
      not any(card_rules.whereabouts_gone(k, "sold") for k in ("action", "event", "family")))
check("thing / project / collection grey when sold",
      all(card_rules.whereabouts_gone(k, "sold") for k in ("thing", "project", "collection")))

# ---- 2. validation + writes ------------------------------------------------------------------
thing = cards.create("Talon Stand-in", kind="thing", stage="done").data["card"]
code, msg = err_of(cards.set_whereabouts, thing["id"], "towed_away")
check("bogus value refused: bad_whereabouts", code == "bad_whereabouts", code)
check("refusal names the choices, abandoned included", msg and "abandoned" in msg and "towed_away" in msg, msg)
check("refusal wrote nothing", db.get_project(thing["id"]).get("whereabouts") is None)
use = cards.create("Daily Driver", kind="thing", stage="in_use").data["card"]
code, _ = err_of(cards.set_whereabouts, use["id"], "abandoned")
check("an in-use card can't be abandoned", code == "bad_whereabouts", code)

r = cards.set_whereabouts(thing["id"], "abandoned", "Towed by the city")
check("set abandoned", db.get_project(thing["id"])["whereabouts"] == "abandoned")
check("label read back", cards.whereabouts_fields(db.get_project(thing["id"]))["whereabouts_label"]
      == "Abandoned (left it behind)")
cards.undo(r.batch_id)
check("undo clears it", db.get_project(thing["id"]).get("whereabouts") is None)

from mcp_server import server as mcp  # noqa: E402
res = mcp.constructicon_update_project(card=thing["slug"], fields={"whereabouts": "abandoned"})
check("MCP update_project sets abandoned", db.get_project(thing["id"])["whereabouts"] == "abandoned", res)
cards.undo(res["batch_id"])
check("MCP write undone", db.get_project(thing["id"]).get("whereabouts") is None)


# ---- 3. faces --------------------------------------------------------------------------------
def render(face, size):
    face = dict(face, cover_url=None)
    t = templates.env.from_string('{% from "_card.html" import card %}{{ card(c, s) }}')
    return t.render(c=face, s=size)


def classes(html):
    return re.search(r'<a class="([^"]*)"', html).group(1).split()


for value in card_rules.WHEREABOUTS_GONE:
    b = cards.set_whereabouts(thing["id"], value).batch_id
    f = cards.card_face(thing["id"])
    ok = f["gone"] and all("cx-gone" in classes(render(f, s)) for s in ("full", "small", "mini"))
    no_tag = all(card_rules.WHEREABOUTS_LABELS[value] not in render(f, s) for s in ("full", "small", "mini"))
    check(f"{value}: grey at full/small/mini, no reason tag", ok and no_tag)
    cards.undo(b)

b = cards.set_whereabouts(thing["id"], "have_it").batch_id
f = cards.card_face(thing["id"])
check("have_it keeps color", not f["gone"] and "cx-gone" not in classes(render(f, "full")))
cards.undo(b)
f = cards.card_face(thing["id"])
check("not recorded keeps color", not f["gone"] and "cx-gone" not in classes(render(f, "full")))

stopped = cards.create("Shelved Rover", kind="thing", stage="stopped", stop_reason="abandoned").data["card"]
cards.set_whereabouts(stopped["id"], "have_it")
f = cards.card_face(stopped["id"])
html = render(f, "full")
check("stopped + have_it: Stopped tag, no grey",
      f["stopped_tag"] and not f["gone"] and "cx-stopped" in classes(html) and "cx-gone" not in classes(html)
      and 'cx-tag-stopped">Stopped<' in html, classes(html))
check("stopped tag shows at mini too", 'cx-tag-stopped">Stopped<' in render(f, "mini"))
cards.set_whereabouts(stopped["id"], "sold")
f = cards.card_face(stopped["id"])
html = render(f, "small")
check("stopped + sold: grey and the Stopped tag", "cx-gone" in classes(html) and "cx-tag-stopped" in html)

done = cards.create("Finished Desk", kind="project", stage="done").data["card"]
html = render(cards.card_face(done["id"]), "full")
check("a done card has no tag and no grey", "cx-tag" not in html and "cx-gone" not in classes(html))

# A kind without whereabouts, even with a stray value in the column (written raw), never greys.
act = cards.create("Swap the motor", kind="action", stage="done").data["card"]
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("UPDATE projects SET whereabouts = 'sold' WHERE id = ?", (act["id"],))
c.commit()
c.close()
f = cards.card_face(act["id"])
check("action with a stray 'sold' never greys", not f["gone"] and "cx-gone" not in classes(render(f, "full")))

hobby_face = {"kind": "hobby", "title": "RC", "slug": "rc", "href": "#", "stage": None, "codes": [],
              "rel_lines": [], "notes": [], "dates": "", "type_line": "Hobby"}
check("hobby face never greys or tags", "cx-gone" not in render(hobby_face, "small") and "cx-tag" not in render(hobby_face, "small"))
ff = cards.file_face(slug="x", title="x.png", dates="", type_line="Image", cover_url=None, href="/object/x")
check("file face never greys or tags", "cx-gone" not in render(ff, "mini") and "cx-tag" not in render(ff, "mini"))

# ---- 4. pages --------------------------------------------------------------------------------
cards.set_whereabouts(thing["id"], "abandoned")
home = client.get("/")
face_html = re.search(r'<a class="[^"]*"[^>]*data-card-slug="%s".*?</a>' % re.escape(thing["slug"]), home.text, flags=re.S)
check("home: the abandoned card renders grey", home.status_code == 200 and face_html and "cx-gone" in face_html.group(0),
      home.status_code)
page = client.get(f"/project/{thing['slug']}")
check("card page: 200 and its header card is grey", page.status_code == 200 and "cx-gone" in page.text, page.status_code)
check("card page: picker offers 'Abandoned (left it behind)' with help",
      '<option value="abandoned" title="' in page.text and "Abandoned (left it behind)" in page.text)
api = client.get(f"/api/cards/{thing['slug']}").json()
check("GET /api/cards/<slug>: gone true", api.get("gone") is True and api.get("stopped_tag") is False, api.get("gone"))

ctx.__exit__(None, None, None)
print(f"\n{len(FAILS)} failure(s)")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
else:
    print("ALL PASS")
