#!/usr/bin/env python3
"""Self-contained check for #686: hobby covers (a chosen cover with an automatic fallback) and the
hobbies index / drawer on the shared card component.

Throwaway SQLite DB and storage (scripts/_testenv.py), built by the real `db.init_db()`; the real
FastAPI app through TestClient and the MCP tool functions called directly. Covers:
  - the schema: hobby_settings.cover_slug exists after init_db, and a second init_db is a no-op;
  - the automatic order: no cover anywhere -> none; the most recent project (updated_at) that HAS a
    cover; a highlighted project's cover beats a more recent one; a highlighted project with no
    cover is skipped; a project borrowing a child's cover counts;
  - a chosen cover overrides it; a deleted (trashed) or redacted chosen cover falls back to the
    automatic one (picked_unavailable), and undoing the delete brings the choice back;
  - set / clear through the service, the web route and the MCP tool; refusals (unknown item,
    redacted, no picture, cover_slug + clear) write nothing; dry_run writes nothing; each write is
    one `hobby_cover` change-log row with the right actor, and its undo leaves the whole DB as it was;
  - GET /api/hobbies and GET /api/hobby/{slug} carry cover_url / cover_slug / cover_picked /
    cover_source; MCP list_hobbies carries the same fields;
  - /hobbies and /api/hobbies/html render the shared card (cx cx-small cx-kind-hobby) with an <img>
    from the thumb route and NO hardcoded placeholder when a cover exists; a hobby with no cover gets
    the card's own empty art; flags, group code, project count and activity still show; the page
    has no inline script any more; the hobby page's COVER group lists the choices;
  - scripts/check_layering.py passes.
No server needed:

    python scripts/test_hobby_covers_686.py

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
TMP = _testenv.isolate("hobby-covers-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, card_rules, cards, db, hobbies, ingest, items, paths  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None  # no background threads in a unit check

FAILS = []


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


def execute(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def snapshot():
    """Every table but the audit/change log and the derived search index, every non-BLOB column."""
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                              "AND name NOT IN ('audit_log') AND name NOT LIKE 'item_search%' ORDER BY name"):
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


def mk(slug, media_type="image", ext="png"):
    sf = f"{slug}.{ext}"
    if media_type == "image":
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (10, 200, 10)).save(buf, "PNG")
        data = buf.getvalue()
    else:
        data = b"ID3" + b"\0" * 64
    (paths.storage_dir() / sf).write_bytes(data)
    db.insert_upload(slug, f"{slug}.{ext}", sf, "tester", media_type=media_type)
    return slug


def resolved(hobby_id):
    return cards.resolve_hobby_cover(db.get_hobby(hobby_id))


def touch(card_id, ts):
    """Fixture: set a card's updated_at directly (raw SQL is allowed in test fixtures)."""
    execute("UPDATE projects SET updated_at = ? WHERE id = ?", ts, card_id)


# ---- 1. schema ---------------------------------------------------------------------------------
db.init_db()
cols = {r["name"] for r in q("PRAGMA table_info(hobby_settings)")}
check("hobby_settings.cover_slug exists after init_db", "cover_slug" in cols, sorted(cols))
db.init_db()
check("a second init_db is a no-op for the column", {r["name"] for r in q("PRAGMA table_info(hobby_settings)")} == cols)
check("no data migration needed: cover_slug is NULL (automatic) for every row",
      not q("SELECT 1 FROM hobby_settings WHERE cover_slug IS NOT NULL"))

ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

from web import app as webapp  # noqa: E402
from mcp_server import server  # noqa: E402

client = _testenv.client(webapp.app)

# ---- 2. fixture --------------------------------------------------------------------------------
H = hobbies.create("R/C Adventures").data["hobby"]
hobbies.set_group_code(H["id"], "RCA")
older = cards.create("Older truck", kind="thing").data["card"]
newer = cards.create("Newer truck", kind="thing").data["card"]
star = cards.create("Star truck", kind="thing").data["card"]
for c in (older, newer, star):
    hobbies.add_card(H["id"], c["id"])
p_old, p_new, p_star, p_child = mk("old-photo"), mk("new-photo"), mk("star-photo"), mk("child-photo")
loose = mk("loose-photo")
song = mk("a-song", media_type="audio", ext="mp3")
execute("INSERT OR IGNORE INTO post_tags (post_slug, tag_id) VALUES (?, ?)", loose, H["id"])  # a loose object
touch(older["id"], 1000)
touch(newer["id"], 2000)
touch(star["id"], 500)

# ---- 3. the automatic order -------------------------------------------------------------------
r = resolved(H["id"])
check("no cover anywhere: none (source None, not picked)", r["slug"] is None and r["source"] is None and not r["picked"], r)

cards.update(older["id"], cover_slug=p_old)
touch(older["id"], 1000)
r = resolved(H["id"])
check("only the older project has a cover: it is used (recent_project)",
      r["slug"] == p_old and r["source"] == card_rules.HOBBY_COVER_RECENT and r["project"] == older["slug"], r)

cards.update(newer["id"], cover_slug=p_new)
touch(newer["id"], 2000)
r = resolved(H["id"])
check("the most recently updated project with a cover wins", r["slug"] == p_new and r["project"] == newer["slug"], r)

cards.set_highlight(star["id"], True)
touch(star["id"], 500)
r = resolved(H["id"])
check("a highlighted project with NO cover is skipped (still the most recent with one)", r["slug"] == p_new, r)

cards.update(star["id"], cover_slug=p_star)
touch(star["id"], 500)
r = resolved(H["id"])
check("the highlighted project's cover beats a more recent project's",
      r["slug"] == p_star and r["source"] == card_rules.HOBBY_COVER_HIGHLIGHT and r["project"] == star["slug"], r)

cards.update(star["id"], cover_slug="")
child = cards.create("Star truck chassis", kind="thing", parent=star["id"]).data["card"]
cards.update(child["id"], cover_slug=p_child)
cards.update(star["id"], cover_project_id=child["id"])
touch(star["id"], 500)
r = resolved(H["id"])
check("a highlighted project borrowing its child's cover counts (resolved cover)",
      r["slug"] == p_child and r["source"] == card_rules.HOBBY_COVER_HIGHLIGHT, r)
check("recent_first: updated_at, newest first", [p["slug"] for p in cards.recent_first(db.list_projects_for_hobby(H["id"]))]
      == [newer["slug"], older["slug"], star["slug"]])

# ---- 4. a chosen cover --------------------------------------------------------------------------
before = snapshot()
dry = hobbies.set_cover(H["id"], loose, dry_run=True)
check("dry run: answers, writes nothing", dry.dry_run and snapshot() == before and dry.data["picked_cover_slug"] == loose)
res = hobbies.set_cover(H["id"], loose)
r = resolved(H["id"])
check("a chosen cover overrides the automatic one", r["slug"] == loose and r["picked"]
      and r["source"] == card_rules.HOBBY_COVER_PICKED, r)
check("service set: one hobby_cover change-log row, actor from context",
      ops_of(res.batch_id) == [("hobby_cover", actor.ACTOR_UI)], ops_of(res.batch_id))
check("service set: the answer carries the resolved cover", res.data["cover"]["cover_slug"] == loose
      and res.data["cover"]["cover_picked"] is True)
again = hobbies.set_cover(H["id"], loose)
check("choosing the same cover again writes nothing", again.batch_id is None and again.warnings == ["Nothing changed."])
undo_ok("service set (settings row inserted)", res.batch_id, before)

res = hobbies.set_cover(H["id"], loose)
cleared = hobbies.set_cover(H["id"], "")
check("clearing goes back to automatic", resolved(H["id"])["source"] == card_rules.HOBBY_COVER_HIGHLIGHT
      and ops_of(cleared.batch_id) == [("hobby_cover", actor.ACTOR_UI)])
cards.undo(cleared.batch_id)
check("undoing the clear restores the choice", resolved(H["id"])["slug"] == loose)

# deleted / trashed / redacted chosen cover -> automatic, choice kept
d = items.delete([loose])
r = resolved(H["id"])
check("chosen cover deleted (in the trash): falls back to automatic, flagged unavailable",
      r["slug"] == p_child and not r["picked"] and r["picked_unavailable"] and r["picked_slug"] == loose, r)
check("the choice itself is kept while the item is gone",
      q("SELECT cover_slug FROM hobby_settings WHERE hobby_tag_id = ?", H["id"])[0]["cover_slug"] == loose)
cards.undo(d.batch_id)
check("undoing the delete brings the chosen cover back", resolved(H["id"])["slug"] == loose and resolved(H["id"])["picked"])
red = items.redact(loose)
r = resolved(H["id"])
check("chosen cover redacted: falls back to automatic", r["slug"] == p_child and r["picked_unavailable"], r)
before = snapshot()
check("refused: a redacted item can't be chosen (bad_cover)", code_of(hobbies.set_cover, H["id"], loose) == "bad_cover")
check("refusal wrote nothing", snapshot() == before)
cards.undo(red.batch_id)
check("refused: unknown item (not_found)", code_of(hobbies.set_cover, H["id"], "no-such-item") == "not_found")
check("refused: an item with no picture (audio) (bad_cover)", code_of(hobbies.set_cover, H["id"], song) == "bad_cover")
check("refused: unknown hobby (not_found)", code_of(hobbies.set_cover, "no-such-hobby", loose) == "not_found")
cards.undo(res.batch_id)

# ---- 5. web ------------------------------------------------------------------------------------
before = snapshot()
resp = client.post(f"/api/hobby/{H['slug']}/cover", data={"cover_slug": p_old})
body = resp.json()
check("web set: 200, one hobby_cover row as the token", resp.status_code == 200
      and ops_of(body["batch_id"]) == [("hobby_cover", "token")], resp.text[:300])
check("web set: answer carries cover_url from the thumb route", body["cover"]["cover_url"] == f"/f/{p_old}/thumb"
      and body["cover"]["cover_picked"] is True, body.get("cover"))
api = {h["slug"]: h for h in client.get("/api/hobbies").json()}
h = api[H["slug"]]
check("GET /api/hobbies: resolved cover + picked", h["cover_url"] == f"/f/{p_old}/thumb" and h["cover_picked"] is True
      and h["cover_source"] == "picked" and h["cover_slug"] == p_old, {k: h.get(k) for k in h if k.startswith("cover")})
one = client.get(f"/api/hobby/{H['slug']}").json()
check("GET /api/hobby/{slug}: resolved cover too", one["cover_slug"] == p_old and one["cover_picked"] is True)
resp2 = client.post(f"/api/hobby/{H['slug']}/cover", data={"cover_slug": ""})
h = {x["slug"]: x for x in client.get("/api/hobbies").json()}[H["slug"]]
check("web clear: back to automatic (highlighted project), not picked",
      resp2.status_code == 200 and h["cover_picked"] is False and h["cover_source"] == "highlighted_project"
      and h["cover_project"] == star["slug"] and h["cover_url"] == f"/f/{p_child}/thumb", h)
bad = client.post(f"/api/hobby/{H['slug']}/cover", data={"cover_slug": song})
check("web: a picture-less item is 422 bad_cover", bad.status_code == 422 and bad.json()["error"]["code"] == "bad_cover",
      bad.text[:200])
bad = client.post(f"/api/hobby/{H['slug']}/cover", data={"cover_slug": "nope"})
check("web: unknown item is 404 not_found", bad.status_code == 404, bad.text[:200])
cards.undo(resp2.json()["batch_id"])
undo_ok("web set", body["batch_id"], before)

# ---- 6. MCP ------------------------------------------------------------------------------------
before = snapshot()
out = server.constructicon_update_hobby(hobby=H["slug"], cover_slug=p_new)
check("MCP set: ok, one hobby_cover row as mcp", out.get("ok") and ops_of(out["batch_id"]) == [("hobby_cover", "mcp")]
      and out["cover"]["cover_slug"] == p_new, out)
lst = {x["slug"]: x for x in server.constructicon_list_hobbies()}[H["slug"]]
check("MCP list_hobbies carries the resolved cover", lst["cover_slug"] == p_new and lst["cover_picked"] is True
      and lst["cover_source"] == "picked", lst)
out2 = server.constructicon_update_hobby(hobby=H["id"], clear_cover=True)
check("MCP clear: back to automatic", out2.get("ok") and out2["cover"]["cover_picked"] is False
      and out2["cover"]["cover_source"] == "highlighted_project", out2)
both = server.constructicon_update_hobby(hobby=H["slug"], cover_slug=p_new, clear_cover=True)
check("MCP: cover_slug + clear is refused", both.get("ok") is False, both)
nope = server.constructicon_update_hobby(hobby=H["slug"], cover_slug="nope")
check("MCP: unknown item is not_found", nope.get("ok") is False and nope["error"]["code"] == "not_found", nope)
dry = server.constructicon_update_hobby(hobby=H["slug"], cover_slug=p_old, dry_run=True)
alias = server.constructicon_set_hobby_cover(hobby=H["slug"], cover_slug=p_old, dry_run=True)
check("the #746 alias set_hobby_cover still answers (dry run)", alias.get("ok") and alias["dry_run"] is True, alias)
check("MCP dry run", dry.get("ok") and dry["dry_run"] is True)
cards.undo(out2["batch_id"])
undo_ok("MCP set", out["batch_id"], before)

# ---- 7. pages ----------------------------------------------------------------------------------
H2 = hobbies.create("Empty Shelf").data["hobby"]   # no projects, no cover
H3 = hobbies.create("Paused Thing", "inactive").data["hobby"]
busy = cards.create("Still busy", kind="project").data["card"]
hobbies.add_card(H3["id"], busy["id"])
flags = db.hobby_flags(H3["id"])
hobbies.set_cover(H["id"], loose)

page = client.get("/hobbies")
html = page.text
check("/hobbies: 200", page.status_code == 200, page.status_code)
check("/hobbies: no hardcoded placeholder any more", "project-cover-placeholder" not in html)
check("/hobbies: no inline script left (cards are server-rendered; JS is static/js/hobbies.js)",
      not re.search(r"<script nonce=[^>]*>\s*(?:async function loadHobbies|grid\.innerHTML)", html)
      and "/static/js/hobbies.js" in html)
cards_html = re.findall(r'<a class="cx cx-small cx-kind-hobby[^"]*" href="/hobby/([^"]+)".*?</a>', html, flags=re.S)
check("/hobbies: every hobby is a shared small hobby card", sorted(cards_html) == sorted([H["slug"], H2["slug"], H3["slug"]]),
      cards_html)
card_h = re.search(r'<a class="cx cx-small cx-kind-hobby[^"]*" href="/hobby/%s".*?</a>' % re.escape(H["slug"]), html, flags=re.S).group(0)
check("/hobbies: the chosen cover is an <img> from the thumb route", f'<img src="/f/{loose}/thumb"' in card_h, card_h[:400])
check("/hobbies: group code, activity, project count still shown",
      ">RCA<" in card_h and "Hobby · Active" in card_h and "3 projects" in card_h)
card_e = re.search(r'<a class="cx cx-small cx-kind-hobby[^"]*" href="/hobby/%s".*?</a>' % re.escape(H2["slug"]), html, flags=re.S).group(0)
check("/hobbies: a hobby with no cover gets the card's own empty art, no <img>",
      "cx-art-empty" in card_e and "<img" not in card_e)
card_p = re.search(r'<a class="cx cx-small cx-kind-hobby[^"]*" href="/hobby/%s".*?</a>' % re.escape(H3["slug"]), html, flags=re.S).group(0)
check("/hobbies: mismatch flags still shown (and an inactive hobby is dimmed)",
      bool(flags) and all(f["label"] in card_p for f in flags) and "cx-inactive" in card_p, (flags, card_p[-500:]))
check("/hobbies: no hardcoded colours from the old inline template", "#C9A227" not in html and "rgba(127,163,124" not in html)

frag = client.get("/api/hobbies/html")
check("/api/hobbies/html: 200, the same cards (drawer fragment)", frag.status_code == 200
      and f'<img src="/f/{loose}/thumb"' in frag.text and frag.text.count("cx-kind-hobby") == 3, frag.text[:300])
drawer = client.get("/").text
check("drawer: no inline template left, loads hobbies.js", "project-cover-placeholder" not in drawer
      and 'id="hobbies-drawer-grid"' in drawer and "/static/js/hobbies.js" in drawer)
hp = client.get(f"/hobby/{H['slug']}").text
check("hobby page: COVER group with Automatic + the choices, the chosen one checked",
      'data-group="cover"' in hp and 'name="hobby-cover" value=""' in hp
      and re.search(r'name="hobby-cover" value="%s" checked' % re.escape(loose), hp) is not None
      and f'value="{p_new}"' in hp, hp[hp.find('data-group="cover"'):][:600])
check("hobby page: its own card shows the chosen cover", re.search(
    r'<div class="detail-header-card">\s*<a class="cx cx-full cx-kind-hobby.*?<img src="/f/%s/thumb"' % re.escape(loose),
    hp, flags=re.S) is not None)
check("hobby page: the cover source is described", "Chosen by hand" in hp)
items.delete([loose])
hp = client.get(f"/hobby/{H['slug']}").text
check("hobby page: a deleted chosen cover says so and shows the automatic one",
      "can't be shown" in hp and f'<img src="/f/{p_child}/thumb"' in hp and f"/f/{loose}/thumb" not in hp)
idx = client.get("/hobbies").text
check("/hobbies: a deleted chosen cover is never a broken image (automatic one instead)",
      f"/f/{loose}/thumb" not in idx and f"/f/{p_child}/thumb" in idx)

# ---- 8. layering ---------------------------------------------------------------------------------
r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_layering.py")], capture_output=True, text=True)
check("check_layering passes", r.returncode == 0, r.stdout[-300:])

ctx.__exit__(None, None, None)
if FAILS:
    print(f"\nFAILED: {', '.join(FAILS)}")
print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
