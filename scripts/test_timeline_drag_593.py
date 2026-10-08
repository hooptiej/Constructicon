#!/usr/bin/env python3
"""Turn the dial (#593): the server side of dragging a project-timeline marker. Throwaway DB, no server.

    python scripts/test_timeline_drag_593.py

In-process, with real sessions (editors E1 and E2, viewer V), anonymous and the install token:
  1. an editor re-dates an item (POST /api/image/{slug}/date): ONE change-log row, the override is
     stored, the project page's timeline data carries the new date and "set by hand", and the card's
     computed span recomputes (the answer's `span` and the page agree);
  2. undo (the answer's batch_id) restores the old date, the timeline and the span;
  3. a same-date drop is a no-op: no batch_id, no change-log row; reset=true clears the override in
     one row and is undoable; bad dates (before 1970, past 2100, nan, missing) are refused 400 and
     write nothing;
  4. the card's own Timeline start / end overrides still win over the recomputed span;
  5. permissions: a viewer gets 403, anonymous 401, and nothing is written; the page tells an editor
     canEdit=true and a viewer false;
  6. the policy: a sensitive item (admin-owned) is not in a viewer's or another editor's timeline data
     at all, an editor who can't see it gets 404 on the write, and it doesn't leak into the `span`
     an editor's drag returns (even when it sits far in the future);
  7. the service: items.set_display_date validates and is the only writer.
Exits 1 if any check fails.
"""

import io
import json
import os
import re
import secrets
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("tl593-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, items, membership, users  # noqa: E402
from core.errors import InvalidInput  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
BASE = "http://testhost.local"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def new_client():
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def png_bytes(seed):
    from PIL import Image
    img = Image.new("RGB", (32, 24), (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def row(slug):
    return db.get_by_slug(slug)


def change_rows(batch_id):
    conn = db.get_conn()
    try:
        return conn.execute("SELECT id, op FROM audit_log WHERE batch_id = ?", (batch_id,)).fetchall()
    finally:
        conn.close()


def timeline_json(client):
    """The project page's `timelineItems` list and `canEdit` flag, parsed out of the inline script."""
    html = client.get(f"/project/{CARD['slug']}").text
    m = re.search(r"const timelineItems = (\[.*?\]);\n", html, re.S)
    ce = re.search(r"const canEdit = (true|false);", html)
    return ({i["slug"]: i for i in json.loads(m.group(1))} if m else None), (ce.group(1) if ce else None)


db.init_db()
users.limiter.reset()
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("tl_ed1", "editor"), ("tl_ed2", "editor"), ("tl_view", "viewer")):
        PW[name] = "tl-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
C = {"anonymous": new_client(), "token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for key, name in (("E1", "tl_ed1"), ("E2", "tl_ed2"), ("V", "tl_view")):
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    check(f"{key} signs in", r.status_code == 200, r.text[:200])
    C[key] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in ("E1", "E2", "V") else dict(SAME)) for k, c in C.items()}


def upload(who, filename, seed):
    r = C[who].post("/api/upload", files={"file": (filename, png_bytes(seed), "image/png")},
                    data={"description": f"tl593 {filename}"}, headers=H[who])
    assert r.status_code == 200, r.text[:200]
    return r.json()["slug"]


def put_date(who, slug, **form):
    return C[who].post(f"/api/image/{slug}/date", data=form, headers=H[who])


Y = 365 * 86400
T0 = 1_600_000_000.0  # Sep 2020
A, B, S = upload("token", "tl593-a.png", 1), upload("token", "tl593-b.png", 2), upload("token", "tl593-sens.png", 3)
D = upload("token", "tl593-d.png", 4)
with actor.acting_as(actor.ACTOR_SCRIPT):
    for slug, ts in ((A, T0), (B, T0 + 100 * 86400), (D, T0 + 200 * 86400)):
        res = items.update(slug, content_date=ts)  # a known real date: the computed date (not by hand)
    card = cards.create("Timeline 593 card").data["card"]
    CARD = db.get_project(card["id"] if isinstance(card, dict) else card)
    membership.add_files(CARD["id"], [A, B, S, D], **membership.UI_EFFECTS)
    items.set_sensitive([S], True)  # admin-owned (script) and flagged: only an admin sees it
    items.update(S, content_date=T0 + 9 * Y)  # far in the future: would show as the card's end if it leaked
CARD = db.get_project(CARD["id"])

print("--- 1. an editor re-dates an item ---")
tl, ce = timeline_json(C["E1"])
check("the page has the timeline data", tl is not None and A in tl, list((tl or {}).keys()))
check("before: A is on its computed date, not by hand", tl[A]["effective_date"] == T0 and tl[A]["set_by_hand"] is False)
check("canEdit is true for an editor", ce == "true", ce)
NEW = T0 - 400 * 86400
r = put_date("E1", A, display_date=str(NEW), project=CARD["slug"])
check("editor POST /date (200)", r.status_code == 200, r.text[:200])
out = r.json()
check("answer: new effective date, set by hand, a batch_id", out["effective_date"] == NEW and out["set_by_hand"] is True
      and out["batch_id"] and out["changed"] is True, out)
check("override stored", row(A)["display_date_override"] == NEW, row(A)["display_date_override"])
check("content_date untouched", row(A)["content_date"] == T0)
rows = change_rows(out["batch_id"])
check("ONE change-log row for the batch", len(rows) == 1, [tuple(x) for x in rows])
tl, _ = timeline_json(C["E1"])
check("timeline data after reload: A at the new date, set by hand", tl[A]["effective_date"] == NEW and tl[A]["set_by_hand"] is True)
check("the card span recomputed: starts at A's new date", out["span"]["start"] == NEW and out["span"]["end"] == T0 + 200 * 86400, out["span"])
check("span says computed, not by hand", out["span"]["start_by_hand"] is False and out["span"]["end_by_hand"] is False)
check("item page DATES group reads 'set by hand'", "set by hand" in C["E1"].get(f"/object/{A}").text)

print("--- 2. undo ---")
ru = C["E1"].post(f"/api/changes/{out['batch_id']}/undo", headers=H["E1"])
check("undo (200)", ru.status_code == 200, ru.text[:200])
check("override cleared again", row(A)["display_date_override"] is None)
tl, _ = timeline_json(C["E1"])
check("timeline back on the computed date", tl[A]["effective_date"] == T0 and tl[A]["set_by_hand"] is False)

print("--- 3. no-op, reset, bad dates ---")
r1 = put_date("E1", A, display_date=str(NEW))
r2 = put_date("E1", A, display_date=str(NEW))
check("dropping on the same date again is a no-op: no batch_id, changed false",
      r2.status_code == 200 and r2.json()["batch_id"] is None and r2.json()["changed"] is False, r2.text[:200])
rr = put_date("E1", A, reset="true", project=CARD["slug"])
check("reset (200): back on the computed date, not by hand",
      rr.status_code == 200 and rr.json()["effective_date"] == T0 and rr.json()["set_by_hand"] is False, rr.text[:200])
check("reset: one change-log row and override NULL", len(change_rows(rr.json()["batch_id"])) == 1 and row(A)["display_date_override"] is None)
check("reset is undoable (the override comes back)",
      C["E1"].post(f"/api/changes/{rr.json()['batch_id']}/undo", headers=H["E1"]).status_code == 200
      and row(A)["display_date_override"] == NEW)
put_date("E1", A, reset="true")
before_rows = len(db.get_conn().execute("SELECT id FROM audit_log WHERE op IS NOT NULL").fetchall())
for label, form in (("before 1970", {"display_date": "-5"}), ("after 2100", {"display_date": "99999999999"}),
                    ("nan", {"display_date": "nan"}), ("inf", {"display_date": "inf"}), ("missing", {})):
    rb = put_date("E1", A, **form)
    check(f"bad date ({label}) is refused 400, nothing written", rb.status_code == 400 and row(A)["display_date_override"] is None,
          (rb.status_code, rb.text[:120]))
check("refusals wrote no change-log rows",
      len(db.get_conn().execute("SELECT id FROM audit_log WHERE op IS NOT NULL").fetchall()) == before_rows)
check("unknown item is 404", put_date("E1", "nosuchslug", display_date=str(NEW)).status_code == 404)

print("--- 4. the card's own overrides still win ---")
with actor.acting_as(actor.ACTOR_SCRIPT):
    cards.update(CARD["id"], end=T0 + 500 * 86400)
r = put_date("E1", B, display_date=str(T0 + 50 * 86400), project=CARD["slug"])
check("end override wins in the recomputed span; start follows the items",
      r.json()["span"]["end"] == T0 + 500 * 86400 and r.json()["span"]["end_by_hand"] is True and r.json()["span"]["start"] == T0
      and r.json()["span"]["start_by_hand"] is False, r.json().get("span"))
with actor.acting_as(actor.ACTOR_SCRIPT):
    cards.update(CARD["id"], end=None)
    items.set_display_date(B, None)

print("--- 5. permissions ---")
rv = put_date("V", A, display_date=str(NEW))
check("viewer is refused 403 forbidden", rv.status_code == 403 and rv.json()["error"]["code"] == "forbidden", rv.text[:160])
ra = put_date("anonymous", A, display_date=str(NEW))
check("anonymous is refused 401", ra.status_code == 401, ra.status_code)
check("nothing was written by them", row(A)["display_date_override"] is None)
_, cev = timeline_json(C["V"])
check("canEdit is false for a viewer (a static timeline)", cev == "false", cev)
rt = put_date("token", A, display_date=str(NEW))
check("the install token (admin) may", rt.status_code == 200 and row(A)["display_date_override"] == NEW)
put_date("token", A, reset="true")

print("--- 6. the item policy ---")
tlv, _ = timeline_json(C["V"])
tle2, _ = timeline_json(C["E2"])
tla, _ = timeline_json(C["token"])
check("a sensitive item is not in a viewer's timeline data", tlv is not None and S not in tlv and A in tlv, list((tlv or {}).keys()))
check("nor in another editor's", tle2 is not None and S not in tle2)
check("an admin (the token) does see it", S in tla)
check("the sensitive slug isn't anywhere in the viewer's page", S not in C["V"].get(f"/project/{CARD['slug']}").text)
r = put_date("E2", S, display_date=str(NEW))
check("an editor who can't see it gets 404 on the write, nothing written", r.status_code == 404 and row(S)["display_date_override"] is None, r.status_code)
r = put_date("E1", D, display_date=str(T0 + 210 * 86400), project=CARD["slug"])
check("the span an editor gets doesn't count the hidden far-future item",
      r.json()["span"]["end"] == T0 + 210 * 86400, r.json().get("span"))
r = put_date("token", S, display_date=str(NEW))
check("the admin can re-date it", r.status_code == 200 and row(S)["display_date_override"] == NEW)

print("--- 7. the service ---")
bad = []
for v in (-1, 4102444801, float("nan"), float("inf"), True, "abc"):
    try:
        with actor.acting_as(actor.ACTOR_SCRIPT):
            items.set_display_date(A, v)
        bad.append(v)
    except InvalidInput as e:
        if e.code != "bad_date":
            bad.append((v, e.code))
check("items.set_display_date refuses bad dates with bad_date", not bad, bad)
with actor.acting_as(actor.ACTOR_SCRIPT):
    res = items.set_display_date(A, NEW)
    res2 = items.set_display_date(A, NEW)
    dry = items.set_display_date(A, NEW + 86400, dry_run=True)
check("service: first write changed, repeat did not, dry run wrote nothing",
      res.data["changed"] and not res2.data["changed"] and row(A)["display_date_override"] == NEW and dry.data["effective_date"] == NEW + 86400)

print()
print(("FAILED: " + ", ".join(FAILS)) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
