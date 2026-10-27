"""Self-contained check for #554's pages part (batch 5, "Every Kind Gets Its Own Room"):

  - per-kind page config (card_rules.page_config) instead of is_group switches;
  - the cover pick, the same everywhere: a card borrows a nested child's OR a family / collection
    member's cover (cards.set_cover, POST /api/projects/{id}/cover) and follows it; a file pick;
    one at a time (a file clears a borrowed card and back); every refusal says why (bad_cover with
    details.reason: not_related, no_cover, cover_loop, no_picture, redacted, hidden); undo;
  - a hobby borrows a member card's cover (hobby_settings.cover_project_id, cover_card=) and follows
    it; refusals; back to automatic; the MCP's cover_card;
  - renaming a hobby (hobbies.rename, POST /api/hobby/{id}/rename, MCP name=): the slug follows, the
    old address 308s to the new one (query kept), a name clash is 409, undo, both addresses keep working;
  - deleting a card answers with a batch_id whose undo brings it all back (the page's Undo bar);
  - pages: a family's members in the member layout with cover picks, an event leads with its date
    and write-up, the hobby page has no COVER dropdown; a viewer sees the picks greyed (aria-disabled,
    "editors") and the cover API refuses them with 403.

Throwaway DB, no server:  python scripts/test_rooms_554.py
"""

import io
import json
import os
import re
import secrets
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("rooms-554-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import actor, card_rules, cards, db, hobbies, ingest, items, paths, users  # noqa: E402
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None  # no background threads in a unit check

FAILS = []
BASE = "http://testhost.local"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def refusal(fn, *a, **kw):
    """(code, details.reason, message) of an AppError, or None."""
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code, (e.details or {}).get("reason"), e.message
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
    """Every table but the change log, the derived search index and the (never-undone) slug history."""
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                              "AND name NOT IN ('audit_log', 'hobby_slug_history') AND name NOT LIKE 'item_search%' "
                              "ORDER BY name"):
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({t})") if (r["type"] or "").upper() != "BLOB"]
            out[t] = sorted(json.dumps(dict(r), sort_keys=True, default=str)
                            for r in c.execute(f"SELECT {', '.join(cols)} FROM {t}"))
        return out
    finally:
        c.close()


def diff(a, b):
    return {t: (len(a[t]), len(b.get(t, []))) for t in a if a[t] != b.get(t)}


def mk(slug, media_type="image", ext="png"):
    sf = f"{slug}.{ext}"
    if media_type == "image":
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 200, 10)).save(buf, "PNG")
        data = buf.getvalue()
    else:
        data = b"ID3" + b"\0" * 64
    (paths.storage_dir() / sf).write_bytes(data)
    db.insert_upload(slug, f"{slug}.{ext}", sf, "tester", media_type=media_type)
    return slug


db.init_db()
users.limiter.reset()

# ---- 1. per-kind page config --------------------------------------------------------------------
print("--- 1. page config ---")
check("family: members in the member layout, can't nest", card_rules.page_config("family")["members"]
      and not card_rules.page_config("family")["nests"])
check("collection: members too", card_rules.page_config("collection")["members"])
ev = card_rules.page_config("event")
check("event: write-up leads, date banner, no members", ev["lead"] == "writeup" and ev["date_banner"] and not ev["members"])
check("project / thing / action: the default layout",
      all(card_rules.page_config(k) == card_rules.PAGE_DEFAULT for k in ("project", "thing", "action")))
check("every kind has a page config", set(card_rules.CARD_PAGES) == set(card_rules.KINDS))
cols = {r["name"] for r in q("PRAGMA table_info(hobby_settings)")}
check("hobby_settings.cover_project_id added by init_db", "cover_project_id" in cols, sorted(cols))
check("hobby_slug_history table exists", bool(q("SELECT name FROM sqlite_master WHERE name='hobby_slug_history'")))

ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()
from web import app as webapp  # noqa: E402
from mcp_server import server  # noqa: E402
client = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)

# ---- 2. fixture ---------------------------------------------------------------------------------
fam = cards.create("Joe Builds", kind="family").data["card"]
m1 = cards.create("Joe Jeep", kind="thing").data["card"]
m2 = cards.create("Joe Tank", kind="thing").data["card"]
proj = cards.create("Big Rig", kind="project").data["card"]
child = cards.create("Rig Cab", kind="thing", parent=proj["id"]).data["card"]
stranger = cards.create("Unrelated", kind="thing").data["card"]
for m in (m1, m2):
    cards.add_to_family(fam["id"], m["id"])
jeep_pic, tank_pic, cab_pic, rig_pic, fam_pic = mk("jeep-pic"), mk("tank-pic"), mk("cab-pic"), mk("rig-pic"), mk("fam-pic")
song = mk("a-song", media_type="audio", ext="mp3")
from core import membership  # noqa: E402
for card, slug in ((m1, jeep_pic), (m2, tank_pic), (child, cab_pic), (proj, rig_pic), (fam, fam_pic), (fam, song)):
    membership.add_files(card["id"], [slug], link_tag=False, merge_free_tags=False, auto_cover=False)
cards.set_cover(m1["id"], slug=jeep_pic)
cards.set_cover(child["id"], slug=cab_pic)

# ---- 3. card cover picks ------------------------------------------------------------------------
print("--- 3. card cover picks ---")
before = snapshot()
r = cards.set_cover(fam["id"], from_card=m1["id"])
check("a family borrows a MEMBER's cover (#554 widened from nested children)",
      r.batch_id and r.data["cover"] == jeep_pic and r.data["cover_project_id"] == m1["id"], r.data)
m1_new = mk("jeep-pic-2")
membership.add_files(m1["id"], [m1_new], link_tag=False, merge_free_tags=False, auto_cover=False)
cards.set_cover(m1["id"], slug=m1_new)
check("it follows the member's cover when that changes", db.resolve_project_cover_slug(db.get_project(fam["id"])) == m1_new)
ref = refusal(cards.set_cover, fam["id"], from_card=m2["id"])
check("a member with no cover yet is refused (no_cover), and nothing moved",
      ref and ref[1] == "no_cover" and db.get_project(fam["id"])["cover_project_id"] == m1["id"], ref)
cards.set_cover(m2["id"], slug=tank_pic)
r3 = cards.set_cover(fam["id"], from_card=m2["id"])
check("ticking another member moves the borrowed cover", r3.data["cover"] == tank_pic and r3.data["cover_project_id"] == m2["id"])
r4 = cards.set_cover(fam["id"], slug=fam_pic)
check("a file pick clears the borrowed card (one at a time)", r4.data["cover_slug"] == fam_pic and r4.data["cover_project_id"] is None)
again = cards.set_cover(fam["id"], slug=fam_pic)
check("the same pick again writes nothing", again.batch_id is None and again.warnings == ["Nothing changed."])
rp = cards.set_cover(proj["id"], from_card=child["id"])
check("a project still borrows a NESTED child's cover (#356)", rp.data["cover"] == cab_pic)

ref = refusal(cards.set_cover, fam["id"], from_card=stranger["id"])
check("refused: a card that isn't nested or a member (bad_cover, not_related)",
      ref and ref[:2] == ("bad_cover", "not_related") and "Unrelated" in ref[2] and "Nothing was changed" in ref[2], ref)
coverless = cards.create("No Pic Yet", kind="thing").data["card"]
cards.add_to_family(fam["id"], coverless["id"])
ref = refusal(cards.set_cover, fam["id"], from_card=coverless["id"])
check("refused: a member with no cover (no_cover, says to give it one first)",
      ref and ref[1] == "no_cover" and "Give it a cover first" in ref[2], ref)
execute("UPDATE projects SET cover_slug = NULL, cover_project_id = ? WHERE id = ?", proj["id"], child["id"])  # child borrows from parent
ref = refusal(cards.set_cover, proj["id"], from_card=child["id"])
check("refused: a loop (the child already borrows the parent's cover)", ref and ref[1] == "cover_loop", ref)
execute("UPDATE projects SET cover_slug = ?, cover_project_id = NULL WHERE id = ?", cab_pic, child["id"])
ref = refusal(cards.set_cover, fam["id"], slug=song)
check("refused: a file with no picture (no_picture)", ref and ref[1] == "no_picture", ref)
ref = refusal(cards.set_cover, fam["id"], slug="no-such-file")
check("refused: an unknown file (not_found)", ref and ref[0] == "not_found", ref)
ref = refusal(cards.set_cover, fam["id"])
check("refused: neither a file nor a card (bad_request)", ref and ref[0] == "bad_request", ref)
red = mk("red-pic")
items.redact(red)
ref = refusal(cards.set_cover, fam["id"], slug=red)
check("refused: a redacted file (redacted)", ref and ref[1] == "redacted", ref)

resp = client.post(f"/api/projects/{fam['id']}/cover", data={"card": str(m1["id"])}, headers=SAME)
body = resp.json()
check("web: POST /api/projects/{id}/cover borrows a member's cover, answers batch_id", resp.status_code == 200
      and body.get("batch_id") and body.get("cover") == m1_new, resp.text[:300])
resp = client.post(f"/api/projects/{fam['id']}/cover", data={"card": str(stranger["id"])}, headers=SAME)
check("web: a refused pick is 422 bad_cover with a reason and a message", resp.status_code == 422
      and resp.json()["error"]["code"] == "bad_cover" and resp.json()["error"]["details"]["reason"] == "not_related"
      and resp.json()["error"]["message"], resp.text[:300])
cards.undo(body["batch_id"])
check("undo of the web pick restores the file cover", db.get_project(fam["id"])["cover_slug"] == fam_pic)
out = server.constructicon_update_project(card=fam["slug"], fields={"cover_card": m2["slug"]}, dry_run=True)
check("MCP update_project(fields={cover_card}) borrows a member's cover (dry run)",
      out.get("ok") is not False and out.get("dry_run") and db.get_project(fam["id"])["cover_project_id"] is None, out)
out = server.constructicon_update_project(card=fam["slug"], fields={"cover_card": stranger["slug"]})
check("MCP: a refused cover_card says why", out.get("ok") is False and out["error"]["code"] == "bad_cover"
      and out["error"]["details"]["reason"] == "not_related", out)

# ---- 4. hobby borrows a member card's cover -----------------------------------------------------
print("--- 4. hobby cover from a card ---")
H = hobbies.create("Model Making").data["hobby"]
for c in (m1, m2, coverless):
    hobbies.add_card(H["id"], c["id"])
hb = snapshot()
res = hobbies.set_cover(H["id"], cover_card=m1["id"])
cov = cards.resolve_hobby_cover(db.get_hobby(H["id"]))
check("a hobby borrows a member card's cover (picked_card)", cov["slug"] == m1_new and cov["source"] == card_rules.HOBBY_COVER_CARD
      and cov["picked"] and cov["project"] == m1["slug"], cov)
newest = mk("jeep-pic-3")
membership.add_files(m1["id"], [newest], link_tag=False, merge_free_tags=False, auto_cover=False)
cards.set_cover(m1["id"], slug=newest)
check("and follows it", cards.resolve_hobby_cover(db.get_hobby(H["id"]))["slug"] == newest)
res2 = hobbies.set_cover(H["id"], cover_card=m2["id"])
check("ticking another card moves it", cards.resolve_hobby_cover(db.get_hobby(H["id"]))["slug"] == tank_pic)
loose = mk("loose-pic")
execute("INSERT OR IGNORE INTO post_tags (post_slug, tag_id) VALUES (?, ?)", loose, H["id"])
res3 = hobbies.set_cover(H["id"], loose)
st = db.get_hobby_settings(H["id"])
check("a file pick clears the borrowed card", st["cover_slug"] == loose and st["cover_project_id"] is None, st)
ref = refusal(hobbies.set_cover, H["id"], cover_card=stranger["id"])
check("refused: a card that isn't in the hobby (not_related)", ref and ref[1] == "not_related", ref)
ref = refusal(hobbies.set_cover, H["id"], cover_card=coverless["id"])
check("refused: a member with no cover (no_cover)", ref and ref[1] == "no_cover", ref)
ref = refusal(hobbies.set_cover, H["id"], loose, cover_card=m1["id"])
check("refused: a file AND a card", ref and ref[0] == "bad_request", ref)
hobbies.remove_card(H["id"], m2["id"])
hobbies.set_cover(H["id"], cover_card=m1["id"])
hobbies.remove_card(H["id"], m1["id"])
cov = cards.resolve_hobby_cover(db.get_hobby(H["id"]))
check("a borrowed card that leaves the hobby: automatic cover, flagged unavailable",
      cov["picked_unavailable"] and cov["source"] != card_rules.HOBBY_COVER_CARD, cov)
hobbies.add_card(H["id"], m1["id"])
hobbies.add_card(H["id"], m2["id"])
resp = client.post(f"/api/hobby/{H['id']}/cover", data={"cover_card": m2["slug"]}, headers=SAME)
check("web: POST /api/hobby/{id}/cover cover_card", resp.status_code == 200 and resp.json()["cover"]["cover_source"] == "picked_card"
      and resp.json()["cover"]["cover_url"] == f"/f/{tank_pic}/thumb", resp.text[:300])
resp = client.post(f"/api/hobby/{H['id']}/cover", data={"cover_slug": "", "cover_card": ""}, headers=SAME)
check("web: both blank = back to automatic", resp.status_code == 200 and resp.json()["cover"]["cover_picked"] is False, resp.text[:300])
out = server.constructicon_update_hobby(hobby=H["slug"], cover_card=m1["slug"])
check("MCP update_hobby(cover_card=)", out.get("ok") and out["cover"]["cover_source"] == "picked_card", out)
both = server.constructicon_update_hobby(hobby=H["slug"], cover_card=m1["slug"], clear_cover=True)
check("MCP: cover_card + clear_cover refused", both.get("ok") is False, both)

# ---- 5. rename a hobby --------------------------------------------------------------------------
print("--- 5. rename ---")
rb = snapshot()
old_slug = H["slug"]
rn = hobbies.rename(H["id"], "Scale Modelling")
newh = db.get_hobby(H["id"])
check("rename: new name and a slug made from it", newh["name"] == "Scale Modelling" and newh["slug"] == "scale-modelling"
      and rn.data["old_slug"] == old_slug and rn.data["url"] == "/hobby/scale-modelling", rn.data)
check("rename: one hobby_rename change-log row", [r["op"] for r in q("SELECT op FROM audit_log WHERE batch_id = ? AND op IS NOT NULL",
                                                                     rn.batch_id)] == ["hobby_rename"])
r = client.get(f"/hobby/{old_slug}?rev=all")
check("the old address 308s to the new one, query kept", r.status_code == 308
      and r.headers["location"] == "/hobby/scale-modelling?rev=all", (r.status_code, r.headers.get("location")))
check("the new address renders", client.get("/hobby/scale-modelling").status_code == 200)
again = hobbies.rename(H["id"], "Scale Modelling")
check("the same name again writes nothing", again.batch_id is None)
other = hobbies.create("Gardening").data["hobby"]
ref = refusal(hobbies.rename, H["id"], "gardening")
check("refused: a name another tag has, any case (409 hobby_name_conflict)", ref and ref[0] == "hobby_name_conflict", ref)
ref = refusal(hobbies.rename, H["id"], "   ")
check("refused: an empty name", ref and ref[0] == "bad_request", ref)
resp = client.post(f"/api/hobby/{H['id']}/rename", data={"name": "Gardening"}, headers=SAME)
check("web: the clash is a 409 with a message", resp.status_code == 409 and "already a tag" in resp.json()["error"]["message"])
cards.undo(rn.batch_id)
check("undo the rename: name and slug back", db.get_hobby(H["id"])["slug"] == old_slug and db.get_hobby(H["id"])["name"] == "Model Making")
check("after the undo the old address is live and the new one redirects to it",
      client.get(f"/hobby/{old_slug}").status_code == 200
      and client.get("/hobby/scale-modelling").headers.get("location") == f"/hobby/{old_slug}")
resp = client.post(f"/api/hobby/{H['id']}/rename", data={"name": "Kit Bashing"}, headers=SAME)
check("web rename: 200 with the new slug and url", resp.status_code == 200 and resp.json()["slug"] == "kit-bashing"
      and resp.json()["url"] == "/hobby/kit-bashing", resp.text[:300])
cards.undo(resp.json()["batch_id"])
out = server.constructicon_update_hobby(hobby=H["id"], name="Kit Bashing", dry_run=True)
check("MCP update_hobby(name=) dry run: answers, writes nothing", out.get("ok") and out["slug"] == "kit-bashing"
      and db.get_hobby(H["id"])["slug"] == old_slug, out)
out = server.constructicon_update_hobby(hobby=H["id"], name="Kit Bashing")
check("MCP rename", out.get("ok") and db.get_hobby(H["id"])["name"] == "Kit Bashing", out)
cards.undo(out["batch_id"])

# ---- 6. delete a card, undo it ------------------------------------------------------------------
print("--- 6. delete + undo ---")
gone = cards.create("Doomed Card", kind="project").data["card"]
membership.add_files(gone["id"], [mk("doomed-pic")], link_tag=False, merge_free_tags=False, auto_cover=False)
db_before = snapshot()
resp = client.post(f"/api/projects/{gone['id']}/delete", headers=SAME)
bid = resp.json().get("batch_id")
check("web delete answers a batch_id (the page's Undo bar)", resp.status_code == 200 and bid, resp.text[:200])
resp = client.post(f"/api/changes/{bid}/undo", headers=SAME)
check("undoing that batch brings the card back", resp.status_code == 200 and db.get_project(gone["id"]) is not None)
check("... and the database is exactly as before", snapshot() == db_before, diff(db_before, snapshot()))

# ---- 7. pages -----------------------------------------------------------------------------------
print("--- 7. pages ---")
fp = client.get(f"/project/{fam['slug']}").text
check("family page: a Members section in the member layout", 'id="members"' in fp and fp.count('class="hp-block') >= 2)
check("family page: each member has a cover pick (borrow), the stacks fan files have one too",
      f'data-cover-kind="card" data-cover-ref="{m1["id"]}"' in fp and 'data-cover-kind="item"' in fp
      and 'id="cover-pick-config"' in fp)
check("family page: a member's Remove button says family", "Remove from family" in fp)
check("family page: no Part of picker (families don't nest)", 'id="parent-project-select"' not in fp)
check("family page: no old cover JS left", "project-cover-selector-child" not in fp and "currentCoverSlug" not in fp)
pp = client.get(f"/project/{proj['slug']}").text
check("project page: the nested child beside the piles carries the cover pick, checked",
      re.search(r'data-cover-kind="card" data-cover-ref="%d"[^>]*checked' % child["id"], pp) is not None)
check("project page: Part of picker still there", 'id="parent-project-select"' in pp)
evc = cards.create("Track Day", kind="event").data["card"]
cards.update(evc["id"], start=1_600_000_000, end=1_600_086_400)
wslug = db.get_project(evc["id"])["writeup_slug"]
items.update(wslug, type_metadata={"body": "We raced all afternoon and the jeep won."}) if wslug else None
ep = client.get(f"/project/{evc['slug']}").text
i_date, i_hero = ep.find('id="card-date-banner"'), ep.find('class="dp-hero"')
i_write = ep.find("project-writeup-lead") if "project-writeup-lead" in ep else ep.find("project-writeup-missing")
check("event page: the date banner, then the write-up, then the card and its details",
      0 < i_date < i_write < i_hero, (i_date, i_write, i_hero))
check("event page: the date is shown", "2020" in ep[i_date:i_date + 300], ep[i_date:i_date + 300])
hp = client.get(f"/hobby/{H['slug']}").text
check("hobby page: no COVER dropdown, picks on its cards", 'name="hobby-cover"' not in hp and 'data-cover-kind="card"' in hp)
check("hobby page: the name is editable in IDENTITY", 'id="hobby-name-input"' in hp)

# ---- 8. a viewer: greyed picks, refused writes ----------------------------------------------------
print("--- 8. viewer ---")
pw = "rv-" + secrets.token_urlsafe(16)
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("rooms_viewer", pw, "viewer")
vc = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
check("viewer signs in", vc.post("/api/auth/login", json={"username": "rooms_viewer", "password": pw}, headers=SAME).status_code == 200)
vp = vc.get(f"/project/{fam['slug']}").text
picks = re.findall(r'<input type="radio" class="project-cover-selector cx-cover-pick[^>]*>', vp)
check("viewer: every cover pick is shown, greyed (is-locked + aria-disabled)", picks
      and all("is-locked" in p and 'aria-disabled="true"' in p for p in picks), picks[:2])
check("viewer: labelled with the role it needs", "&middot; editors" in vp and "needs the editor role" in vp)
cfg = json.loads(re.search(r'<script type="application/json" id="cover-pick-config"[^>]*>(.*?)</script>', vp, re.S).group(1))
check("viewer: the page config says why on click", cfg["can_edit"] is False and "editor" in cfg["locked_message"])
csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', vp).group(1)
resp = vc.post(f"/api/projects/{fam['id']}/cover", data={"card": str(m1["id"])}, headers={**SAME, "X-CSRF-Token": csrf})
check("viewer: the cover API refuses (403 forbidden, saying the role)", resp.status_code == 403
      and resp.json()["error"]["code"] == "forbidden", resp.text[:200])
vh = vc.get(f"/hobby/{H['slug']}").text
gcfg = json.loads(re.search(r'<script type="application/json" id="loose-config"[^>]*>(.*?)</script>', vh, re.S).group(1))
check("viewer: the loose grid's picks are greyed too (cards.js reads can_edit)", gcfg["cover_pick"]["can_edit"] is False)

ctx.__exit__(None, None, None)
if FAILS:
    print(f"\nFAILED: {', '.join(FAILS)}")
else:
    print("\nALL PASS")
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
