#!/usr/bin/env python3
"""Search reads run-together filenames (#762, "Camel case closed"). Throwaway DB, no server.

    python scripts/test_search_camel_762.py

In-process (scripts/_testenv.py: temp DB + storage), the real FastAPI app through TestClient and the
MCP tool function called directly. Covers:
  1. the issue's cases: `camino` finds ElCamino.jpeg (the CamelCase split, a word-start match);
     `el camino` finds Elcamino4.jpeg (no capital to split on: the filename fallback, flagged
     `matched: inside_filename`); `talon` finds OldTalonFR.jpeg; existing prefix behaviour is unchanged
     (the whole run-together word, the original filename, a plain name's ranking);
  2. the split itself (the view's filename column) for the issue's names, and a plain name adds nothing;
  3. the fallback runs only when word-start search found nothing, and says so on /api/search and MCP;
  4. visibility: a sensitive item, a brand asset and a redacted item are not found by the fallback
     for a viewer (nor the redacted one for an admin); the tag filter and the limit hold;
  5. `%` and `_` in a query are literal, never LIKE wildcards; accents fold on both sides;
  6. a write through a FRESH sqlite3 connection (no app setup at all) fires the triggers without error,
     and renaming a file re-indexes its split variant;
  7. the migration `search_filename_parts_762`: an old view is replaced at boot, the rebuild logs its
     count and time, and is idempotent;
  8. other callers keep their shape: db.search() without the flag never falls back (the gallery).
Exits 1 if any check fails.
"""

import contextlib
import io
import os
import re
import secrets
import sqlite3
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("search762-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, paths, tags, users  # noqa: E402
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


def api(query="", tags_csv="", client=None):
    r = (client or admin).get("/api/search", params={"query": query, "tags": tags_csv})
    return r.json() if r.status_code == 200 else {"status": r.status_code, "body": r.text[:200]}


def api_slugs(query="", tags_csv="", client=None):
    body = api(query, tags_csv, client)
    return [x["slug"] for x in body] if isinstance(body, list) else body


def api_matched(query, client=None):
    body = api(query, client=client)
    return {x["slug"]: x.get("matched") for x in body} if isinstance(body, list) else body


def mcp_search(query=None, tags_list=None):
    with actor.acting_as(actor.ACTOR_MCP):
        return mcp.constructicon_search(query=query, tags=tags_list)


def put(slug, filename, **kw):
    (paths.storage_dir() / f"{slug}.txt").write_text(f"{slug}\n")
    db.insert_upload(slug, filename, f"{slug}.txt", "tester", **kw)


def indexed_filename(slug):
    con = db.get_conn()
    try:
        r = con.execute("SELECT s.filename FROM item_search_src s JOIN capture_events ce ON ce.id = s.id "
                        "WHERE ce.slug = ?", (slug,)).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def drift():
    d = db.check_search_index()
    return d["missing"] or d["stale"]


# ---- fixtures -----------------------------------------------------------------------------------
put("elco4", "Elcamino4.jpeg")
# the issue's case exactly, while Elcamino4.jpeg is the only El Camino file (see the note below)
r0 = api_matched("el camino")
m0 = mcp_search("el camino")
put("elco", "ElCamino.jpeg")
put("elco-work", "ElCamino(work).jpeg")
put("talon", "OldTalonFR.jpeg")
put("logo", "HooptieJlogo3BigDragonly.png")
put("lildrag", "lildragclean1.jpg")
put("plain", "garden-shed_plans.png")
put("tuning", "tuning-notes.txt", description="Tuning the fans")

# ---- 1. the issue's four cases ------------------------------------------------------------------
print("--- 1. the issue's cases ---")
r = api_matched("camino")
check("'camino' finds ElCamino.jpeg (and ElCamino(work).jpeg) by word start", {"elco", "elco-work"} <= set(r), r)
check("... flagged word_start, not the fallback", r.get("elco") == "word_start", r)
check("'el camino' finds Elcamino4.jpeg through the filename fallback", r0 == {"elco4": "inside_filename"}, r0)
check("... flagged matched=inside_filename on the MCP search too",
      [(x["slug"], x.get("matched")) for x in m0] == [("elco4", "inside_filename")], m0)
r = api_matched("el camino")
check("'el camino' finds ElCamino.jpeg by word start (split: El Camino)", r.get("elco") == "word_start", r)
# Owner rule: the fallback runs ONLY when word start finds nothing. Once ElCamino.jpeg is there,
# 'el camino' has word-start hits, so Elcamino4.jpeg is not added (found by 'elcamino' / 'el camino 4').
check("with a word-start hit present, 'el camino' does not fall back (Elcamino4 not mixed in)",
      "elco4" not in r and "inside_filename" not in r.values(), r)
r = api_matched("el camino 4")
check("'el camino 4' finds Elcamino4.jpeg through the filename fallback", list(r) == ["elco4"], r)
check("... flagged matched=inside_filename on /api/search", r.get("elco4") == "inside_filename", r)
m = mcp_search("el camino 4")
check("... and on the MCP search", [x["slug"] for x in m] == ["elco4"] and m[0].get("matched") == "inside_filename",
      [(x["slug"], x.get("matched")) for x in m])
r = api_matched("talon")
check("'talon' finds OldTalonFR.jpeg", list(r) == ["talon"] and r["talon"] == "word_start", r)
check("'fr' finds OldTalonFR.jpeg (the trailing capitals are a word)", "talon" in api_slugs("fr"), api_slugs("fr"))
check("prefix unchanged: 'elcamino' still finds the run-together names",
      {"elco", "elco4", "elco-work"} <= set(api_slugs("elcamino")), api_slugs("elcamino"))
check("prefix unchanged: 'elcam' (a word start) finds them too", {"elco", "elco4"} <= set(api_slugs("elcam")))
check("prefix unchanged: the whole original filename still finds it", api_slugs("ElCamino(work).jpeg") == ["elco-work"],
      api_slugs("ElCamino(work).jpeg"))
check("prefix unchanged: 'tun' finds 'Tuning' by word start", api_matched("tun").get("tuning") == "word_start")
check("prefix unchanged: 'shed plans' finds garden-shed_plans.png", api_slugs("shed plans") == ["plain"])
check("'dragonly' finds HooptieJlogo3BigDragonly.png (CamelCase split)", api_slugs("dragonly") == ["logo"])
check("'big dragon' finds it by word start", api_slugs("big dragon") == ["logo"])
r = api_matched("logo")
check("'logo' (inside 'Jlogo') finds it through the fallback, flagged", r == {"logo": "inside_filename"}, r)
r = api_matched("clean")
check("'clean' (inside lildragclean1) finds it through the fallback, flagged", r == {"lildrag": "inside_filename"}, r)

# ---- 2. the split in the index -----------------------------------------------------------------
print("--- 2. the split ---")
for slug, want in (("elco", "ElCamino.jpeg El Camino jpeg"), ("elco4", "Elcamino4.jpeg Elcamino 4 jpeg"),
                   ("elco-work", "ElCamino(work).jpeg El Camino work  jpeg"),
                   ("talon", "OldTalonFR.jpeg Old Talon FR jpeg"),
                   ("logo", "HooptieJlogo3BigDragonly.png Hooptie Jlogo 3 Big Dragonly png"),
                   ("lildrag", "lildragclean1.jpg lildragclean 1 jpg")):
    got = indexed_filename(slug)
    check(f"indexed filename for {slug}: {want!r}", got == want, got)
check("a plain name adds nothing (ranking unchanged)", indexed_filename("plain") == "garden-shed_plans.png",
      indexed_filename("plain"))
put("noname", None, media_type="youtube", external_url="https://youtu.be/x762", content_description="A video")
check("an item with no filename indexes an empty filename", indexed_filename("noname") == "", indexed_filename("noname"))
check("the index is in step", not drift(), db.check_search_index())

# ---- 3. the fallback runs only on nothing --------------------------------------------------------
print("--- 3. only when word start finds nothing ---")
check("no query: matched is null (the most recent items)",
      all(x.get("matched") is None for x in api("")) and len(api("")) > 0)
check("a query nothing matches at all is still empty (no 'everything')", api_slugs("zzqqxx") == [])
check("a needle under 3 letters doesn't fall back ('ra' is inside 'Dragonly', but too short)",
      api_slugs("ra") == [] and api_slugs("r a") == [], (api_slugs("ra"), api_slugs("r a")))
with actor.acting_as(actor.ACTOR_MCP):
    m = mcp.constructicon_search(query="talon")
check("MCP word-start results say matched=word_start", m and all(x.get("matched") == "word_start" for x in m),
      [x.get("matched") for x in m])

# ---- 4. visibility --------------------------------------------------------------------------------
print("--- 4. visibility ---")
pw = "s762-" + secrets.token_urlsafe(16)
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("s762_viewer", pw, "viewer")
users.limiter.reset()
viewer = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
r = viewer.post("/api/auth/login", json={"username": "s762_viewer", "password": pw}, headers=SAME)
check("the viewer signs in", r.status_code == 200, r.text[:200])
put("hid-flag", "Secretquokkavan.png", sensitive=True)
put("hid-brand", "Brandquokkavan.png")
put("hid-redact", "Redactquokkavan.png")
put("hid-cert", "Certquokkavan.pem", media_type="certkey")
put("see-quokka", "Openquokkavan.png")
con = db.get_conn()
con.execute("UPDATE capture_events SET is_brand_asset = 1 WHERE slug = 'hid-brand'")
con.commit()
con.close()
db._mark_redacted("hid-redact")
check("'quokka van' has no word-start hit (so it falls back)", db.search(query="quokka van") == [])
r = api_matched("quokka van", client=viewer)
check("the viewer's fallback finds only the ordinary item", r == {"see-quokka": "inside_filename"}, r)
r = api_matched("quokka van")
check("an admin's fallback finds the sensitive one too, never the brand, redacted or restricted type",
      set(r) == {"see-quokka", "hid-flag"}, r)
check("db.search(include_brand=True) brings the brand asset back in the fallback",
      "hid-brand" in [x["slug"] for x in db.search(query="quokka van", include_brand=True, filename_fallback=True)])
check("the viewer can't find the sensitive one by its own run-together name ('secretquokka')... ",
      api_slugs("secret quokka", client=viewer) == [], api_slugs("secret quokka", client=viewer))
with actor.acting_as(actor.ACTOR_SCRIPT):
    tags.attach("see-quokka", ["Wagonwheel762"])
check("the tag filter applies to the fallback (tagged item)", api_slugs("quokka van", "Wagonwheel762") == ["see-quokka"],
      api_slugs("quokka van", "Wagonwheel762"))
check("... and a tag nothing carries leaves the fallback empty", api_slugs("quokka van", "Nosuchtag762") == [])
for i in range(5):
    put(f"lim-{i}", f"Bulkzebrawagon{i}.png")
got = [x["slug"] for x in db.search(query="zebra wagon", limit=3, filename_fallback=True)]
check("the limit holds for the fallback (3 of 5), newest first", got == ["lim-4", "lim-3", "lim-2"], got)

# ---- 5. wildcards are literal, accents fold -------------------------------------------------------
print("--- 5. wildcards and accents ---")
for q in ("%", "_", "%%", "%_%", "e%o", "el_o", "c%mino"):
    got = api_slugs(q)
    check(f"query {q!r} is not a wildcard (finds nothing)", got == [], got)
got = api_matched("_lcamino4")
check("'_lcamino4': the '_' is punctuation, 'lcamino4' a literal substring of Elcamino4",
      got == {"elco4": "inside_filename"}, got)
got = api_matched("% camino4 %")
check("'% camino4 %': the '%' is punctuation, 'camino4' a literal substring", got == {"elco4": "inside_filename"}, got)
put("accent", "Crèmebrûlée7.png")
check("accents fold: 'creme brulee' finds Crèmebrûlée7.png", api_slugs("creme brulee") == ["accent"],
      api_slugs("creme brulee"))
check("accents fold in the query too: 'crème brûlée 7'", api_slugs("crème brûlée 7") == ["accent"],
      api_slugs("crème brûlée 7"))
check("case is ignored: 'ELCAMINO 4'", api_slugs("ELCAMINO 4") == ["elco4"], api_slugs("ELCAMINO 4"))

# ---- 6. fresh connections and renames -------------------------------------------------------------
print("--- 6. a fresh connection, a rename ---")
err = None
try:
    raw = sqlite3.connect(db.DB_PATH)  # nothing registered on it: the triggers must be pure SQL
    raw.execute("UPDATE capture_events SET filename = 'NewTalonBL.jpeg' WHERE slug = 'lildrag'")
    raw.execute("INSERT INTO capture_events (slug, filename, stored_filename, tech, timestamp, tags) "
                "VALUES ('rawrow', 'RawRowCamaro.png', NULL, 'tester', strftime('%s','now'), '[]')")
    raw.commit()
    raw.close()
except sqlite3.Error as e:
    err = f"{type(e).__name__}: {e}"
check("a write through a bare sqlite3 connection fires the triggers without error", err is None, err)
check("the renamed file is found by its new split word ('talon bl')", "lildrag" in api_slugs("talon bl"),
      api_slugs("talon bl"))
check("... and its new split variant is indexed", indexed_filename("lildrag") == "NewTalonBL.jpeg New Talon BL jpeg",
      indexed_filename("lildrag"))
check("... and no longer by its old name ('lildragclean')", "lildrag" not in api_slugs("lildragclean"))
check("the raw-inserted row is found by 'camaro'", api_slugs("camaro") == ["rawrow"], api_slugs("camaro"))
check("the index is in step after the raw writes", not drift(), db.check_search_index())

# ---- 7. the migration -----------------------------------------------------------------------------
print("--- 7. migration ---")
check("search_filename_parts_762 is registered and was applied at boot",
      "search_filename_parts_762" in [n for n, _ in db.MIGRATIONS] and
      "search_filename_parts_762" in db.applied_migrations())
before = {q: api_slugs(q) for q in ("camino", "talon", "el camino 4", "dragonly", "tuning")}
# an install that predates #762: the old view (no split) and an index built from it
old_view = db._SEARCH_VIEW.replace(db._FILENAME_PARTS_SQL, "''")
con = db.get_conn()
for name in db._search_sync_triggers():
    con.execute(f"DROP TRIGGER IF EXISTS {name}")
con.execute("DROP VIEW item_search_src")
con.execute(old_view)
con.execute("DELETE FROM item_search")
con.execute(f"INSERT INTO item_search(rowid, {db._SEARCH_COLS_SQL}) SELECT id, {db._SEARCH_COLS_SQL} FROM item_search_src")
con.execute("DELETE FROM schema_migrations WHERE name = 'search_filename_parts_762'")
con.commit()
con.close()
check("with the old view, 'talon' finds nothing by word start (the bug, reproduced)",
      db.search(query="talon") == [], [x["slug"] for x in db.search(query="talon")])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    db.init_db()
log = out.getvalue()
check("boot re-creates the view and runs the migration", "search_filename_parts_762" in db.applied_migrations(), log)
mm = re.search(r"search_filename_parts_762 re-indexed (\d+) item\(s\) in [0-9.]+s", log)
check("the migration logs its item count and time", mm is not None and int(mm.group(1)) > 0, log[-400:])
after = {q: api_slugs(q) for q in before}
check("after the migration every query answers exactly as before", after == before, (before, after))
rows1, _ = db.rebuild_search_index()
check("a second rebuild is idempotent", {q: api_slugs(q) for q in before} == before and rows1 == int(mm.group(1)))
check("the index is in step", not drift(), db.check_search_index())

# ---- 8. other callers keep their shape ------------------------------------------------------------
print("--- 8. other callers ---")
check("db.search() without the flag never falls back", db.search(query="el camino 4") == [])
check("db.search() rows carry no search_matched key on a word-start hit",
      all("search_matched" not in x for x in db.search(query="talon", filename_fallback=True)))
g = admin.get("/api/gallery", params={"query": "el camino 4"})
check("the gallery stays word-start (its totals agree with its items)",
      g.status_code == 200 and sum(x["total"] for x in g.json()) == sum(len(x["items"]) for x in g.json()) == 0, g.text[:300])
body = admin.get("/api/search", params={"query": "talon"})
check("/api/search is still a flat list of item dicts", body.status_code == 200 and isinstance(body.json(), list)
      and "slug" in body.json()[0] and "matched" in body.json()[0])

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
