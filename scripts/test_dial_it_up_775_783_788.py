#!/usr/bin/env python3
"""Batch 14 "Dial It Up": #775 pass 2 (span bars + density glow), #788 (the tuning panel only with
?tune=1 for an admin), #783 (the museum dial theme, an Admin > Install choice).

Throwaway SQLite DB and storage (scripts/_testenv.py), the real FastAPI app through TestClient
(install token = admin, plus a signed-in admin and a signed-in editor). No server, no network:

    python scripts/test_dial_it_up_775_783_788.py

  1. #775 pass 2: the home page embeds each dial card's end date (a spanned card's real end, a
     single-moment card's end = start); radio-dial.js draws the bars and glow (no pass-2 stubs left);
     the span / density feel values are in DIAL_DEFAULTS, mirrored by --dial-* CSS defaults, and have
     sliders; the bar and glow rules never scale or zoom.
  2. #788: on a dev instance (is_dev) the tuning panel is absent without ?tune=1 (admin and editor),
     present for a signed-in admin (and the install token) with ?tune=1, absent for an editor with it.
  3. #783: the dial theme. Default radio (no row, no env); the env default
     (CONSTRUCTICON_TUNER_THEME=museum) applies when nothing is set; a bad env value is ignored and
     named in the admin view; set museum via POST /api/install-config (one imaged change-log row, op
     install_config_update, undoable); an invalid value is a 400 bad_tuner_theme with a message and
     writes nothing; every page carries <html data-tuner-theme>; style.css has the museum block and
     routes the dial's colours through --tuner-* only.
  4. The node maths test (scripts/test_dial_it_up_775.js) and the widget test
     (scripts/test_radio_dial_ui_775.js), when node is on PATH; otherwise a clear SKIP line.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("dial-it-up-775-")
os.environ.setdefault("CAPTION_DISABLED", "1")
os.environ.pop("CONSTRUCTICON_TUNER_THEME", None)
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from fastapi.testclient import TestClient  # noqa: E402
from core import db, install_config  # noqa: E402
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


def html_theme(html):
    m = re.search(r'<html[^>]*\sdata-tuner-theme="([^"]*)"', html)
    return m.group(1) if m else None


def set_span(card_id, start, end):
    conn = db.get_conn()
    try:
        conn.execute("UPDATE projects SET start_date_override = ?, end_date_override = ? WHERE id = ?", (start, end, card_id))
        conn.commit()
    finally:
        conn.close()


def q(sql, args=()):
    conn = db.get_conn()
    try:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def sign_in(username, password):
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": username, "password": password}, headers=SAME)
    check(f"sign in as {username}", r.status_code == 200, r.text[:200])
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', c.get("/").text) if r.status_code == 200 else None
    check(f"{username}'s page carries a CSRF token", m is not None)
    return c, (m.group(1) if m else "")


db.init_db()
tok = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)
webcommon.templates.env.globals["is_dev"] = False

# --- 1. #775 pass 2: end dates on the page, bars and glow in the widget -------------------------------
JAN_2000, JUL_2003, FEB_2002 = 946_728_000, 1_057_032_000, 1_012_564_800
spanned = db._create_project("Long build")
single = db._create_project("One-day thing")
set_span(spanned["id"], JAN_2000, JUL_2003)
set_span(single["id"], FEB_2002, FEB_2002)
r = tok.get("/")
check("pass2: / 200", r.status_code == 200, r.status_code)
data = {d["slug"]: d for d in (dial_data(r.text) or [])}
check("pass2: the dial data carries the spanned card's real end", data.get(spanned["slug"], {}).get("effective_end") == JUL_2003,
      data.get(spanned["slug"]))
check("pass2: a single-moment card's end = its start (it stays a tick)",
      data.get(single["slug"], {}).get("effective_end") == data.get(single["slug"], {}).get("effective_start") == FEB_2002)
js = read("web/static/js/radio-dial.js")
math_js = read("web/static/js/radio-dial-math.js")
check("pass2: the no-op stubs are gone, the layers are drawn",
      "pass 2 (#775): start-to-end bars" not in js and "M.spanBars(" in js and "M.yearDensity(" in js and "M.densityLevels(" in js)
check("pass2: the maths is in radio-dial-math.js", all(f"function {f}(" in math_js for f in ("spanBars", "yearDensity", "densityLevels")))
check("pass2: the band reaches the latest end", "coverEnds: true" in js)
css = re.sub(r"/\*.*?\*/", "", read("web/static/style.css"), flags=re.S)
all_rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
rule_body = lambda sel: next((b for s, b in all_rules if s.strip() == sel), "")  # noqa: E731
dial_vars = rule_body(".radio-dial")
for key, var, unit in (("spanHeight", "--dial-span-h", "px"), ("spanGap", "--dial-span-gap", "px"),
                       ("spanOpacity", "--dial-span-opacity", ""), ("densityStrength", "--dial-density-strength", ""),
                       ("densitySpread", "--dial-density-spread", "%")):
    css_v = re.search(re.escape(var) + r":\s*([0-9.]+)" + re.escape(unit) + r"\s*;", dial_vars)
    js_v = re.search(r"\b" + key + r":\s*([0-9.]+),", js)
    check(f"pass2: {var} default matches DIAL_DEFAULTS.{key}",
          bool(css_v and js_v) and float(css_v.group(1)) == float(js_v.group(1)),
          (css_v and css_v.group(1), js_v and js_v.group(1)))
    check(f"pass2: {key} has a slider and a CSS mapping", f"key: '{key}'" in js and f"{key}: ['{var}'" in js)
for key in ("spanMinPx", "spanLanes", "densityCurve", "densitySpans"):
    check(f"pass2: {key} is a default with a slider", re.search(r"\b" + key + r":\s*[0-9.]+,", js) and f"key: '{key}'" in js)
for sel in (".radio-dial-span", ".radio-dial-density"):
    body = rule_body(sel)
    check(f"pass2: {sel} rule exists and never scales or zooms", body and not re.search(r"scale\(|zoom|transform", body), body[:120])
check("pass2: the bars and glow take their colours from the tuner theme",
      "var(--tuner-span)" in rule_body(".radio-dial-span") and "var(--tuner-density)" in rule_body(".radio-dial-density"))

# --- 2. #788: the tuning panel only with ?tune=1 for an admin, also on dev ------------------------------
admin_name, admin_pw = "admin_" + secrets.token_hex(2), secrets.token_urlsafe(16)
ed_name, ed_pw = "editor_" + secrets.token_hex(2), secrets.token_urlsafe(16)
for name, pw, role in ((admin_name, admin_pw, "admin"), (ed_name, ed_pw, "editor")):
    r = tok.post("/api/users", json={"username": name, "password": pw, "role": role}, headers=SAME)
    check(f"tune: make a {role}", r.status_code == 200, r.text[:200])
admin, admin_csrf = sign_in(admin_name, admin_pw)
editor, ed_csrf = sign_in(ed_name, ed_pw)
panel = lambda html: 'id="radio-dial-tune"' in html or "radio-dial-tuning.js" in html  # noqa: E731
webcommon.templates.env.globals["is_dev"] = True
try:
    check("tune: dev, admin, no ?tune=1: no panel", not panel(admin.get("/").text))
    check("tune: dev, editor, no ?tune=1: no panel", not panel(editor.get("/").text))
    check("tune: dev, install token, no ?tune=1: no panel", not panel(tok.get("/").text))
    r = admin.get("/?tune=1")
    check("tune: dev, signed-in admin with ?tune=1: the panel and its script",
          'id="radio-dial-tune"' in r.text and "/static/js/radio-dial-tuning.js?v=" in r.text)
    check("tune: install token (admin) with ?tune=1: the panel", 'id="radio-dial-tune"' in tok.get("/?tune=1").text)
    check("tune: dev, editor with ?tune=1: no panel", not panel(editor.get("/?tune=1").text))
    check("tune: ?tune=0 shows nothing", not panel(admin.get("/?tune=0").text))
finally:
    webcommon.templates.env.globals["is_dev"] = False
check("tune: prod, admin with ?tune=1: the panel", 'id="radio-dial-tune"' in admin.get("/?tune=1").text)
home_src = read("web/templates/home.html")
check("tune: home.html no longer shows it for is_dev", "is_dev or" not in home_src
      and "{% set dial_tune = request.query_params.get('tune') == '1' and has_role('admin') %}" in home_src)
tune_js = read("web/static/js/radio-dial-tuning.js")
check("tune: the panel no longer calls itself dev-only", "(dev only)" not in tune_js and "CONSTRUCTICON_ENV=dev, or" not in tune_js)
check("tune: the panel previews themes without saving them", 'data-act="theme"' in tune_js and "data-tuner-theme" in tune_js)

# --- 3. #783: the dial theme -----------------------------------------------------------------------------
install_config.clear_cache()
check("theme: default is radio (no row, no env)", install_config.tuner_theme() == "radio"
      and install_config.tuner_theme_status()["source"] == "default")
check("theme: the page carries data-tuner-theme=radio", html_theme(tok.get("/").text) == "radio")
os.environ["CONSTRUCTICON_TUNER_THEME"] = "Museum"
try:
    install_config.clear_cache()
    st = install_config.tuner_theme_status()
    check("theme: the env default applies when nothing is set (any case)",
          install_config.tuner_theme() == "museum" and st["source"] == "env" and not st["env_invalid"], st)
    check("theme: the page follows the env default", html_theme(tok.get("/").text) == "museum")
    cfg = tok.get("/api/install-config").json()
    check("theme: admin view shows the env default as the fallback", "Museum" in cfg["fallbacks"]["tuner_theme"], cfg["fallbacks"])
    os.environ["CONSTRUCTICON_TUNER_THEME"] = "neon"
    install_config.clear_cache()
    st = install_config.tuner_theme_status()
    check("theme: a bad env value is ignored (radio) and named, not silent",
          install_config.tuner_theme() == "radio" and st["env_invalid"] and st["env_value"] == "neon", st)
finally:
    os.environ.pop("CONSTRUCTICON_TUNER_THEME", None)
    install_config.clear_cache()

cfg = tok.get("/api/install-config").json()
field = next((f for f in cfg["fields"] if f["key"] == "tuner_theme"), None)
check("theme: Admin > Install lists a 'Dial theme' choice field",
      field is not None and field.get("kind") == "choice" and [c["value"] for c in field["choices"]] == ["radio", "museum"], field)
check("theme: the admin view carries the status", cfg.get("tuner_theme", {}).get("current") == "radio")

before_rows = q("SELECT COUNT(*) FROM audit_log")
before_cfg = q("SELECT key, value FROM install_config ORDER BY key")
for bad, why in (("neon", "unknown"), (7, "not text"), ("museum; drop", "junk")):
    r = tok.post("/api/install-config", json={"tuner_theme": bad}, headers=SAME)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    err = body.get("error") or {}
    check(f"theme: invalid value ({why}) refused with 400 bad_tuner_theme and a message",
          r.status_code == 400 and err.get("code") == "bad_tuner_theme" and "radio" in err.get("message", "")
          and "museum" in err.get("message", ""), (r.status_code, r.text[:200]))
check("theme: a refused value writes nothing", q("SELECT key, value FROM install_config ORDER BY key") == before_cfg)

r = admin.post("/api/install-config", json={"tuner_theme": "museum"},
               headers={**SAME, "X-CSRF-Token": admin_csrf or ""})
check("theme: a signed-in admin sets museum", r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text[:200]))
batch = r.json().get("batch_id") if r.status_code == 200 else None
check("theme: the save answers a batch id", bool(batch))
check("theme: stored", q("SELECT value FROM install_config WHERE key = 'tuner_theme'") == [("museum",)])
rows = db.get_change_rows(batch_id=batch) if batch else []
check("theme: one change-log row, op install_config_update", len(rows) == 1 and rows[0]["op"] == install_config.OP_UPDATE,
      [dict(x).get("op") for x in rows])
check("theme: the change-log row names the admin", rows and rows[0].get("actor") == f"user:{admin_name}", rows and rows[0].get("actor"))
check("theme: the response's config shows museum from the setting",
      r.json()["config"]["tuner_theme"]["current"] == "museum" and r.json()["config"]["tuner_theme"]["source"] == "setting")
for path in ("/", "/admin", f"/project/{spanned['slug']}"):
    page = tok.get(path)
    check(f"theme: {path} carries data-tuner-theme=museum", page.status_code == 200 and html_theme(page.text) == "museum",
          (page.status_code, html_theme(page.text)))
r = tok.post("/api/install-config", json={"tuner_theme": "MUSEUM "}, headers=SAME)
check("theme: saving the same value again changes nothing", r.status_code == 200 and r.json().get("batch_id") is None, r.text[:200])
os.environ["CONSTRUCTICON_TUNER_THEME"] = "radio"
try:
    install_config.clear_cache()
    check("theme: the Admin > Install pick beats the env default", install_config.tuner_theme() == "museum")
finally:
    os.environ.pop("CONSTRUCTICON_TUNER_THEME", None)
    install_config.clear_cache()
u = tok.post(f"/api/changes/{batch}/undo", headers=SAME)
install_config.clear_cache()
check("theme: undo puts radio back", u.status_code == 200 and install_config.tuner_theme() == "radio"
      and html_theme(tok.get("/").text) == "radio", (u.status_code, u.text[:200]))
r = tok.post("/api/install-config", json={"tuner_theme": "museum"}, headers=SAME)
r = tok.post("/api/install-config", json={"tuner_theme": ""}, headers=SAME)
install_config.clear_cache()
check("theme: blank clears it (back to the default)", r.status_code == 200 and install_config.tuner_theme() == "radio"
      and q("SELECT value FROM install_config WHERE key = 'tuner_theme'") == [])
r = editor.post("/api/install-config", json={"tuner_theme": "museum"}, headers={**SAME, "X-CSRF-Token": ed_csrf})
check("theme: an editor can't change it (admin setting, 403 forbidden)",
      r.status_code == 403 and (r.json().get("error") or {}).get("code") == "forbidden", (r.status_code, r.text[:160]))

theme_rule = rule_body(':root[data-tuner-theme="museum"]')
check("theme: style.css has the museum block", bool(theme_rule))
for var in ("--tuner-chassis", "--tuner-readout", "--tuner-readout-glow", "--tuner-yellow", "--tuner-phosphor",
            "--tuner-shadow", "--tuner-lamp", "--tuner-plate", "--tuner-font", "--tuner-span", "--tuner-density"):
    check(f"theme: museum sets {var}", re.search(re.escape(var) + r":", theme_rule) is not None)
check("theme: museum's derived colours don't depend on the radio sliders' base values",
      not re.search(r"var\(--(tuner-glow|tuner-backlight|tuner-readout-hue|accent-yellow-hue)\)", theme_rule))
dial_rules = [(s, b) for s, b in all_rules if s.strip().startswith(".radio-dial") and not s.strip().startswith(".radio-dial-tune")]
check("theme: no hard-coded colours left in the dial's rules (all through --tuner-*)",
      not any(re.search(r"rgba?\(|#[0-9a-fA-F]{3,6}\b|'JetBrains Mono'", b) for _, b in dial_rules),
      [s.strip() for s, b in dial_rules if re.search(r"rgba?\(|#[0-9a-fA-F]{3,6}\b|'JetBrains Mono'", b)])
radio_root = next((b for s, b in all_rules if s.strip() == ":root" and "--tuner-chassis" in b), "")
check("theme: radio stays the :root default (the chassis is still #0a0d12)", "--tuner-chassis: #0a0d12" in radio_root)
admin_src = read("web/templates/admin.html")
check("theme: the admin panel draws a choice field as a select, and names a bad env value",
      "f.kind === 'choice'" in admin_src and "env_invalid" in admin_src)
check("theme: base.html puts it on <html>", 'data-tuner-theme="{{ tuner_theme() }}"' in read("web/templates/base.html"))

# --- 4. the node tests -----------------------------------------------------------------------------------
node = shutil.which("node")
if not node:
    print("SKIP node tests: node is not on PATH (run node scripts/test_dial_it_up_775.js and "
          "node scripts/test_radio_dial_ui_775.js where it is)")
else:
    for script in ("scripts/test_dial_it_up_775.js", "scripts/test_radio_dial_ui_775.js"):
        p = subprocess.run([node, os.path.join(ROOT, script)], capture_output=True, text=True, timeout=120)
        fails = [ln for ln in p.stdout.splitlines() if ln.startswith("FAIL")]
        check(f"node: {script}", p.returncode == 0 and not fails, (p.returncode, fails[:5], p.stderr[-300:]))

print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
