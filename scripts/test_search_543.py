#!/usr/bin/env python3
"""Search finds items by every name, tag and card they have (#543). Throwaway DB, no server.

    python scripts/test_search_543.py

In-process (scripts/_testenv.py: temp DB + storage), the real FastAPI app through TestClient and the
MCP tool functions called directly. Covers:
  1. finding: by content_description (a YouTube-style title), by display_name, by the OLD filename after
     a rename, by a free-text tag name, by a tag-tree tag name, by a card title, by the uploader's note,
     by OCR text; word starts match ("tun" finds "Tuning"); words are ANDed; accents fold;
  2. staying current (the sync triggers): a newly added tag becomes findable at once and a removed one
     stops matching; a rename, a retitled card, a renamed tag, a deleted card and a deleted item are all
     reflected; the index never drifts (db.check_search_index) after any of it;
  3. ranking: a name or tag hit outranks a hit inside OCR text;
  4. visibility: a flagged-sensitive item, a restricted-type item and a redacted one are hidden from a
     signed-in viewer on /api/search, the MCP search and the gallery counts (and an admin keeps them
     where the policy says so); superseded revisions are still found but marked;
  5. shape: /api/search and the MCP search return the same item dicts as before (the keys of a public
     item, plus `rev`/`superseded_by` from revisions.decorate), a flat list;
  6. the tags filter and `limit` are applied in SQL (a tagged item older than `limit` newer untagged
     ones is still returned);
  7. /api/gallery's per-uploader "total" agrees with the items its own search lists, for a query that
     only a tag or a card title matches;
  8. the migration: reports a row count and a time, is idempotent (a second run, and running it again
     after its record is removed, leave identical results) and `db.rebuild_search_index()` is repeatable;
  9. odd input (quotes, `*`, `-`, `:`, parentheses, AND/OR/NEAR, unicode, emoji, a very long string,
     SQL-looking text, nothing but punctuation) never 500s: 200 and a (possibly empty) list, on the API,
     the MCP and the gallery.
Exits 1 if any check fails.
"""

import contextlib
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("search543-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, items, membership, paths, policy, revisions, tags, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
admin = _testenv.client(webapp.app, base_url=BASE)  # the install token: role admin
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)


# membership with NO side effects: the item gets no linked tag, so the card's title is the only way in
NO_TAG = {"link_tag": False, "merge_free_tags": False, "auto_cover": False}


def slugs_of(rows):
    return [r["slug"] for r in rows]


def api(query="", tags_csv="", client=None):
    c = client or admin
    r = c.get("/api/search", params={"query": query, "tags": tags_csv})
    return r


def api_slugs(query="", tags_csv="", client=None):
    r = api(query, tags_csv, client)
    return slugs_of(r.json()) if r.status_code == 200 else None


def mcp_slugs(query=None, tags_list=None):
    with actor.acting_as(actor.ACTOR_MCP):
        return slugs_of(mcp.constructicon_search(query=query, tags=tags_list))


def ids():
    return {s: i for s, i in db.get_conn().execute("SELECT slug, id FROM capture_events").fetchall()}


def put(slug, filename, description="", media_type="image", **kw):
    (paths.storage_dir() / f"{slug}.txt").write_text(f"{slug}\n")
    db.insert_upload(slug, filename, f"{slug}.txt", "tester", description=description, media_type=media_type, **kw)


def drift():
    d = db.check_search_index()
    return d["missing"] or d["stale"]


# ---- fixtures -----------------------------------------------------------------------------------
put("yt-tune", None, media_type="youtube", external_url="https://youtu.be/xx", content_description="Tuning the Gigabyte Aorus fans")
put("renamed", "IMG_4410_oldname.png", description="a plain note")
put("tagged", "tagged-file.png", tags=["Zebrafinch"])
put("plain", "plain-file.png", description="nothing special here")
put("ocr-only", "scan-one.png")
put("ocr-title", "Harvest-notes.png")
put("carded", "carded-file.png")
put("cafe", "cafe-menu.png", description="Café menu")
put("note", "note-file.png", description="warranty paperwork for the dishwasher")
db.set_extracted_text("ocr-only", "harvest harvest harvest the barley and the wheat")
db.set_extracted_text("ocr-title", "unrelated receipt text")

with actor.acting_as(actor.ACTOR_UI):
    items.update("renamed", display_name="Greenhouse Heater Install")
    cards.create("Quokka Workshop")
    CARD = next(p for p in db.list_projects() if p["title"] == "Quokka Workshop")
    membership.add_files(CARD["id"], ["carded"], **NO_TAG)
    tags.attach("plain", ["Wombat"])  # tag-tree tag only (post_tags), not the free-text column

# ---- 1. finding ---------------------------------------------------------------------------------
print("--- 1. finding ---")
check("content_description: 'Tuning' finds the YouTube-style title", "yt-tune" in api_slugs("Tuning"))
check("content_description: a word from the middle ('gigabyte')", "yt-tune" in api_slugs("gigabyte"))
check("word start: 'tun' finds 'Tuning'", "yt-tune" in api_slugs("tun"))
check("words are ANDed, any order: 'aorus tuning'", api_slugs("aorus tuning") == ["yt-tune"], api_slugs("aorus tuning"))
check("an extra word that is not there finds nothing: 'tuning zzzz'", api_slugs("tuning zzzz") == [])
check("display_name: the new name finds the item", api_slugs("greenhouse heater") == ["renamed"], api_slugs("greenhouse heater"))
check("the OLD filename still finds it after the rename ('oldname')", api_slugs("oldname") == ["renamed"], api_slugs("oldname"))
check("... and so does the whole old filename", api_slugs("IMG_4410_oldname.png") == ["renamed"], api_slugs("IMG_4410_oldname.png"))
check("free-text tag name finds the item", api_slugs("zebrafinch") == ["tagged"], api_slugs("zebrafinch"))
check("tag-tree tag name (post_tags only) finds the item", api_slugs("wombat") == ["plain"], api_slugs("wombat"))
check("card title finds the item on the card", "carded" in api_slugs("quokka"), api_slugs("quokka"))
check("the uploader's note still finds it ('dishwasher')", api_slugs("dishwasher") == ["note"], api_slugs("dishwasher"))
check("OCR text still finds it ('barley')", api_slugs("barley") == ["ocr-only"], api_slugs("barley"))
check("accents fold: 'cafe' finds 'Café'", api_slugs("cafe") == ["cafe"], api_slugs("cafe"))
check("accents fold the other way: 'Café' finds it", api_slugs("Café") == ["cafe"], api_slugs("Café"))
check("MCP search finds by content_description", "yt-tune" in mcp_slugs("Tuning"))
check("MCP search finds the old filename", mcp_slugs("oldname") == ["renamed"])
check("MCP search finds by tag-tree tag", mcp_slugs("wombat") == ["plain"])
check("MCP search finds by card title", "carded" in mcp_slugs("quokka"))

# ---- 2. staying current -------------------------------------------------------------------------
print("--- 2. staying current ---")
check("before: 'Fernlizard' finds nothing", api_slugs("fernlizard") == [])
with actor.acting_as(actor.ACTOR_UI):
    tags.attach("plain", ["Fernlizard"])
check("a newly attached tag-tree tag is findable at once", api_slugs("fernlizard") == ["plain"], api_slugs("fernlizard"))
with actor.acting_as(actor.ACTOR_UI):
    items.update("tagged", tags=["Zebrafinch", "Newtagword"])
check("a newly saved free-text tag is findable at once", api_slugs("newtagword") == ["tagged"], api_slugs("newtagword"))
with actor.acting_as(actor.ACTOR_UI):
    items.update("tagged", tags=["Zebrafinch"])
check("... and stops matching once removed", api_slugs("newtagword") == [], api_slugs("newtagword"))
with actor.acting_as(actor.ACTOR_UI):
    t = tags.find("Fernlizard")
    tags.detach("plain", t["id"])
check("a detached tag-tree tag stops matching", api_slugs("fernlizard") == [], api_slugs("fernlizard"))
con = db.get_conn()
con.execute("UPDATE blog_tags SET name = 'Wallaby' WHERE name = 'Wombat'")
con.commit()
con.close()
check("a renamed tag: the new name finds the item", api_slugs("wallaby") == ["plain"], api_slugs("wallaby"))
check("a renamed tag: the old name no longer does", api_slugs("wombat") == [], api_slugs("wombat"))
con = db.get_conn()
con.execute("UPDATE projects SET title = 'Numbat Atelier' WHERE id = ?", (CARD["id"],))
con.commit()
con.close()
check("a retitled card: the new title finds the item", "carded" in api_slugs("numbat"), api_slugs("numbat"))
check("a retitled card: the old title no longer does", "carded" not in api_slugs("quokka"), api_slugs("quokka"))
with actor.acting_as(actor.ACTOR_UI):
    items.update("renamed", display_name="Polytunnel Heater Install")
check("a second rename: the newest name finds it", api_slugs("polytunnel") == ["renamed"])
check("a second rename: the first name no longer does", api_slugs("greenhouse") == [], api_slugs("greenhouse"))
check("a second rename: the original filename still does", api_slugs("oldname") == ["renamed"])
with actor.acting_as(actor.ACTOR_UI):
    membership.remove_files(CARD["id"], ["carded"])
check("removed from the card: the card title stops matching it", "carded" not in api_slugs("numbat"), api_slugs("numbat"))
with actor.acting_as(actor.ACTOR_UI):
    membership.add_files(CARD["id"], ["carded"], **NO_TAG)
check("added again: the card title matches it again", "carded" in api_slugs("numbat"), api_slugs("numbat"))
db.set_extracted_text("note", "freshly scanned crocodile text")
check("new OCR text is findable", api_slugs("crocodile") == ["note"], api_slugs("crocodile"))
con = db.get_conn()
con.execute("DELETE FROM project_items WHERE project_id = ?", (CARD["id"],))
con.execute("DELETE FROM projects WHERE id = ?", (CARD["id"],))
con.commit()
con.close()
check("a deleted card: its title matches nothing", api_slugs("numbat") == [], api_slugs("numbat"))
check("the index is in step after all of that", not drift(), db.check_search_index())
db._delete_upload("note")
check("a deleted item drops out of search", api_slugs("dishwasher") == [], api_slugs("dishwasher"))
check("... and out of the index", not drift(), db.check_search_index())

# ---- 3. ranking ---------------------------------------------------------------------------------
print("--- 3. ranking ---")
got = api_slugs("harvest")
check("both 'harvest' items are found", set(got) == {"ocr-only", "ocr-title"}, got)
check("a filename hit outranks three mentions inside OCR text", got[:1] == ["ocr-title"], got)
put("rank-new", "rank-new.png", description="trellis")
put("rank-ocr", "rank-ocr.png")
db.set_extracted_text("rank-ocr", "trellis " * 5)
got = api_slugs("trellis")
check("a note hit outranks repeated OCR text", got and got[0] == "rank-new", got)

# ---- 4. visibility ------------------------------------------------------------------------------
print("--- 4. visibility ---")
pw = "s543-" + secrets.token_urlsafe(16)
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("s543_viewer", pw, "viewer")
users.limiter.reset()
viewer = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
r = viewer.post("/api/auth/login", json={"username": "s543_viewer", "password": pw}, headers=SAME)
check("the viewer signs in", r.status_code == 200, r.text[:200])
put("hid-flag", "hid-flag.png", description="Mulberryjam secret one", sensitive=True)
put("hid-cert", "hid-cert.pem", description="Mulberryjam secret two", media_type="certkey")
put("hid-redact", "hid-redact.png", description="Mulberryjam secret three")
put("see-me", "see-me.png", description="Mulberryjam visible")
db._mark_redacted("hid-redact")
check("an admin finds the ordinary item and the flagged one", set(api_slugs("mulberryjam")) >= {"see-me", "hid-flag"},
      api_slugs("mulberryjam"))
check("nobody finds the restricted type or the redacted item through search",
      not ({"hid-cert", "hid-redact"} & set(api_slugs("mulberryjam"))), api_slugs("mulberryjam"))
check("the viewer finds only the ordinary item (/api/search)", api_slugs("mulberryjam", client=viewer) == ["see-me"],
      api_slugs("mulberryjam", client=viewer))
check("... also by a word only the flagged item's note has ('one')... still hidden",
      api_slugs("secret one", client=viewer) == [], api_slugs("secret one", client=viewer))
tagged = db.get_conn()
tagged.execute("UPDATE capture_events SET tags = ? WHERE slug = 'hid-flag'", (json.dumps(["Quillbadger"]),))
tagged.commit()
tagged.close()
check("the viewer cannot find a flagged item by its tag", api_slugs("quillbadger", client=viewer) == [],
      api_slugs("quillbadger", client=viewer))
check("... nor with the tag filter", api_slugs("", "Quillbadger", client=viewer) == [], api_slugs("", "Quillbadger", client=viewer))
check("an admin can find it by tag", api_slugs("quillbadger") == ["hid-flag"], api_slugs("quillbadger"))
with actor.acting_as(actor.ACTOR_MCP):
    mcp_admin = slugs_of(mcp.constructicon_search(query="mulberryjam"))
check("MCP (admin actor) hides the restricted and redacted, shows the rest",
      set(mcp_admin) == {"see-me", "hid-flag"}, mcp_admin)
g = viewer.get("/api/gallery", params={"query": "mulberryjam"})
check("the viewer's gallery counts only what they can see",
      g.status_code == 200 and sum(x["total"] for x in g.json()) == 1 and
      sum(len(x["items"]) for x in g.json()) == 1, g.text[:300])
check("db.search(include_redacted=True) still enumerates the redacted one",
      "hid-redact" in slugs_of(db.search(query="mulberryjam", include_redacted=True)))

# superseded revisions: still found, marked
put("rev-old", "rev-old.png", description="Pangolin drawing v1")
put("rev-new", "rev-new.png", description="Pangolin drawing v2")
con = db.get_conn()
con.execute("INSERT INTO item_revisions (old_slug, new_slug, created_at) VALUES ('rev-old', 'rev-new', 1)")
con.commit()
con.close()
r = api("pangolin").json()
check("old and new revisions are both found", {x["slug"] for x in r} == {"rev-old", "rev-new"}, r)
check("the old one is marked superseded_by the new one",
      next(x for x in r if x["slug"] == "rev-old").get("superseded_by") == "rev-new")
check("include_superseded=False lists only the current revision",
      slugs_of(db.search(query="pangolin", include_superseded=False)) == ["rev-new"])

# ---- 5. shape -----------------------------------------------------------------------------------
print("--- 5. response shape ---")
from web.routes.items import _to_public  # noqa: E402
want_keys = set(_to_public(db.get_by_slug("yt-tune")).keys()) | {"rev", "superseded_by"}
r = api("tuning")
body = r.json()
check("/api/search is 200 and a flat list", r.status_code == 200 and isinstance(body, list) and len(body) == 1, r.text[:200])
check("each /api/search item has the public-item keys (nothing added, nothing dropped)",
      set(body[0].keys()) <= want_keys and set(_to_public(db.get_by_slug("yt-tune")).keys()) <= set(body[0].keys()),
      sorted(set(body[0].keys()) ^ want_keys))
with actor.acting_as(actor.ACTOR_MCP):
    m = mcp.constructicon_search(query="tuning")
mcp_keys = set(mcp._to_public(db.get_by_slug("yt-tune")).keys()) | {"rev", "superseded_by"}
check("the MCP search is a list of MCP public items (its own shape, unchanged)",
      isinstance(m, list) and len(m) == 1 and set(m[0].keys()) == mcp_keys, sorted(set(m[0].keys()) ^ mcp_keys) if m else m)
check("a row from db.search has the same columns as a capture_events row",
      set(db.search(query="tuning")[0].keys()) == set(db.get_by_slug("yt-tune").keys()))
check("no query, no tags: the newest items, newest first",
      slugs_of(db.search(limit=3)) == [r_["slug"] for r_ in sorted(db.search(limit=1000), key=lambda x: -x["timestamp"])[:3]])

# ---- 6. tags filter + limit in SQL --------------------------------------------------------------
print("--- 6. tags filter and limit ---")
base_ts = time.time()
put("tagfilter-old", "tagfilter-old.png", tags=["Ibexlist"])
for i in range(12):
    put(f"newer-{i}", f"newer-{i}.png")
con = db.get_conn()
con.execute("UPDATE capture_events SET timestamp = ? WHERE slug = 'tagfilter-old'", (base_ts - 100000,))
con.commit()
con.close()
check("tag filter finds an item older than `limit` newer untagged ones",
      slugs_of(db.search(tags=["Ibexlist"], limit=5)) == ["tagfilter-old"], slugs_of(db.search(tags=["Ibexlist"], limit=5)))
check("tag filter through /api/search", api_slugs("", "Ibexlist") == ["tagfilter-old"])
check("tag filter through MCP", mcp_slugs(None, ["Ibexlist"]) == ["tagfilter-old"])
check("tag filter is any-of: two tags", set(mcp_slugs(None, ["Ibexlist", "Zebrafinch"])) == {"tagfilter-old", "tagged"})
check("query AND tag filter", api_slugs("tagfilter", "Ibexlist") == ["tagfilter-old"] and api_slugs("tagfilter", "Zebrafinch") == [])
check("limit is applied: limit=4 gives 4 rows", len(db.search(limit=4)) == 4)
check("limit applies to a query too", len(db.search(query="newer", limit=4)) == 4)
con = db.get_conn()
con.execute("UPDATE capture_events SET tags = 'not json' WHERE slug = 'newer-0'")  # a corrupt row must not break the filter
con.commit()
con.close()
try:
    ok = slugs_of(db.search(tags=["Ibexlist"], limit=5)) == ["tagfilter-old"]
    detail = ""
except Exception as e:  # noqa: BLE001
    ok, detail = False, f"{type(e).__name__}: {e}"
check("a row with malformed tags JSON does not break the tag filter in SQL (it is simply not a match)", ok, detail)
con = db.get_conn()
con.execute("UPDATE capture_events SET tags = '[]' WHERE slug = 'newer-0'")
con.commit()
con.close()

# ---- 7. gallery totals agree --------------------------------------------------------------------
print("--- 7. gallery totals ---")
put("g-card", "g-card.png")
with actor.acting_as(actor.ACTOR_UI):
    cards.create("Capybara Garage")
    GC = next(p for p in db.list_projects() if p["title"] == "Capybara Garage")
    membership.add_files(GC["id"], ["g-card"], **NO_TAG)
g = admin.get("/api/gallery", params={"query": "capybara", "per_user": 50}).json()
total = sum(x["total"] for x in g)
listed = sum(len(x["items"]) for x in g)
check("a query that only a card title matches: the group totals equal the items listed", total == listed and total >= 1,
      (total, listed))
check("db.list_uploaders agrees with db.search on the same query",
      sum(u["total"] for u in db.list_uploaders(query="capybara")) == len(db.search(query="capybara")))

# ---- 8. the migration ---------------------------------------------------------------------------
print("--- 8. migration ---")
before = {q: api_slugs(q) for q in ("tuning", "oldname", "harvest", "mulberryjam", "capybara")}
con = db.get_conn()
con.execute("DELETE FROM schema_migrations WHERE name = 'search_index_543'")
con.commit()
con.close()
check("the migration shows as pending once its record is gone", "search_index_543" in db.pending_migrations())
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ran = db.run_pending_migrations()
out = buf.getvalue()
n_items = db.get_conn().execute("SELECT COUNT(*) FROM capture_events").fetchone()[0]
check("it ran exactly the one migration", ran == ["search_index_543"], ran)
m = re.search(r"search_index_543 indexed (\d+) item\(s\) in ([0-9.]+)s", out)
check("it reports a row count and a time", bool(m), out)
check("... the count is the number of items", bool(m) and int(m.group(1)) == n_items, (m.group(1) if m else None, n_items))
check("search gives identical results after re-running it", {q: api_slugs(q) for q in before} == before)
check("a second run applies nothing", db.run_pending_migrations() == [])
db.init_db()
check("a second init_db (as at every boot) changes nothing and applies nothing",
      db.pending_migrations() == [] and {q: api_slugs(q) for q in before} == before)
for _ in range(2):
    rows, secs = db.rebuild_search_index()
check("rebuild_search_index() is repeatable and reports (rows, seconds)", rows == n_items and secs >= 0, (rows, secs))
check("the index matches the items after rebuilding", not drift(), db.check_search_index())
# an index that has drifted (rows added while the triggers were absent) is healed by the rebuild
con = db.get_conn()
con.execute("DELETE FROM item_search")
con.commit()
con.close()
check("an emptied index is detected (every item missing)", len(db.check_search_index()["missing"]) == n_items)
db.rebuild_search_index()
check("... and the rebuild restores it", not drift() and {q: api_slugs(q) for q in before} == before)
con = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
n_trig = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'item_search_%'").fetchone()[0]
con.close()
check("the sync triggers exist exactly once each (init_db is idempotent)", n_trig == len(db._search_sync_triggers()), n_trig)

# ---- 9. odd input -------------------------------------------------------------------------------
print("--- 9. odd input ---")
ODD = ['"', "'", '"tuning', 'tun"ing', "tuning*", "*", "**", "-", "--", "-tuning", "tuning -gigabyte", "a:b", "NEAR(a b)",
       "tuning AND", "AND OR NOT", "(", ")", "((tuning)", "tuning)", "^tuning", "col:tuning", "{display_name}: tuning",
       "' OR 1=1 --", "'; DROP TABLE capture_events; --", "%", "_", "%_%", "\\", "\x00", "tun\x00ing", "\n\t ", "   ",
       "日本語のテスト", "Ünïcödé ñ", "😀", "tuning 😀", "ß", "İstanbul", "a" * 5000, "word " * 500, "/api/search?x=1",
       "<script>alert(1)</script>", "tuning, gigabyte", "1e999", "NULL", "..", "...", "@#$%^&*()"]
bad = []
for q in ODD:
    for label, call in (("api", lambda q=q: api(q)),
                        ("api+tag", lambda q=q: api(q, "x,y")),
                        ("gallery", lambda q=q: admin.get("/api/gallery", params={"query": q}))):
        try:
            r = call()
            if r.status_code != 200:
                bad.append((label, q[:30], r.status_code, r.text[:160]))
        except Exception as e:  # noqa: BLE001
            bad.append((label, q[:30], type(e).__name__, str(e)[:160]))
    try:
        with actor.acting_as(actor.ACTOR_MCP):
            res = mcp.constructicon_search(query=q)
        if not isinstance(res, list):
            bad.append(("mcp", q[:30], "not a list", str(res)[:160]))
    except Exception as e:  # noqa: BLE001
        bad.append(("mcp", q[:30], type(e).__name__, str(e)[:160]))
check(f"{len(ODD)} odd queries never error on the API, the gallery or the MCP (200 / a list)", not bad, bad[:5])
check("punctuation-only queries find nothing (not everything)", api_slugs("***") == [] and api_slugs('"') == [] and api_slugs("-") == [])
check("quotes around a real word still find it", "yt-tune" in api_slugs('"tuning"'))
check("a leading minus is just punctuation: '-tuning' finds 'tuning'", "yt-tune" in api_slugs("-tuning"))
check("AND/OR/NOT are plain words, never operators ('tuning AND' needs the word 'and')", api_slugs("tuning AND") == [])
check("odd input: the table survived the SQL-looking text", db.get_conn().execute("SELECT COUNT(*) FROM capture_events").fetchone()[0] > 0)
check("the index is still in step", not drift(), db.check_search_index())

# the error, if SQLite ever does refuse, names the query and the SQLite message (not a generic one)
real = db.get_conn
class _Boom:
    def __init__(self, c):
        self.c = c
    def execute(self, sql, *a):
        if "item_search MATCH" in sql:
            raise sqlite3.OperationalError("fts5: syntax error near \"x\"")
        return self.c.execute(sql, *a)
    def close(self):
        self.c.close()
db.get_conn = lambda: _Boom(real())
try:
    db.search(query="tuning")
    msg = None
except Exception as e:  # noqa: BLE001
    msg = e
finally:
    db.get_conn = real
check("a SQLite refusal surfaces as an AppError that quotes the query and SQLite's own message",
      msg is not None and getattr(msg, "code", "") == "search_failed" and "tuning" in str(msg) and "syntax error" in str(msg), repr(msg))

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
if FAILS:
    sys.exit(1)
