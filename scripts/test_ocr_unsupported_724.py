#!/usr/bin/env python3
"""#724 part a: the "not compatible with OCR" state (ocr_status='unsupported').

Throwaway DB + storage (scripts/_testenv.py), the real app through TestClient, no server:

    python scripts/test_ocr_unsupported_724.py

Covers:
  * status transitions (ocr.run_ocr): a web link and a legacy .doc / .ppt settle 'unsupported' with the
    plain reason in type_metadata.ocr_note; a text source that read the file and found no text settles
    'done' with empty text; real errors stay 'failed' (a corrupt image, a .docx that isn't a zip, a
    stored file that's missing, a YouTube thumbnail that couldn't be fetched); set_ocr_status needs a
    note for 'unsupported' and clears it for any other status
  * the ocr_unsupported_724 migration: re-settles failed rows with nothing to read (web links), leaves
    genuine failures (image, YouTube, a typed file with a text reader), redacted rows, done rows and
    unreadable type_metadata alone, keeps the other type_metadata keys, is idempotent, is registered
  * the processing drawer: 'unsupported' is the settled state 'off' (with the note), not failed or pending
  * the lamps: an 'unsupported' entry (grey #6E6B57, "Not compatible with OCR") in BOTH OCR_LAMP_META
    maps (cards.js and object_detail.html), 'none' no longer red, the reason in the tooltip; the slim
    card payload carries ocr_note; ingest.js treats anything but 'failed' as settled
  * retry stays allowed: POST /api/image/<slug>/ocr and MCP constructicon_run_type_action(slug, "retry_ocr") on an unsupported item

Exits 1 if any check fails.
"""

import io
import json
import os
import re
import sqlite3
import struct
import sys
import time
import types
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("ocr-unsupported-724-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, card_payload, db, ingest, object_types, ocr, paths, thumbnails  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def q(sql, *args):
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def raw(slug):
    return q("SELECT ocr_status, extracted_text, type_metadata FROM capture_events WHERE slug = ?", slug)[0]


def state(slug):
    r = db.get_by_slug(slug)
    return r["ocr_status"], (r.get("type_metadata") or {}).get("ocr_note"), r["extracted_text"]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


try:
    from web import app as webapp
    from web.routes import items as web_items
except Exception as e:  # web stack not importable here: the box run covers it
    webapp = web_items = None
    print(f"SKIP web checks (web import failed: {e!r})")

client = None
if webapp is not None:
    client = _testenv.client(webapp.app, raise_server_exceptions=False)
    with client:  # real startup (init_db + migrations)
        pass
else:
    db.init_db()

ingest.run_in_thread = lambda fn, *a: None  # nothing runs OCR behind the test's back
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()


def mkfile(slug, data, ext, media_type, type_metadata=None):
    sf = f"{slug}{ext}"
    (paths.storage_dir() / sf).write_bytes(data)
    db.insert_upload(slug, f"{slug}{ext}", sf, "tester", media_type=media_type, file_size=len(data),
                     ocr_status="pending", type_metadata=type_metadata)
    return slug


def mklink(slug, url, media_type="url"):
    db.insert_content(slug, "tester", media_type, external_url=url)
    return slug


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def docx_bytes(text_xml="<w:body/>"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                                        'wordprocessingml/2006/main">' + text_xml + '</w:document>')
    return buf.getvalue()


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504  # an OLE header and nothing else: a broken file
# #724 part b: .doc and .ppt have readers now, so "nothing we can read" is an older format: a Word 6
# FIB (wIdent 0xA5DC, nFib 0x65) and a deck with no Current User stream (scripts/test_legacy_office_724b.py
# has the readers' own tests).
import make_type_samples  # noqa: E402  (scripts/: its hand-built OLE writer)
WORD6 = make_type_samples.ole_file({"WordDocument": struct.pack("<HH", 0xA5DC, 0x0065) + b"\x00" * 0x200})
PPT95 = make_type_samples.ole_file({"PowerPoint Document": b"\x00" * 64})
DOC_NOTE = "legacy .doc: Word 6/95 or older format, no readable text"
PPT_NOTE = "legacy .ppt: PowerPoint 95 or older format, no readable text"

# ============================================================================================
# 1. status transitions
# ============================================================================================
LINK = mklink("weblink", "https://cults3d.com/en/3d-model/some-thing")
DOC = mkfile("legacydoc", WORD6, ".doc", "word")
PPT = mkfile("legacyppt", PPT95, ".ppt", "powerpoint")
EMPTY_DOCX = mkfile("emptydocx", docx_bytes(), ".docx", "word")
TEXT_DOCX = mkfile("textdocx", docx_bytes("<w:body><w:p><w:r><w:t>Hello rover</w:t></w:r></w:p></w:body>"), ".docx", "word")
BAD_DOCX = mkfile("baddocx", b"this is not a zip file at all", ".docx", "word")
GONE_DOC = mkfile("gonedoc", OLE_MAGIC, ".doc", "word")
(paths.storage_dir() / "gonedoc.doc").unlink()
BAD_IMG = mkfile("badimg", b"definitely not a png, just bytes", ".png", "image")
YT = mklink("ytvideo", "https://www.youtube.com/watch?v=jNQXAC9IVRw", media_type="youtube")

for s in (LINK, DOC, PPT, EMPTY_DOCX, TEXT_DOCX, BAD_DOCX, GONE_DOC, BAD_IMG):
    ocr.run_ocr(s)
# a YouTube thumbnail that can't be fetched (no network here, but don't depend on that)
_real_ensure = thumbnails.ensure_thumbnail
thumbnails.ensure_thumbnail = lambda row, wait=None: False
try:
    ocr.run_ocr(YT)
finally:
    thumbnails.ensure_thumbnail = _real_ensure

check("a web link settles 'unsupported' (not failed)", state(LINK)[0] == "unsupported", state(LINK))
check("a web link's reason is plain", state(LINK)[1] == "web link: no readable text yet", state(LINK))
check("a legacy .doc settles 'unsupported' with its reason",
      state(DOC)[:2] == ("unsupported", DOC_NOTE), state(DOC))
check("a legacy .ppt settles 'unsupported' with its reason",
      state(PPT)[:2] == ("unsupported", PPT_NOTE), state(PPT))
check("a text source that read the file and found no text settles done, empty text",
      state(EMPTY_DOCX) == ("done", None, ""), state(EMPTY_DOCX))
check("a text source with text still settles done with it",
      state(TEXT_DOCX)[0] == "done" and "Hello rover" in (state(TEXT_DOCX)[2] or ""), state(TEXT_DOCX))
check("a .docx that isn't a zip (the reader raised) stays 'failed'", state(BAD_DOCX)[0] == "failed", state(BAD_DOCX))
check("a missing stored file stays 'failed' (not 'unsupported')", state(GONE_DOC)[0] == "failed", state(GONE_DOC))
check("a corrupt image stays 'failed'", state(BAD_IMG)[0] == "failed", state(BAD_IMG))
check("a YouTube video whose thumbnail couldn't be fetched stays 'failed'", state(YT)[0] == "failed", state(YT))
# #724 (b2): every failure says why ("never fail silently"); these used to carry no note
check("a missing stored file's failure says so", state(GONE_DOC)[1] == ocr.MISSING_FILE_NOTE, state(GONE_DOC))
check("a corrupt image's failure says it's corrupt, naming the file not the server path",
      (state(BAD_IMG)[1] or "").startswith("corrupt or unreadable .png: ")
      and str(paths.storage_dir()) not in (state(BAD_IMG)[1] or ""), state(BAD_IMG))
check("a YouTube video with no picture says no picture could be made",
      state(YT)[1] == ocr.NO_PICTURE_NOTE, state(YT))
check("a corrupt .docx's failure says so (#724 owner decision 2026-10-17)",
      (state(BAD_DOCX)[1] or "").startswith("corrupt or unreadable .docx: "), state(BAD_DOCX))

# set_ocr_status rules
try:
    db.set_ocr_status(LINK, "unsupported")
    check("'unsupported' without a note is refused", False)
except ValueError:
    check("'unsupported' without a note is refused", True)
db.set_ocr_status(LINK, "pending")
check("going back to pending clears the note", state(LINK)[:2] == ("pending", None), state(LINK))
db.set_ocr_status(LINK, "unsupported", note="web link: no readable text yet")
check("set_ocr_status writes status + note together", state(LINK)[:2] == ("unsupported", "web link: no readable text yet"))
db.set_ocr_status(LINK, "done")
check("any other status clears the note", state(LINK)[:2] == ("done", None), state(LINK))
db.set_ocr_status(LINK, "unsupported", note="web link: no readable text yet")

# registry helpers
spec_url = object_types.get_object_type("url")
check("registry: a web link has nothing to read", object_types.nothing_to_read({"media_type": "url"}, spec_url))
check("registry: an image never has 'nothing to read'",
      not object_types.nothing_to_read({"media_type": "image"}, object_types.get_object_type("image")))
check("registry: a YouTube link expects a picture",
      object_types.picture_expected({"external_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
                                    object_types.get_object_type("youtube")))
check("registry: a default note names the type",
      object_types.ocr_unsupported_note({"media_type": "markdown"}).endswith(": no readable text"),
      object_types.ocr_unsupported_note({"media_type": "markdown"}))

# ============================================================================================
# 2. the migration
# ============================================================================================
M_LINK = mklink("mig_link", "https://store.steampowered.com/app/1/")
M_LINK2 = mklink("mig_link2", "https://github.com/hooptiej/x")
M_IMG = mkfile("mig_img", png_bytes(), ".png", "image")
M_YT = mklink("mig_yt", "https://www.youtube.com/watch?v=jNQXAC9IVRw", media_type="youtube")
M_DOC = mkfile("mig_doc", OLE_MAGIC, ".doc", "word")  # has a text reader: re-run (Retry) decides, not the migration
M_RED = mklink("mig_redacted", "https://example.com/r")
M_DONE = mklink("mig_done", "https://example.com/d")
M_BADJSON = mklink("mig_badjson", "https://example.com/j")
for s in (M_LINK, M_LINK2, M_IMG, M_YT, M_DOC, M_RED, M_BADJSON):
    db.set_ocr_status(s, "failed")
db.set_ocr_status(M_DONE, "done")
with db.transaction() as tx:
    tx.conn.execute("UPDATE capture_events SET redacted = 1 WHERE slug = ?", (M_RED,))  # a link has no file to redact
    tx.conn.execute("UPDATE capture_events SET type_metadata = ? WHERE slug = ?",
                    (json.dumps({"keep_me": 1}), M_LINK2))
    tx.conn.execute("UPDATE capture_events SET type_metadata = ? WHERE slug = ?", ("not json {", M_BADJSON))

before = {r["slug"]: r for r in q("SELECT slug, ocr_status, type_metadata FROM capture_events")}
with db.transaction():
    db._mig_ocr_unsupported_724()
after = {r["slug"]: r for r in q("SELECT slug, ocr_status, type_metadata FROM capture_events")}
changed = sorted(s for s in after if after[s] != before[s])
check("migration: re-settles exactly the failed web links", changed == sorted([M_LINK, M_LINK2]), changed)
check("migration: a re-settled row reads 'unsupported' with the reason",
      state(M_LINK)[:2] == ("unsupported", "web link: no readable text yet"), state(M_LINK))
check("migration: other type_metadata keys are kept",
      json.loads(raw(M_LINK2)["type_metadata"]) == {"keep_me": 1, "ocr_note": "web link: no readable text yet"},
      raw(M_LINK2)["type_metadata"])
check("migration: a genuinely failed image stays failed", after[M_IMG]["ocr_status"] == "failed")
check("migration: a failed YouTube video (its thumbnail is the picture) stays failed", after[M_YT]["ocr_status"] == "failed")
check("migration: a failed typed file with a text reader stays failed (Retry re-decides it)",
      after[M_DOC]["ocr_status"] == "failed")
check("migration: a redacted row is left alone", after[M_RED]["ocr_status"] == "failed")
check("migration: a done row is untouched", after[M_DONE]["ocr_status"] == "done")
check("migration: unreadable type_metadata is left alone (still failed, bytes unchanged)",
      after[M_BADJSON] == before[M_BADJSON])
with db.transaction():
    db._mig_ocr_unsupported_724()
again = {r["slug"]: r for r in q("SELECT slug, ocr_status, type_metadata FROM capture_events")}
check("migration: idempotent (a second run changes nothing)", again == after)
check("migration: registered in MIGRATIONS", "ocr_unsupported_724" in [n for n, _ in db.MIGRATIONS])
check("migration: recorded once run by init_db", "ocr_unsupported_724" in db.applied_migrations())
with db.transaction() as tx:  # put the garbled fixture back so later reads of the whole table stay quiet
    tx.conn.execute("UPDATE capture_events SET type_metadata = '{}' WHERE slug = ?", (M_BADJSON,))

# ============================================================================================
# 3. the processing drawer
# ============================================================================================
if web_items is not None:
    def stage(slug):
        st = web_items._derive_processing_status(db.get_processing_rows_by_slugs([slug])[0])
        return st, {s["stage"]: s for s in st["stages"]}
    st, stages = stage(LINK)
    check("drawer: 'unsupported' is the settled state 'off'", stages["OCR"]["state"] == "off", stages)
    check("drawer: the reason rides along", stages["OCR"].get("note") == "web link: no readable text yet", stages)
    check("drawer: settled, not in flight, overall done", not st["in_flight"] and st["overall"] == "done", st)
    st, stages = stage(BAD_IMG)
    check("drawer: a real failure still reads failed", stages["OCR"]["state"] == "failed", st)  # (its caption stage is separate)
    j = client.get(f"/api/processing?session={LINK}").json()
    row = next((i for i in j["items"] if i["slug"] == LINK), None)
    check("drawer API: the link shows as settled", row is not None and row["overall"] == "done", j)
    drawer = read("web/templates/_processing_drawer.html")
    check("drawer template: 'off' has its own label and the note is the tooltip",
          "' off'" in drawer and "st.note" in drawer and "Not compatible with OCR" in drawer)

# ============================================================================================
# 4. the lamps (both OCR_LAMP_META maps), the payload, ingest.js
# ============================================================================================
GREY = "#6E6B57"


def lamp_map(src):
    m = re.search(r"OCR_LAMP_META\s*=\s*\{(.*?)\n\s*\};", src, re.S)
    out = {}
    for key, body in re.findall(r"^\s*(\w+):\s*\{([^}]*)\}", m.group(1) if m else "", re.M):
        out[key] = body
    return out


for rel in ("web/static/js/cards.js", "web/templates/object_detail.html"):
    lm = lamp_map(read(rel))
    check(f"{rel}: OCR_LAMP_META has an 'unsupported' entry", "unsupported" in lm, sorted(lm))
    check(f"{rel}: 'unsupported' is grey {GREY}", GREY in lm.get("unsupported", ""), lm.get("unsupported"))
    check(f"{rel}: 'unsupported' says 'Not compatible with OCR'", "Not compatible with OCR" in lm.get("unsupported", ""))
    check(f"{rel}: 'none' (not run yet) is no longer red", "#E24B4A" not in lm.get("none", "#E24B4A"), lm.get("none"))
    check(f"{rel}: 'failed' is still red", "#E24B4A" in lm.get("failed", ""), lm.get("failed"))
    check(f"{rel}: the reason (ocr_note) goes into the tooltip", "ocr_note" in read(rel) or "ocrNote" in read(rel))

slim = card_payload.card_item_public({"type_metadata": {"ocr_note": "web link: no readable text yet", "body": "x" * 50}})
check("payload: the slim card record carries ocr_note (and still drops the rest)",
      slim["type_metadata"] == {"ocr_note": "web link: no readable text yet"}, slim["type_metadata"])
ingest_js = read("web/static/js/ingest.js")
check("ingest.js: only 'failed' marks an upload ocr-failed (unsupported is settled)",
      ingest_js.count("finalStatus === 'failed' ? 'ocr-failed' : 'done'") >= 3)

if client is not None:
    j = client.get(f"/api/image/{LINK}").json()
    check("item API: ocr_status and the note are there for the card lamp",
          j["ocr_status"] == "unsupported" and (j.get("type_metadata") or {}).get("ocr_note"), j.get("type_metadata"))
    j = client.get(f"/api/image/{LINK}/text?limit=1").json()
    check("text API (the detail page's poll): carries ocr_note",
          j["ocr_status"] == "unsupported" and j.get("ocr_note") == "web link: no readable text yet", j)
    page = client.get(f"/object/{LINK}")
    check("object page renders with the note embedded",
          page.status_code == 200 and "web link: no readable text yet" in page.text, page.status_code)

# ============================================================================================
# 5. retry stays allowed
# ============================================================================================
if client is not None:
    r = client.post(f"/api/image/{LINK}/ocr")
    check("retry (web): allowed on an unsupported item", r.status_code == 200, (r.status_code, r.text[:200]))
    # TestClient runs the background task after the response: it settles again, honestly
    check("retry (web): re-runs and settles 'unsupported' again", state(LINK)[:2] == ("unsupported", "web link: no readable text yet"),
          state(LINK))

from mcp_server import server  # noqa: E402  (imported last on purpose: it sets the MCP role)
out = server.constructicon_run_type_action(DOC, "retry_ocr")
check("retry (MCP run_type_action retry_ocr): allowed on an unsupported item", isinstance(out, dict) and "error" not in out
      and out.get("ok") is True and out["item"].get("ocr_status") in ("pending", "unsupported"),
      out if not isinstance(out, dict) else out.get("error"))
for _ in range(100):
    if db.get_by_slug(DOC)["ocr_status"] != "pending":
        break
    time.sleep(0.1)
check("retry (MCP): the re-run settles 'unsupported' again", state(DOC)[:2] == ("unsupported", DOC_NOTE),
      state(DOC))

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
