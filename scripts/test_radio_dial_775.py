#!/usr/bin/env python3
"""#775 "Tune in to the past" (pass 1): the home page timeline is a horizontal radio dial.

Throwaway SQLite DB and storage (scripts/_testenv.py), the real FastAPI app through TestClient
(install token = admin, plus a signed-in viewer). No server, no network:

    python scripts/test_radio_dial_775.py

  1. No dated cards: the dial's chassis still renders, with a plain "No dated cards yet" message
     (never a blank bar) and no data block or dial scripts.
  2. With cards: the dial container, its scripts (mountain-time, radio-dial-math, radio-dial) and a
     nonce'd JSON data block whose rows are exactly the dated cards (an undated card is left off),
     each with id, slug, title, cover_url, effective_start, effective_end, is_child.
  3. The old vertical left rail is gone: no timeline-rail markup or script, no -shifted layout
     classes on the page; timeline-rail.js is deleted; style.css has no rail or -shifted rules.
  4. The dev tuning panel: absent on a non-dev instance (admin token and viewer alike), absent for a
     viewer even with ?tune=1, present for an admin with ?tune=1, present on a dev instance
     (CONSTRUCTICON_ENV=dev, the `is_dev` template global).
  5. style.css: the dial's needle, readout and static rules exist, and no dial rule zooms or scales
     a mark (#775's lesson: marks never grow).
  6. The card page's own tuner (project-video-timeline.js) is untouched by the dial.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import json
import os
import re
import secrets
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("radio-dial-775-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from fastapi.testclient import TestClient  # noqa: E402
from core import db  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from web import common as webcommon  # noqa: E402

FAILS = []
BASE = "http://testserver"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def dial_data(html):
    m = re.search(r'<script type="application/json" id="radio-dial-data" nonce="[^"]+">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


def set_span(card_id, start, end):
    conn = db.get_conn()
    try:
        conn.execute("UPDATE projects SET start_date_override = ?, end_date_override = ? WHERE id = ?", (start, end, card_id))
        conn.commit()
    finally:
        conn.close()


db.init_db()
tok = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)
webcommon.templates.env.globals["is_dev"] = False  # a dev container sets CONSTRUCTICON_ENV=dev

# --- 1. no dated cards: a clear message in the chassis ---------------------------------------------
r = tok.get("/")
check("empty: / 200", r.status_code == 200, r.status_code)
html = r.text
check("empty: dial chassis renders", 'id="home-radio-dial"' in html and 'class="radio-dial-chassis"' in html)
check("empty: says there are no dated cards yet", "No dated cards yet" in html)
check("empty: no data block, no dial script", dial_data(html) is None and "radio-dial.js" not in html)

# --- 2. with cards ---------------------------------------------------------------------------------
JAN_1999, JUN_2005, DEC_2024 = 915_235_200, 1_117_584_000, 1_734_998_400
old = db._create_project("Old radio build")
mid = db._create_project("Mid-2000s thing")
new = db._create_project("Recent project")
undated = db._create_project("Undated idea")
set_span(old["id"], JAN_1999, JAN_1999 + 86_400 * 40)
set_span(mid["id"], JUN_2005, JUN_2005 + 86_400 * 400)
set_span(new["id"], DEC_2024, DEC_2024)
child = db._create_project("A part of the old build", parent_id=old["id"])
set_span(child["id"], JAN_1999 + 86_400 * 5, JAN_1999 + 86_400 * 9)

r = tok.get("/")
check("cards: / 200", r.status_code == 200, r.status_code)
html = r.text
check("cards: dial container present", '<section class="radio-dial" id="home-radio-dial"' in html)
for js in ("mountain-time.js", "radio-dial-math.js", "radio-dial.js"):
    check(f"cards: loads {js}", f"/static/js/{js}?v=" in html)
data = dial_data(html)
check("cards: nonce'd JSON data block present", data is not None)
data = data or []
slugs = {d["slug"] for d in data}
check("cards: data = the four dated cards", slugs == {old["slug"], mid["slug"], new["slug"], child["slug"]}, sorted(slugs))
check("cards: undated card left off the dial", undated["slug"] not in slugs)
want_keys = {"id", "slug", "title", "cover_url", "effective_start", "effective_end", "is_child"}
check("cards: every row has the dial's fields", all(want_keys <= set(d) for d in data), [sorted(d) for d in data][:1])
by_slug = {d["slug"]: d for d in data}
check("cards: start dates carried as unix seconds", by_slug.get(old["slug"], {}).get("effective_start") == JAN_1999)
check("cards: child flagged", by_slug.get(child["slug"], {}).get("is_child") is True)
check("cards: dial sits above the page title", 0 <= html.find('id="home-radio-dial"') < html.find('class="page-title"'))
check("cards: mount script reports a failure in the chassis", "The timeline dial did not load" in html)

# --- 3. the old rail is gone ---------------------------------------------------------------------------
for gone in ("timeline-rail", "gallery-timeline-rail", "TimelineRail", "app-main-shifted", "brand-shifted"):
    check(f"rail gone: no '{gone}' on the home page", gone not in html)
check("rail gone: timeline-rail.js deleted", not os.path.exists(os.path.join(ROOT, "web", "static", "js", "timeline-rail.js")))
css = re.sub(r"/\*.*?\*/", "", read("web/static/style.css"), flags=re.S)  # rules only, not comments
for rule in (".timeline-rail", ".timeline-entry", ".timeline-year-marker", ".timeline-month-marker",
             ".app-main-shifted", ".brand-shifted", ".timeline-popover-hoverable"):
    check(f"rail gone: no {rule} rule in style.css", not re.search(re.escape(rule) + r"\b[^{]*\{", css))
check("kept: .timeline-popover (the card page's tuner uses it)", ".timeline-popover {" in css)
r = tok.get("/unfiled")
check("other pages: no -shifted brand", r.status_code == 200 and "brand-shifted" not in r.text)

# --- 4. the dev tuning panel ---------------------------------------------------------------------------
check("tune: absent on non-dev for the admin token", 'id="radio-dial-tune"' not in html and "radio-dial-tuning.js" not in html)
r = tok.get("/?tune=1")
check("tune: ?tune=1 shows it to an admin", 'id="radio-dial-tune"' in r.text and "/static/js/radio-dial-tuning.js?v=" in r.text)
viewer_name, viewer_pw = "viewer_" + secrets.token_hex(2), secrets.token_urlsafe(16)
r = tok.post("/api/users", json={"username": viewer_name, "password": viewer_pw, "role": "viewer"}, headers=SAME)
check("tune: admin creates a viewer", r.status_code == 200, r.text[:200])
viewer = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
r = viewer.post("/api/auth/login", json={"username": viewer_name, "password": viewer_pw}, headers=SAME)
check("tune: viewer signs in", r.status_code == 200, r.text[:200])
r = viewer.get("/")
check("tune: viewer sees the dial", r.status_code == 200 and 'id="home-radio-dial"' in r.text)
check("tune: absent for a viewer on non-dev", 'id="radio-dial-tune"' not in r.text)
r = viewer.get("/?tune=1")
check("tune: ?tune=1 does not show it to a viewer", 'id="radio-dial-tune"' not in r.text and "radio-dial-tuning.js" not in r.text)
webcommon.templates.env.globals["is_dev"] = True
try:
    r = viewer.get("/")
    check("tune: present on a dev instance", 'id="radio-dial-tune"' in r.text and "radio-dial-tuning.js" in r.text)
finally:
    webcommon.templates.env.globals["is_dev"] = False

# --- 5. the dial's CSS: static marks, needle, readout ------------------------------------------------
dial_rules = re.findall(r"(\.radio-dial[^{]*)\{([^}]*)\}", css)
check("css: dial rules exist", len(dial_rules) > 10, len(dial_rules))
for sel in (".radio-dial-needle", ".radio-dial-readout", ".radio-dial-static", ".radio-dial-card", ".radio-dial-year-label"):
    check(f"css: {sel} rule", any(s.strip().startswith(sel) for s, _ in dial_rules))
check("css: no dial rule scales or zooms a mark", not any(re.search(r"scale\(|zoom", body) for _, body in dial_rules),
      [s for s, b in dial_rules if re.search(r"scale\(|zoom", b)])
check("css: no hover rule on a dial mark", not any(":hover" in s and re.search(r"radio-dial-(card|year|month)", s) for s, _ in dial_rules))
check("css: glow is a variable", "--dial-glow" in css)
rule_body = lambda sel: next((b for s, b in dial_rules if s.strip() == sel), "")  # noqa: E731
check("css: backlight drives the chassis's inset wash",
      re.search(r"inset[^;]*var\(--dial-backlight\)", rule_body(".radio-dial-chassis")) is not None, rule_body(".radio-dial-chassis"))
check("css: backlight drives the lamp behind the band", "--dial-backlight" in rule_body(".radio-dial-window"))
check("css: the readout sits on a plate that thickens with the backlight (legible at max)",
      "--dial-backlight" in rule_body(".radio-dial-readout") and "z-index" in rule_body(".radio-dial-readout"))
js_src = read("web/static/js/radio-dial.js")
for var, key in (("--dial-glow", "glow"), ("--dial-backlight", "backlight")):
    css_default = re.search(re.escape(var) + r":\s*([0-9.]+)", css)
    js_default = re.search(r"\b" + key + r":\s*([0-9.]+),", js_src)
    check(f"css: {var} default matches DIAL_DEFAULTS.{key}",
          bool(css_default and js_default) and float(css_default.group(1)) == float(js_default.group(1)),
          (css_default and css_default.group(1), js_default and js_default.group(1)))
js = read("web/static/js/radio-dial.js")
check("js: feel values in one DIAL_DEFAULTS block", "const DIAL_DEFAULTS = {" in js
      and all(k in js for k in ("visibleYears", "friction", "snapStrength", "monthTickMinPx", "glow")))
check("js: no inline event handler strings", not re.search(r"\son[a-z]+=", js))

# --- 6. the card page's tuner is its own thing ---------------------------------------------------------
pvt = read("web/static/js/project-video-timeline.js")
check("card tuner: still its own class, not using the dial", "class ProjectVideoTimeline" in pvt and "RadioDial" not in pvt)

print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
