#!/usr/bin/env python3
"""Self-contained check for captions-off and agent captioning (#585, #588, #587 item 5).

Throwaway DB + storage (scripts/_testenv.py), no server. Covers:
  * the processing view: with captions disabled the Caption stage reads "off" (settled, so the item
    leaves "processing" once OCR is done); enabled it still reads pending; a caption an agent wrote
    reads done either way
  * captions.needs_caption: what is listed (never captioned, failed/skipped) and what is not
    (described, captioned, still queued, redacted, not caption-capable, hidden by the policy)
  * captions.queue_skipped: refuses with `captions_disabled` when off; queues the right items when on
  * items.set_caption: suggestion lands in the review queue, accept=True also sets the description,
    one batch, undo reverts each, a re-suggestion re-opens a skipped one, bad input refused
  * the MCP tools (called directly): list_needs_caption, view (image content, size cap, bytes decode,
    not_viewable), set_caption, update(content_description=...)

    python scripts/test_captions_mcp.py

Exits 1 if any check fails.
"""

import io
import os
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("captions-mcp-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, captions, cards, db, ingest, items, paths, policy, storage  # noqa: E402
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None

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


def mk(slug, size=(8, 8), media_type="image", noisy=False):
    """An item with a real file and a stored thumbnail."""
    sf = f"{slug}.png"
    img = Image.effect_noise(size, 80).convert("RGB") if noisy else Image.new("RGB", size, (30, 90, 200))
    img.save(paths.storage_dir() / sf, "PNG")
    Image.new("RGB", (8, 8), (30, 90, 200)).save(storage.thumb_path_for(slug), "JPEG")
    db.insert_upload(slug, sf, sf, "tester", media_type=media_type)
    return slug


def tm(slug):
    return db.get_by_slug(slug)["type_metadata"]


def status(slug, st):
    db._update_content_metadata(slug, type_metadata={"auto_caption_status": st})


db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

# --- the fixtures ---------------------------------------------------------------------------
NEVER, FAILED, SKIPPED = mk("never"), mk("failed"), mk("skipped")
status(FAILED, "failed")
status(SKIPPED, "skipped")
DESCRIBED = mk("described")
items.update(DESCRIBED, content_description="already described")
DONE = mk("done")
db._update_content_metadata(DONE, type_metadata={"auto_caption": "a blue square", "auto_caption_status": "done"})
PENDING = mk("pending")
status(PENDING, "pending")
REDACTED = mk("redacted")
items.redact(REDACTED)
STL = mk("stlitem", media_type="stl")  # not caption-capable
BIG = mk("big", size=(2400, 1600), noisy=True)

# --- processing status (#585) ---------------------------------------------------------------
try:
    from web.routes import items as web_items
except Exception as e:  # web stack not importable here: the box run covers it
    web_items = None
    print(f"SKIP processing-status checks (web import failed: {e!r})")


def stage(slug):
    st = web_items._derive_processing_status(db.get_processing_rows_by_slugs([slug])[0])
    return st, {s["stage"]: s["state"] for s in st["stages"]}


if web_items is not None:
    # image rows have OCR too: settle OCR so only the caption stage decides
    for s in (NEVER, FAILED, DONE, PENDING):
        db.set_ocr_status(s, "done")
    captions.DISABLED = False
    st, stages = stage(NEVER)
    check("captions on: never-captioned image is still pending/processing (unchanged)",
          stages["Caption"] == "pending" and st["overall"] == "processing" and st["in_flight"], stages)
    check("captions on: failed stays failed", stage(FAILED)[1]["Caption"] == "failed")
    captions.DISABLED = True
    st, stages = stage(NEVER)
    check("captions off: the Caption stage is 'off', so the item is settled (not in flight)",
          stages["Caption"] == "off" and not st["in_flight"] and st["overall"] == "done", (st, stages))
    check("captions off: a stale 'pending' status is settled too", stage(PENDING)[1]["Caption"] == "off")
    check("captions off: a failed mark (the old workaround) is settled, not 'failed'",
          stage(FAILED)[0]["overall"] == "done")
    check("captions off: a caption that exists still reads done", stage(DONE)[1]["Caption"] == "done")
    captions.DISABLED = False

# --- needs_caption ---------------------------------------------------------------------------
slugs = lambda **kw: [r["slug"] for r in captions.needs_caption(**kw)]  # noqa: E731
got = set(slugs(limit=100))
check("needs_caption lists never-captioned, failed and skipped items", {NEVER, FAILED, SKIPPED, BIG} <= got, got)
check("...not described, captioned, queued/pending, redacted or non-capable ones",
      not ({DESCRIBED, DONE, PENDING, REDACTED, STL} & got), got)
check("include_failed=False leaves failed/skipped out", set(slugs(limit=100, include_failed=False)) == {NEVER, BIG})
check("limit is honoured", len(slugs(limit=1)) == 1)
check("count_needs_caption agrees with the unlimited list", captions.count_needs_caption() == len(got))
_can_view = policy.can_view
policy.can_view = lambda item, actor=None: item["slug"] != NEVER  # the policy hides one item
check("the item policy filters the list", NEVER not in slugs(limit=100))
policy.can_view = _can_view
check("a restricted type is never listed (browse clause)",
      "stlitem" not in slugs(limit=100) and db.list_needs_caption(["stl"], limit=10) is not None)

# --- queue_skipped (#585) --------------------------------------------------------------------
captions.DISABLED = True
check("queue_skipped refuses with captions_disabled when captions are off",
      code_of(captions.queue_skipped) == "captions_disabled")
check("...and queued nothing", q("SELECT COUNT(*) AS n FROM caption_queue")[0]["n"] == 0)
captions.DISABLED = False
r = captions.queue_skipped()
queued = {x["slug"] for x in q("SELECT slug FROM caption_queue")}
check("queue_skipped queues exactly the never-captioned/failed/skipped items", queued == {NEVER, FAILED, SKIPPED, BIG}, queued)
check("...marking each pending and reporting the count", r["queued"] == 4 and all(tm(s)["auto_caption_status"] == "pending" for s in queued), r)
check("a second click queues nothing new that is not already pending", captions.queue_skipped()["queued"] == 0)
db.dequeue_caption(NEVER), db.dequeue_caption(FAILED), db.dequeue_caption(SKIPPED), db.dequeue_caption(BIG)
for s in (NEVER, FAILED, SKIPPED, BIG):
    status(s, None)  # back to never captioned for the rest of the test
db._update_content_metadata(FAILED, type_metadata={"auto_caption_status": "failed"})
db._update_content_metadata(SKIPPED, type_metadata={"auto_caption_status": "skipped"})
check("queue_skipped(limit=1) queues only the newest one", captions.queue_skipped(limit=1)["queued"] == 1
      and len(q("SELECT slug FROM caption_queue")) == 1)
for r_ in q("SELECT slug FROM caption_queue"):
    db.dequeue_caption(r_["slug"])
    status(r_["slug"], None)
db._update_content_metadata(FAILED, type_metadata={"auto_caption_status": "failed"})
db._update_content_metadata(SKIPPED, type_metadata={"auto_caption_status": "skipped"})

# --- items.set_caption -----------------------------------------------------------------------
captions.DISABLED = True  # everything below must work with captions off
res = items.set_caption(NEVER, "  a blue square on a plain background  ")
m = tm(NEVER)
check("set_caption stores the suggestion (trimmed), status done, model mcp-agent",
      m["auto_caption"] == "a blue square on a plain background" and m["auto_caption_status"] == "done"
      and m["auto_caption_model"] == "mcp-agent", m)
check("...and leaves the description alone (accept=False)", not db.get_by_slug(NEVER)["content_description"])
check("...so it shows in the caption-review queue", NEVER in [r["slug"] for r in db.list_unaccepted_captions()])
check("...and is no longer listed as needing a caption", NEVER not in slugs(limit=100))
logged = db.get_change_rows(batch_id=res.batch_id)
check("ONE change-log row, op item_set_caption's update, actor owner-ui here", len(logged) == 1 and logged[0]["actor"] == "owner-ui", logged)
cards.undo(res.batch_id)
check("undo reverts it (no caption, status gone, not in review)",
      "auto_caption" not in tm(NEVER) or not tm(NEVER).get("auto_caption"))
check("...back in needs_caption", NEVER in slugs(limit=100))

res = items.set_caption(FAILED, "a screenshot of a login page", accept=True)
row = db.get_by_slug(FAILED)
m = row["type_metadata"]
check("accept=True also sets content_description", row["content_description"] == "a screenshot of a login page")
check("...and records the caption as used (like mark-used)",
      m["description_caption_model"] == "mcp-agent" and m["description_caption_used_at"] and m["description_caption_step_label"], m)
check("...so it is not in the review queue (accepted)", FAILED not in [r["slug"] for r in db.list_unaccepted_captions()])
cards.undo(res.batch_id)
row = db.get_by_slug(FAILED)
check("undo of accept=True reverts description and caption in one go",
      not row["content_description"] and row["type_metadata"].get("auto_caption_status") == "failed"
      and "auto_caption" not in row["type_metadata"], row["type_metadata"])

items.update(SKIPPED, type_metadata={"auto_caption": "old", "auto_caption_status": "done", "auto_caption_dismissed": True})
check("a dismissed suggestion is out of the review queue", SKIPPED not in [r["slug"] for r in db.list_unaccepted_captions()])
items.set_caption(SKIPPED, "a new suggestion")
check("a new agent caption re-opens it in the review queue", SKIPPED in [r["slug"] for r in db.list_unaccepted_captions()])

check("empty text refused (bad_caption)", code_of(items.set_caption, NEVER, "   ") == "bad_caption")
check("too long refused (bad_caption)", code_of(items.set_caption, NEVER, "x" * 2001) == "bad_caption")
check("redacted item refused", code_of(items.set_caption, REDACTED, "text") == "redacted")
check("unknown item is not_found", code_of(items.set_caption, "nope", "text") == "not_found")
dry = items.set_caption(NEVER, "dry run", dry_run=True)
check("dry_run writes nothing", dry.dry_run and not tm(NEVER).get("auto_caption"))

# --- the MCP tools ---------------------------------------------------------------------------
from mcp_server import server  # noqa: E402  (sets CONSTRUCTICON_ROLE=mcp; imported last on purpose)
from mcp.server.mcpserver import Image as McpImage  # noqa: E402

lst = server.constructicon_list_needs_caption(limit=100)
ls = {i["slug"]: i for i in lst["items"]}
check("list_needs_caption returns slug/name/type with context and total",
      NEVER in ls and set(ls[NEVER]) >= {"slug", "name", "type", "projects", "hobbies"} and lst["total"] == len(ls), lst)
check("...and excludes redacted, described and non-capable items", not ({REDACTED, DESCRIBED, STL} & set(ls)))

out = server.constructicon_view(BIG)
check("view returns [image content, text]", isinstance(out, list) and isinstance(out[0], McpImage) and isinstance(out[1], str), out)
if isinstance(out, list):
    content = out[0].to_image_content()
    import base64
    raw = base64.b64decode(content.data)
    img = Image.open(io.BytesIO(raw))
    img.load()
    check("...the image decodes, PNG or JPEG, long edge capped at 1024, bytes capped",
          content.mime_type in ("image/png", "image/jpeg") and max(img.size) <= 1024 and len(raw) <= 1_500_000
          and img.format in ("PNG", "JPEG"), (content.mime_type, img.size, len(raw)))
    check("...text block names the item and says OCR", BIG in out[1] and "OCR" in out[1], out[1])
    small = server.constructicon_view(BIG, size="thumb")
    check("size=thumb is the small picture", max(Image.open(io.BytesIO(base64.b64decode(small[0].to_image_content().data))).size) <= 400)
check("a flat PNG screenshot stays PNG (text stays readable)",
      server.constructicon_view(NEVER)[0].to_image_content().mime_type == "image/png")
check("view: bad size", server.constructicon_view(BIG, size="huge")["error"]["code"] == "bad_size")
check("view: unknown slug is not_found", server.constructicon_view("nope")["error"]["code"] == "not_found")
check("view: redacted is not_viewable", server.constructicon_view(REDACTED)["error"]["code"] == "not_viewable")
NOVIS = mk("novis", media_type="zip")  # an archive: no picture, and no thumbnail on disk
storage.thumb_path_for(NOVIS).unlink()
(paths.storage_dir() / f"{NOVIS}.png").write_bytes(b"PK\x03\x04 not an image")
check("view: an item with no visual is not_viewable", server.constructicon_view(NOVIS)["error"]["code"] == "not_viewable")
policy.can_view = lambda item, actor=None: False
check("view: goes through the item policy (refused as not_found)", server.constructicon_view(BIG)["error"]["code"] == "not_found")
check("set_caption tool: goes through the item policy", server.constructicon_set_caption(BIG, "x")["error"]["code"] == "not_found")
policy.can_view = _can_view

r1 = server.constructicon_set_caption(BIG, "a noisy grey texture")
check("set_caption tool (accept=False): suggestion only, in the review queue, actor mcp",
      r1["accepted"] is False and r1["in_review_queue"] and not r1["content_description"]
      and BIG in [r["slug"] for r in db.list_unaccepted_captions()]
      and db.get_change_rows(batch_id=r1["batch_id"])[0]["actor"] == "mcp", r1)
cards.undo(r1["batch_id"])
check("...undo reverts it", not tm(BIG).get("auto_caption"))
r2 = server.constructicon_set_caption(BIG, "a noisy grey texture", accept=True)
check("set_caption tool (accept=True): description set, not in review queue",
      r2["accepted"] is True and r2["content_description"] == "a noisy grey texture" and not r2["in_review_queue"], r2)
cards.undo(r2["batch_id"])
check("...undo reverts both", not db.get_by_slug(BIG)["content_description"] and not tm(BIG).get("auto_caption"))
check("set_caption tool: empty text is bad_caption", server.constructicon_set_caption(BIG, "")["error"]["code"] == "bad_caption")

items.update(NEVER, type_metadata={"keep": "me"})
u = server.constructicon_update(NEVER, content_description="Corrected title", type_metadata={"extra": "1"})
check("update(content_description=...) sets it, merges type_metadata, returns it",
      u["content_description"] == "Corrected title" and u["type_metadata"].get("keep") == "me" and u["type_metadata"]["extra"] == "1", u)
u = server.constructicon_update(NEVER, description="uploader note")
check("...and leaving it None changes nothing else", u["content_description"] == "Corrected title" and u["description"] == "uploader note")
u = server.constructicon_update(NEVER, content_description="")
check("...\"\" clears it", not u["content_description"])

ctx.__exit__(None, None, None)
print()
print("FAILED: %d" % len(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
