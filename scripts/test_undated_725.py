#!/usr/bin/env python3
"""Import date is not a birthday (#725). Throwaway DB, no server, no network.

    python scripts/test_undated_725.py

In-process (scripts/_testenv.py: temp DB + storage + exports), the web app through a TestClient with
the per-run install token and the MCP tool functions called directly. Covers:
  1. real dates only (the owner's test): a card holding real-dated items, an item with only an upload
     time and an item whose file mtime is from AFTER the install's first upload. The card's span
     covers only the real ones, and both undated items inherit exactly that span, approximately
     ("≈ <range> · from <card>"), with card_date_epoch = the range start. Never stored, never fed
     back into the span;
  2. which card lends: the card the item was first filed into; a card with no real dates lends
     nothing; a sensitive item's date never shapes the range others see;
  3. the Curator: needs_date only for undated items with no range to borrow (on a dateless card, or on
     none: the "Undated files" group), each with its best guess; hidden from an editor who can't see the
     item; Defer / Dismiss keys work;
  4. accepting a guess (POST /api/image/{slug}/date-guess): content_date + the recorded source, one
     undoable batch; refusals (already_dated 409, no_such_guess 400, bad_source 400) change nothing;
  5. items.apply_dates: fill-only (an item with a real date is skipped), ONE change-log row, one undo;
  6. the backfill migration: a date in a file name and a PDF's own CreationDate become content_date
     (source recorded), one batch, idempotent;
  7. upload: a name with a date gets content_date at upload; one without stays Undated;
  8. pages and MCP: the item page says "Imported" and the date it shows, the project page counts what
     the timeline leaves off, constructicon_undated lists / accepts / applies, its role rows exist.
Exits 1 if any check fails.
"""

import io
import json
import os
import sqlite3
import sys
from datetime import datetime

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("undated725-")
os.environ.setdefault("CAPTION_DISABLED", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from core import (actor, cards, curation_queue, date_guess, datefmt, db, item_dates, items,  # noqa: E402
                  membership, storage, timeline, users)
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def setcol(slug, **cols):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    try:
        for k, v in cols.items():
            c.execute(f"UPDATE capture_events SET {k} = ? WHERE slug = ?", (v, slug))
        c.commit()
    finally:
        c.close()


def local(*a):
    return timeline.source_datetime_to_epoch(datetime(*a))


T0 = local(2026, 9, 1, 12)          # the install's first upload
LATER = T0 + 20 * 86400             # a download date (after the first upload)


def item(slug, up_offset=3600, filename=None, **cols):
    db.insert_upload(slug, filename or f"{slug}.jpg", None, "tester", media_type="image", content_description=slug)
    setcol(slug, timestamp=T0 + up_offset, **cols)
    return slug


db.init_db()
with actor.acting_as(actor.ACTOR_SCRIPT):
    item("first", up_offset=0, content_date=local(2001, 1, 1))     # makes T0 the first upload
    A = item("real-a", content_date=local(2019, 3, 1))
    B = item("real-b", content_date=local(2020, 6, 1))
    C = item("real-c-mtime", source_modified_at=local(2019, 9, 9))  # a pre-install mtime: real
    U1 = item("upload-only")                                        # nothing but the upload time
    U2 = item("post-mtime", up_offset=21 * 86400, source_modified_at=LATER)  # a download date
    DESK = cards.create("Desk Build").data["card"]
    membership.add_files(DESK["id"], [A, B, C, U1, U2], link_tag=False, auto_cover=False)

# --- 1. real dates only ------------------------------------------------------------------------
print("--- 1. real dates only; undated items inherit the card's real range ---")
check("the cutoff is the install's first upload", db.first_upload_epoch() == T0, db.first_upload_epoch())
check("upload-only: no real date", timeline.resolve_item_date(db.get_by_slug(U1)) is None)
check("post-install mtime: no real date", timeline.resolve_item_date(db.get_by_slug(U2)) is None)
check("pre-install mtime: a real date", timeline.resolve_item_date(db.get_by_slug(C)) == local(2019, 9, 9))
span = timeline.resolve_project_span(db.get_project(DESK["id"]), db.list_project_items(DESK["id"]))
check("the card's span covers only the real dates (Mar 2019 .. Jun 2020)", span == (local(2019, 3, 1), local(2020, 6, 1)), span)
face = cards.card_face(DESK["id"])
check("the card face's span is those real ones; an active card keeps its open end ('- now', owner)",
      (face["effective_start"], face["effective_end"]) == span and face["dates"] == "Mar 2019 - now", face["dates"])
for slug in (U1, U2):
    lent = item_dates.lent_range(slug)
    check(f"{slug}: inherits EXACTLY the card's span", lent and (lent["start"], lent["end"]) == span
          and lent["card_slug"] == DESK["slug"], lent)
    info = item_dates.item_date(db.get_by_slug(slug))
    check(f"{slug}: shows it approximately, from the card", info["label"] == "≈ Mar 2019 - Jun 2020 · from Desk Build"
          and info["approx"] and info["epoch"] is None and info["sort_epoch"] == span[0], info)
from web import shapes  # noqa: E402
pub = shapes._to_public(db.get_by_slug(U1))
check("payload: card_date is the approximate label, card_date_epoch the range start",
      pub["card_date"].startswith("≈ ") and pub["card_date_epoch"] == span[0] and pub["card_date_approx"] is True, pub["card_date"])
check("never stored: the undated items' content_date is still empty",
      all(db.get_by_slug(s)["content_date"] is None for s in (U1, U2)))
span_again = timeline.resolve_project_span(db.get_project(DESK["id"]), db.list_project_items(DESK["id"]))
check("never fed back: the span is unchanged after the items inherited it", span_again == span)
check("list_project_items puts the undated last", [i["slug"] for i in db.list_project_items(DESK["id"])][-2:] in ([U1, U2], [U2, U1]))

# --- 2. which card lends --------------------------------------------------------------------------
print("--- 2. which card lends ---")
with actor.acting_as(actor.ACTOR_SCRIPT):
    X = cards.create("Card X").data["card"]
    Y = cards.create("Card Y").data["card"]
    EMPTY = cards.create("Empty Card").data["card"]
    Z = cards.create("Card Z").data["card"]
    item("x-real", content_date=local(2015, 5, 5))
    item("y-real", content_date=local(2021, 1, 1))
    M = item("multi")
    membership.add_files(X["id"], ["x-real"], link_tag=False, auto_cover=False)
    membership.add_files(Y["id"], ["y-real"], link_tag=False, auto_cover=False)
    membership.add_files(X["id"], [M], link_tag=False, auto_cover=False)   # first filed: X
    membership.add_files(Y["id"], [M], link_tag=False, auto_cover=False)
    E = item("on-empty-card")
    membership.add_files(EMPTY["id"], [E], link_tag=False, auto_cover=False)
    item("z-real", content_date=local(2016, 6, 6))
    item("z-secret", content_date=local(2005, 5, 5))
    ZU = item("z-undated")
    membership.add_files(Z["id"], ["z-real", "z-secret", ZU], link_tag=False, auto_cover=False)
    items.set_sensitive(["z-secret"], True)
    F = item("unfiled-ocr", filename="scan.png")
    setcol(F, extracted_text="Receipt. Date: May 12, 2019. Thank you.")
lent = item_dates.lent_range(M)
check("an item on two cards borrows from the card it was FIRST filed into", lent and lent["card_slug"] == X["slug"], lent)
check("a card with no real dates lends nothing", item_dates.lent_range(E) is None)
check("an unfiled item borrows nothing", item_dates.lent_range(F) is None)
lz = item_dates.lent_range(ZU)
check("a sensitive item's date doesn't shape the range others see (starts 2016, not 2005)",
      lz and lz["start"] == local(2016, 6, 6), lz)
check("an undated item with nothing to borrow says Undated, card_date_epoch null",
      shapes._to_public(db.get_by_slug(E))["card_date"] == "Undated" and shapes._to_public(db.get_by_slug(E))["card_date_epoch"] is None)

# --- 3. the Curator ------------------------------------------------------------------------------
print("--- 3. the Curator's needs_date ---")
with actor.acting_as(actor.ACTOR_SYSTEM):
    queue = curation_queue.build_queue()
date_items = {i["item_slug"]: (g["id"], i) for g in queue["groups"] for i in g["items"] if i["kind"] == "needs_date"}
check("needs_date for the item on a dateless card and the unfiled one", E in date_items and F in date_items, sorted(date_items))
check("no needs_date for items that inherit a range", not ({U1, U2, M, ZU} & set(date_items)), sorted(date_items))
empty_writeup = db.get_project(EMPTY["id"])["writeup_slug"]
check("a card's write-up (undated, on a dateless card) is never a needs_date",
      empty_writeup and empty_writeup not in date_items and timeline.resolve_item_date(db.get_by_slug(empty_writeup)) is None)
check("grouped under its (dateless) card / under Undated files",
      date_items.get(E, ("",))[0] == f"card:{EMPTY['slug']}" and date_items.get(F, ("",))[0] == "undated",
      {k: v[0] for k, v in date_items.items()})
fi = date_items.get(F, (None, {}))[1]
check("the unfiled one carries its best guess (the text's date) as the one-click answer",
      fi.get("accept_source") == "ocr_text" and fi["suggested"]["labels"][0].startswith("May 12, 2019"), fi.get("suggested"))
check("its key is a valid queue key", curation_queue.valid_key(fi.get("key", "")), fi.get("key"))
with actor.acting_as(actor.ACTOR_SCRIPT):
    item("hidden-undated")
    items.set_sensitive(["hidden-undated"], True)
    users.create_user("ed725", "a-long-password-725", "editor", "Editor 725")
curation_queue._cache.update(fp=None, q=None)
with actor.acting_as("user:ed725"):
    q_ed = curation_queue.cached_queue()
ed_slugs = {i.get("item_slug") for g in q_ed["groups"] for i in g["items"] if i["kind"] == "needs_date"}
check("an editor who can't see a sensitive item gets no needs_date about it", "hidden-undated" not in ed_slugs and F in ed_slugs,
      sorted(s for s in ed_slugs if s))
with actor.acting_as(actor.ACTOR_SCRIPT):
    item("hidden-on-empty")
    membership.add_files(EMPTY["id"], ["hidden-on-empty"], link_tag=False, auto_cover=False)
    items.set_sensitive(["hidden-on-empty"], True)
with actor.acting_as("user:ed725"):
    strip = curation_queue.build_queue(card=EMPTY["slug"])   # the project page's strip: no for_actor pass
strip_slugs = {i.get("item_slug") for g in strip["groups"] for i in g["items"]}
check("a card's own slice (built as the asker) leaves out a needs_date the asker can't see",
      "hidden-on-empty" not in strip_slugs and E in strip_slugs, sorted(s for s in strip_slugs if s))
with actor.acting_as(actor.ACTOR_SYSTEM):
    q_all = curation_queue.build_queue()
check("the admin's queue does list it", any(i.get("item_slug") == "hidden-undated" for g in q_all["groups"] for i in g["items"]))
counts_before = q_all["counts"]["needs"]

# --- 4. accept a guess over the web --------------------------------------------------------------
print("--- 4. POST /api/image/{slug}/date-guess ---")
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app)
r = client.post(f"/api/image/{F}/date-guess", data={"source": "ocr_text"})
body = r.json()
check("accept: 200, a batch_id, the date", r.status_code == 200 and body.get("batch_id") and body["date_display"] == "May 12, 2019", r.text[:300])
row = db.get_by_slug(F)
rec = (row.get("type_metadata") or {}).get("content_date_source") or {}
check("content_date is the guess, and where it came from is recorded",
      row["content_date"] == local(2019, 5, 12) and rec.get("source") == "ocr_text" and rec.get("accepted") == "by_hand"
      and rec.get("guessed") is True, (row["content_date"], rec))
check("one change-log row, op item_date_guess", [x["op"] for x in q("SELECT op FROM audit_log WHERE batch_id = ? AND op IS NOT NULL", body["batch_id"])] == ["item_date_guess"])
with actor.acting_as(actor.ACTOR_SYSTEM):
    q_after = curation_queue.build_queue()
check("it left the Curator", not any(i.get("item_slug") == F for g in q_after["groups"] for i in g["items"]))
u = client.post(f"/api/changes/{body['batch_id']}/undo")
row = db.get_by_slug(F)
check("undo: content_date and the recorded source are gone again", u.status_code == 200 and row["content_date"] is None
      and "content_date_source" not in (row.get("type_metadata") or {}), (u.status_code, u.text[:200]))
r = client.post(f"/api/image/{A}/date-guess", data={"source": "siblings"})
check("an item that already has a real date: 409 already_dated, nothing written",
      r.status_code == 409 and r.json()["error"]["code"] == "already_dated" and db.get_by_slug(A)["content_date"] == local(2019, 3, 1), r.text[:200])
r = client.post(f"/api/image/{E}/date-guess", data={"source": "filename"})
check("a source with no guess: 400 no_such_guess saying so", r.status_code == 400 and r.json()["error"]["code"] == "no_such_guess"
      and "no guesses" in r.json()["error"]["message"], r.text[:300])
r = client.post(f"/api/image/{E}/date-guess", data={"source": "astrology"})
check("an unknown source: 400 bad_source", r.status_code == 400 and r.json()["error"]["code"] == "bad_source", r.text[:200])
check("none of the refusals wrote a date", db.get_by_slug(E)["content_date"] is None)

# --- 5. apply_dates ------------------------------------------------------------------------------
print("--- 5. items.apply_dates: fill-only, one batch ---")
rec = {"source": "filename", "confidence": "high", "evidence": "t", "guessed": True, "accepted": "auto"}
with actor.acting_as(actor.ACTOR_SCRIPT):
    res = items.apply_dates([(A, local(2000, 1, 1), rec), (U1, local(2019, 4, 4), rec)])
check("an item with a real date is skipped, never overwritten", res.data["skipped"] == [{"slug": A, "reason": "already_dated"}]
      and db.get_by_slug(A)["content_date"] == local(2019, 3, 1), res.data)
check("the undated one is dated", res.data["applied"] == [U1] and db.get_by_slug(U1)["content_date"] == local(2019, 4, 4))
check("ONE change-log row for the batch", len(q("SELECT id FROM audit_log WHERE batch_id = ? AND op IS NOT NULL", res.batch_id)) == 1)
with actor.acting_as(actor.ACTOR_SCRIPT):
    cards.undo(res.batch_id)
check("one undo takes it back", db.get_by_slug(U1)["content_date"] is None)
try:
    with actor.acting_as(actor.ACTOR_SCRIPT):
        items.apply_dates([(U1, 5.0, rec)])
    check("an implausible date is refused", False)
except Exception as e:  # noqa: BLE001
    check("an implausible date is refused (bad_date), nothing written", getattr(e, "code", "") == "bad_date"
          and db.get_by_slug(U1)["content_date"] is None, repr(e))

# --- 6. the backfill migration ------------------------------------------------------------------
print("--- 6. the backfill migration ---")
import fitz  # noqa: E402  (PyMuPDF, in the app image)
doc = fitz.open()
doc.new_page()
doc.set_metadata({"creationDate": "D:20110203101112-07'00'"})
pdf_bytes = doc.tobytes()
doc.close()
_slug, stored = storage.save_file("manual.pdf", pdf_bytes)
db.insert_upload("pdf-undated", "manual.pdf", stored, "tester", media_type="pdf")
setcol("pdf-undated", timestamp=T0 + 7200)
item("name-dated", filename="IMG_20180704_101500.png")
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("DELETE FROM schema_migrations WHERE name = 'date_guess_backfill_725'")
c.commit()
c.close()
ran = db.run_pending_migrations()
pdf_row, name_row = db.get_by_slug("pdf-undated"), db.get_by_slug("name-dated")
check("the migration ran", "date_guess_backfill_725" in ran, ran)
check("the PDF's own CreationDate became its content_date (offset respected)",
      pdf_row["content_date"] == datetime(2011, 2, 3, 17, 11, 12).replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
      and pdf_row["type_metadata"]["content_date_source"]["source"] == "file_metadata", (pdf_row["content_date"], pdf_row["type_metadata"]))
check("the date in a file name became content_date (Mountain Time)",
      name_row["content_date"] == local(2018, 7, 4, 10, 15) and name_row["type_metadata"]["content_date_source"]["source"] == "filename")
batches = q("SELECT DISTINCT batch_id FROM audit_log WHERE op = 'item_date_guess' AND actor = 'migration'")
check("one batch, as the migration actor", len(batches) == 1, batches)
check("the suggest-only guesses were NOT applied (the OCR-dated item stays undated)", db.get_by_slug(F)["content_date"] is None)
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("DELETE FROM schema_migrations WHERE name = 'date_guess_backfill_725'")
c.commit()
c.close()
db.run_pending_migrations()
check("idempotent: a second run dates nothing new", len(q("SELECT DISTINCT batch_id FROM audit_log WHERE op = 'item_date_guess' AND actor = 'migration'")) == 1)

# --- 7. upload --------------------------------------------------------------------------------------
print("--- 7. upload ---")
from PIL import Image  # noqa: E402


def png(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


now_ms = str(int((T0 + 30 * 86400) * 1000))
r = client.post("/api/upload", files={"file": ("IMG_20170615_083000.png", png("red"), "image/png")}, data={"modified_at": now_ms})
up = r.json() if r.status_code == 200 else {}
row = db.get_by_slug(up.get("slug", "")) or {}
check("a date in the uploaded file's name becomes content_date at upload, source recorded",
      row.get("content_date") == local(2017, 6, 15, 8, 30) and (row.get("type_metadata") or {}).get("content_date_source", {}).get("source") == "filename",
      (r.status_code, row.get("content_date")))
r = client.post("/api/upload", files={"file": ("holiday.png", png("blue"), "image/png")}, data={"modified_at": now_ms})
up2 = r.json() if r.status_code == 200 else {}
row2 = db.get_by_slug(up2.get("slug", "")) or {}
check("no date anywhere real (a post-install mtime): it stays Undated", row2 and timeline.resolve_item_date(row2) is None
      and shapes._to_public(row2)["card_date"] == "Undated", (r.status_code, row2.get("source_modified_at")))

# --- 8. pages and MCP ----------------------------------------------------------------------------------
print("--- 8. pages and the MCP tool ---")
html = client.get(f"/object/{U2}").text
check("item page: 'Imported <date>' and the approximate date it shows", "Imported " in html and "≈ Mar 2019 - Jun 2020" in html)
check("item page: says it has no real date", "no real date" in html)
html = client.get(f"/project/{DESK['slug']}").text
check("project page: says how many undated files the timeline leaves off", "2 undated files not on the timeline" in html)
html = client.get(f"/project/{EMPTY['slug']}").text
check("project page of a dateless card: no 'None' where its dates go", "No real dates yet" in html and ">None <" not in html)
from mcp_server import server as S  # noqa: E402
with actor.acting_as(actor.ACTOR_MCP):
    lst = S.constructicon_undated()
    by = {e["slug"]: e for e in lst["items"]}
    check("MCP list: undated items with what they show and borrow", U1 in by and by[U1]["inherits"]["from_card"] == DESK["slug"]
          and by[U1]["needs_date"] is False and by[E]["needs_date"] is True, {k: (v["shows"], v["needs_date"]) for k, v in by.items()})
    check("MCP list: guesses listed for the OCR item", by.get(F, {}).get("guesses", [{}])[0].get("source") == "ocr_text")
    only = S.constructicon_undated(include_inherited=False)
    check("MCP list include_inherited=false: only the needs_date ones", all(e["needs_date"] for e in only["items"]) and U1 not in {e["slug"] for e in only["items"]})
    acc = S.constructicon_undated(action="accept", slug=F, source="ocr_text")
    check("MCP accept: dated, batch_id", acc.get("batch_id") and db.get_by_slug(F)["content_date"] == local(2019, 5, 12), acc)
    bad = S.constructicon_undated(action="accept", slug=F, source="ocr_text")
    check("MCP accept again: already_dated", isinstance(bad, dict) and bad.get("ok") is False and bad["error"]["code"] == "already_dated", bad)
    item("auto-later", filename="DSC_20160101_120000.jpg")
    dry = S.constructicon_undated(action="apply_auto", dry_run=True)
    check("MCP apply_auto dry run: lists the filename guess, writes nothing",
          any(a["slug"] == "auto-later" for a in dry["applied"]) and db.get_by_slug("auto-later")["content_date"] is None, dry)
    real = S.constructicon_undated(action="apply_auto")
    check("MCP apply_auto: writes it, one batch", db.get_by_slug("auto-later")["content_date"] == local(2016, 1, 1, 12) and real.get("batch_id"), real)
for action, role in (("list", "viewer"), ("accept", "editor"), ("apply_auto", "editor")):
    check(f"role row: constructicon_undated/{action} = {role}", S.ACTION_ROLES.get(("constructicon_undated", action)) == role)

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
