#!/usr/bin/env python3
"""Self-contained check for "Caption Over a Masterpiece" (#787, #743, the short-title part of #542).

Throwaway DB + storage (scripts/_testenv.py), no server, no model (the Ollama call is faked).
Covers:
  * #743: captions.starts_mid_word on the issue's examples (and what is NOT a fragment), so
    _is_garbage treats a fragment like an empty answer: the caption ladder moves on, and a ladder of
    fragments ends `failed` (needs a caption again), never `done`
  * #542: a caption comes with a short-title suggestion; title cleaning; insert-time and
    embedded-title short titles; the seed migration's rule (title-shaped descriptions only) and its
    undo; the search view re-created on an old DB
  * #787: set_caption refuses to replace a non-model suggestion / description / short title
    (would_replace_suggestion, details.existing holds the text), succeeds with replace=true, replaces
    a moondream draft freely, and always reports replaced_suggestion; a hand edit makes a model's
    text protected; the same over MCP and through the review page's accept route (409 then replace)
  * the mid-word scan (read-only) and the re-queue as one undoable batch
  * the short-title backfill: dry run, queue, the worker's proposals in the SAME batch, visible in
    the review list, one undo removes them and the worker then skips the rest

    python scripts/test_captions_titles_787_743.py

Prints a verdict line; exits 1 if any check fails.
"""

import io
import json
import os
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("captions-titles-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, captions, cards, db, ingest, item_title, items, paths  # noqa: E402
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
        return e
    return None


def mk(slug, filename=None):
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (20, 120, 20)).save(buf, "PNG")
    (paths.storage_dir() / sf).write_bytes(buf.getvalue())
    db.insert_upload(slug, filename or f"{slug}.png", sf, "tester", media_type="image")
    return slug


def tm(slug):
    return db.get_by_slug(slug)["type_metadata"]


# ---- #743: the fragment rule ------------------------------------------------------------------
for frag in ("urn with wires and a small black box on top.", "urns of wood and metal, a blue blanket",
             "ids and numbers on a black background.", "erso speedometer and odometer on a car.", "xtr.com",
             "\"urn with a lid\""):
    check(f"mid-word: {frag[:30]!r} is a fragment, so garbage", captions.starts_mid_word(frag) and captions._is_garbage(frag))
for ok in ("A turn with wires", "a red car on a table", "the image shows a desk", "iPhone on a desk", "eBay box",
           "(video) The blue chassis on carpet", "2 cars", "two-tone paint job", "Close-up of a gear", ""):
    check(f"not a fragment: {ok[:30]!r}", not captions.starts_mid_word(ok))
check("no letters is still garbage (#262)", captions._is_garbage("!!!!!!") and captions._is_garbage(""))

# ---- title cleaning ---------------------------------------------------------------------------
check("clean_short_title: quotes, label, trailing stop, spaces",
      item_title.clean_short_title('  Title: "Green  buggy on the berm."  ') == "Green buggy on the berm")
long_t = "word " * 40
cut = item_title.clean_short_title(long_t, limit=30)
check("clean_short_title cuts on a word boundary", cut and len(cut) <= 30 and not cut.endswith("wor"), cut)
check("title_shaped: one line, short", item_title.title_shaped("Short name") and not item_title.title_shaped("a\nb")
      and not item_title.title_shaped("x" * 81))

db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

# ---- the pipeline: fragment -> next rung; title suggestion -------------------------------------
captions.DISABLED = False
calls = []
_unloads = []
captions._ollama_post_json = lambda path, payload, timeout: _unloads.append(payload) or {}


def fake(answers):
    it = iter(answers)

    def caption_once(image_path, prompt=None, temperature=None, num_predict=None, label=None, keep_alive=0):
        calls.append((prompt, temperature, keep_alive))
        return {"caption": next(it), "elapsed_seconds": 0.0, "restarted": False, "restart_seconds": 0.0,
                "restart_reason": None, "error": None}
    captions.caption_once = caption_once


P1 = mk("pipe00001")
fake(["urn with wires and a small black box on top.", "A turntable with wires and a black box", "Turntable and box"])
captions._run_caption_now(P1)
m = tm(P1)
check("a fragment at step 0 moves to step 1 and that caption is stored as done",
      m.get("auto_caption") == "A turntable with wires and a black box" and m.get("auto_caption_status") == "done"
      and m.get("auto_caption_step") == 1, m)
check("the caption comes with a short title (#542)", m.get("auto_caption_title") == "Turntable and box"
      and m.get("auto_caption_title_model") == captions.OLLAMA_MODEL, m)
check("the caption call keeps the model warm for the title call, which is asked next; then it is unloaded",
      calls[-2][2] == captions.WARM_SECONDS and calls[-1][0] == captions.TITLE_PROMPT
      and _unloads and _unloads[-1].get("keep_alive") == 0, (calls, _unloads))
check("...a suggested title is in the review queue", P1 in [r["slug"] for r in db.list_unaccepted_captions()])
P2 = mk("pipe00002")
fake(["ids and numbers", "ids of things", "urns", "erso speedo"])
captions._run_caption_now(P2)
m = tm(P2)
check("a ladder of fragments ends failed, never done (re-queues: needs_caption lists it)",
      m.get("auto_caption_status") == "failed" and not m.get("auto_caption")
      and "mid-word" in (m.get("auto_caption_failed_reason") or "")
      and P2 in [r["slug"] for r in captions.needs_caption(limit=100)], m)
P3 = mk("pipe00003")
fake(["A desk with a lamp", "urn of", "ids"])
captions._run_caption_now(P3)
check("a garbage title is dropped, the caption still lands", tm(P3).get("auto_caption") == "A desk with a lamp"
      and not tm(P3).get("auto_caption_title"), tm(P3))

# ---- #787: set_caption never overwrites silently ---------------------------------------------
A = mk("agent0001")
r1 = items.set_caption(A, "The owner's favourite photo of his arcade machines, his last day owning them.")
check("first agent caption: replaced_suggestion is None", r1.data["replaced_suggestion"] is None, r1.data)
e = err_of(items.set_caption, A, "A short caption")
check("replacing an agent's suggestion is refused: would_replace_suggestion, 409",
      e is not None and e.code == "would_replace_suggestion" and e.status == 409, e and (e.code, e.status))
check("...details.existing holds the existing text",
      e is not None and "favourite photo" in e.details["existing"]["suggestion"]["text"], e and e.details)
check("...and nothing was written", "favourite photo" in tm(A)["auto_caption"])
r2 = items.set_caption(A, "A short caption", replace=True)
check("replace=True replaces it and reports the old text in replaced_suggestion",
      tm(A)["auto_caption"] == "A short caption" and "favourite photo" in (r2.data["replaced_suggestion"] or ""), r2.data)
cards.undo(r2.batch_id)
check("...and one undo brings the rich caption back", "favourite photo" in tm(A)["auto_caption"])
r3 = items.set_caption(P1, "An agent's better caption", title="Turntable rig")
check("a moondream draft is replaced freely, and reported", r3.data["replaced_suggestion"] == "A turntable with wires and a black box"
      and r3.data["replaced"].get("title_suggestion") == "Turntable and box", r3.data)
r4 = items.set_caption(A, "The owner's favourite photo of his arcade machines, his last day owning them.",
                       title="Turbo Outrun's last day", accept=True)
row = db.get_by_slug(A)
check("accept with title: description + short_title set; title_of is the short title",
      row["content_description"].startswith("The owner's favourite") and row["short_title"] == "Turbo Outrun's last day"
      and item_title.title_of(row) == "Turbo Outrun's last day" and r4.data["replaced_suggestion"] is None, (row["short_title"], r4.data))
e = err_of(items.set_caption, A, "Different text", accept=True)
check("an agent-accepted description is protected too", e is not None and e.code == "would_replace_suggestion"
      and "description" in e.details["existing"], e and e.details)
e = err_of(items.set_caption, A, None, title="Another name", accept=True)
check("...and so is its short title", e is not None and "short_title" in e.details["existing"], e and e.details)
H = mk("hand00001")
items.update(H, content_description="Typed by the owner", type_metadata={"auto_caption": "a draft", "auto_caption_model": "moondream",
                                                                      "description_caption_model": "moondream"})
items.update(H, content_description="Typed by the owner, edited")
check("a hand edit drops the recorded model (it is not a draft any more)", "description_caption_model" not in tm(H), tm(H))
e = err_of(items.set_caption, H, "Agent text", accept=True)
check("...so set_caption(accept) won't replace a hand-written description", e is not None and "description" in e.details["existing"])
check("bad title refused", err_of(items.set_caption, H, "x", title="t" * 200).code == "bad_title")
check("neither text nor title refused", err_of(items.set_caption, H, None).code == "bad_caption")

from mcp_server import server  # noqa: E402  (imported late: sets CONSTRUCTICON_ROLE=mcp)
out = server.constructicon_set_caption(A, "Short one")
check("MCP: refused as an error result with would_replace_suggestion and the existing text",
      out.get("ok") is False and out["error"]["code"] == "would_replace_suggestion"
      and "favourite photo" in json.dumps(out["error"]["details"]), out)
out = server.constructicon_set_caption(A, "Short one", replace=True)
check("MCP: replace=true succeeds and every answer carries replaced_suggestion",
      "replaced_suggestion" in out and "favourite photo" in (out["replaced_suggestion"] or ""), out)
out = server.constructicon_set_caption(P3, "A desk lamp, switched on", title="Desk lamp")
check("MCP: title= stores the title suggestion; replaced_suggestion reported for the moondream draft",
      out["replaced_suggestion"] == "A desk with a lamp" and tm(P3)["auto_caption_title"] == "Desk lamp" and out["in_review_queue"], out)
u = server.constructicon_update(P3, short_title="Lamp on desk")
check("MCP update(short_title=) sets the name", u["short_title"] == "Lamp on desk" and u["display_name"] == "Lamp on desk", u)
ctx.__exit__(None, None, None)

# ---- the review page's accept route -----------------------------------------------------------
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app)
R = mk("review001")
with actor.acting_as(actor.ACTOR_UI):
    items.set_caption(R, "An agent wrote this suggestion", title="Agent title")
lst = client.get("/api/captions/unreviewed").json()
it = next((x for x in lst if x["slug"] == R), None)
check("review list: the caption and its title, with who wrote them", it and it["kind"] == "caption"
      and it["title"] == "Agent title" and it["caption_by"] == "mcp-agent", it)
r = client.post(f"/api/captions/{R}/accept", data={"text": "An agent wrote this suggestion", "title": "Agent title"})
check("review accept of the unedited suggestion works (nothing replaced)", r.status_code == 200
      and r.json()["replaced_suggestion"] is None and db.get_by_slug(R)["short_title"] == "Agent title", r.text[:300])
R2 = mk("review002")
with actor.acting_as(actor.ACTOR_UI):
    items.set_caption(R2, "Agent suggestion text")
r = client.post(f"/api/captions/{R2}/accept", data={"text": "The owner edited it"})
body = r.json()
check("review accept that would replace an agent's suggestion: 409 would_replace_suggestion with the text",
      r.status_code == 409 and body["error"]["code"] == "would_replace_suggestion"
      and body["error"]["details"]["existing"]["suggestion"]["text"] == "Agent suggestion text", r.text[:300])
check("...nothing was written", not db.get_by_slug(R2)["content_description"])
r = client.post(f"/api/captions/{R2}/accept", data={"text": "The owner edited it", "replace": "true"})
check("...explicit replace works, recorded as the owner's", r.status_code == 200
      and db.get_by_slug(R2)["content_description"] == "The owner edited it"
      and tm(R2)["description_caption_model"] == captions.OWNER_MODEL, r.text[:300])
page = client.get("/captions/review")
check("review page renders with the replace flow", page.status_code == 200 and "would_replace_suggestion" in page.text
      and "Replace it" in page.text)

# ---- mid-word scan + re-queue ------------------------------------------------------------------
with actor.acting_as(actor.ACTOR_UI):
    M = mk("midword01")
    items.update(M, type_metadata={"auto_caption": "urn with wires", "auto_caption_status": "done", "auto_caption_model": "moondream"})
    scan = captions.scan_mid_word()
    check("scan finds the done mid-word caption (read-only)", M in [i["slug"] for i in scan["items"]]
          and scan["by_model"].get("moondream", 0) >= 1, scan)
    dry = captions.requeue_mid_word(dry_run=True)
    check("re-queue dry run writes nothing", dry["dry_run"] and M in dry["slugs"] and tm(M)["auto_caption_status"] == "done")
    res = captions.requeue_mid_word(dry_run=False)
    check("re-queue: failed, fragment kept aside, out of review, back in needs_caption, one batch",
          tm(M)["auto_caption_status"] == "failed" and tm(M)["auto_caption_rejected"] == "urn with wires"
          and not tm(M).get("auto_caption") and M in [r["slug"] for r in captions.needs_caption(limit=200)]
          and len(db.get_change_rows(batch_id=res["batch_id"])) == 1, (tm(M), res))
    cards.undo(res["batch_id"])
    check("...one undo puts it back as it was", tm(M)["auto_caption"] == "urn with wires" and tm(M)["auto_caption_status"] == "done")

# ---- the short-title backfill ------------------------------------------------------------------
with actor.acting_as(actor.ACTOR_UI):
    L1, L2 = mk("longcap01"), mk("longcap02")
    LONG = "Printed olive suspension links and a blue coil-over shock laid on a green tray next to a red chassis plate."
    for s_ in (L1, L2):
        items.set_caption(s_, LONG, accept=True)  # an agent's long accepted caption, no title
    check("a long accepted caption is NOT the title any more (the filename is)",
          item_title.title_of(db.get_by_slug(L1)) == "longcap01.png")
    plan = captions.propose_short_titles(dry_run=True)
    cands = [e["slug"] for e in plan["examples"]]
    check("backfill dry run lists the long-caption items, writes nothing",
          plan["dry_run"] and {L1, L2} <= set(cands) and A not in cands and not tm(L1).get("short_title_proposal_status"), plan)
    res = captions.propose_short_titles(dry_run=False)
    B = res["batch_id"]
    q = [dict(r) for r in db.get_conn().execute("SELECT * FROM caption_queue WHERE mode = 'title'").fetchall()]
    check("backfill queues title-mode proposals in one batch", B and {L1, L2} <= {r["slug"] for r in q}
          and all(r["batch_id"] == B for r in q) and tm(L1)["short_title_proposal_status"] == "queued", (res, q))
    db.enqueue_caption(P2)  # a real caption queued after the backfill still goes first
    check("a real caption is drained before any title proposal", db.peek_caption_queue()["slug"] == P2)
    db.dequeue_caption(P2)
    with actor.acting_as(actor.ACTOR_SYSTEM):
        nxt = db.peek_caption_queue()
        fake(["Olive suspension links"])
        captions._drain_one()
    first = nxt["slug"]
    check("the worker writes the proposal into the backfill's batch, never into short_title",
          tm(first).get("auto_caption_title") == "Olive suspension links" and tm(first)["short_title_proposal_status"] == "done"
          and not db.get_by_slug(first)["short_title"] and len(db.get_change_rows(batch_id=B)) == 2, tm(first))
    lst = client.get("/api/captions/unreviewed").json()
    pi = next((x for x in lst if x["slug"] == first), None)
    check("the proposal is in the review list as a title-only card with the description",
          pi and pi["kind"] == "title" and pi["title"] == "Olive suspension links" and pi["description"] == LONG, pi)
    cards.undo(B)
    check("one undo removes the proposal and the queued marks",
          not tm(first).get("auto_caption_title") and not tm(L1).get("short_title_proposal_status")
          and not tm(L2).get("short_title_proposal_status")
          and first not in [x["slug"] for x in client.get("/api/captions/unreviewed").json()])
    with actor.acting_as(actor.ACTOR_SYSTEM):
        rest = db.peek_caption_queue()
        fake(["Should not be used"])
        calls.clear()
        captions._drain_one()
    check("...and the worker then skips the rest (no model call, nothing written)",
          rest and not calls and not tm(rest["slug"]).get("auto_caption_title") and db.peek_caption_queue() is None, (rest, calls))
    r = client.post("/api/captions/propose-titles", data={"dry_run": "true"})
    check("admin route: propose-titles dry run answers the count", r.status_code == 200 and r.json()["dry_run"] is True, r.text[:200])
    r = client.get("/api/captions/mid-word")
    check("admin route: mid-word scan answers", r.status_code == 200 and "count" in r.json(), r.text[:200])
    captions.DISABLED = True
    e = err_of(captions.propose_short_titles, dry_run=False)
    check("backfill refuses with captions_disabled when captions are off", e is not None and e.code == "captions_disabled")

# ---- the seed migration and the search view ----------------------------------------------------
with actor.acting_as(actor.ACTOR_UI):
    S1, S2, S3 = mk("seed00001"), mk("seed00002"), mk("seed00003")
    conn = db.get_conn()
    conn.execute("UPDATE capture_events SET content_description = ?, short_title = NULL WHERE slug = ?", ("SuperClodbuster", S1))
    conn.execute("UPDATE capture_events SET content_description = ?, short_title = NULL WHERE slug = ?", (LONG, S2))
    conn.execute("UPDATE capture_events SET content_description = ?, short_title = NULL, type_metadata = ? WHERE slug = ?",
                 ("A green AlienWhoop build with a canopy, sitting on a wooden box marked ZERO for the photo.",
                  json.dumps({"description_caption_used_at": 1.0}), S3))
    conn.commit()
    conn.close()
with actor.acting_as(actor.ACTOR_MIGRATION):
    res = items.seed_short_titles()
check("seed: a title-shaped description becomes the short title, a long one doesn't, an accepted caption over 80 doesn't",
      db.get_by_slug(S1)["short_title"] == "SuperClodbuster" and not db.get_by_slug(S2)["short_title"]
      and not db.get_by_slug(S3)["short_title"] and S1 in res.data["seeded"], res.data)
with actor.acting_as(actor.ACTOR_MIGRATION):
    again = items.seed_short_titles()
check("seed is idempotent (fill-only)", S1 not in again.data["seeded"])
check("seed is ONE change-log row (op item_short_title_seed, actor migration)",
      [(r["op"], r["actor"]) for r in db.get_change_rows(batch_id=res.batch_id)] == [("item_short_title_seed", "migration")])

conn = db.get_conn()
conn.execute("DROP VIEW item_search_src")
conn.execute("CREATE VIEW item_search_src AS SELECT ce.id AS id, COALESCE(ce.display_name, '') AS display_name, "
             "COALESCE(ce.content_description, '') AS content_description, COALESCE(ce.filename, '') AS filename, "
             "'' AS tags, '' AS cards, COALESCE(ce.description, '') AS description, "
             "COALESCE(ce.extracted_text, '') AS extracted_text FROM capture_events ce")
conn.commit()
conn.close()
db.init_db()
conn = db.get_conn()
view = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'item_search_src'").fetchone()[0]
trig = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'item_search_ce_au'").fetchone()[0]
conn.close()
check("an old search view (and trigger) is re-created with short_title at boot", "short_title" in view and "short_title" in trig)
with actor.acting_as(actor.ACTOR_UI):
    items.update(S2, short_title="Zebrafinch kludge")
r = client.get("/api/search", params={"query": "zebrafinch"})
check("search finds the short title through the re-created view", r.status_code == 200 and any(x["slug"] == S2 for x in r.json()))

print()
print(f"FAILED: {', '.join(FAILS)}" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
