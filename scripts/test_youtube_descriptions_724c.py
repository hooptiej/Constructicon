#!/usr/bin/env python3
"""#724 part c: a YouTube video's stored description is searchable as its extracted text.

Throwaway DB + storage (scripts/_testenv.py), the real app through TestClient, no server, no
network (the thumbnail and tesseract are faked: the test decides what OCR "reads"):

    python scripts/test_youtube_descriptions_724c.py

Covers:
  * core/text_sources.py: combine() with N labelled sources (a future transcript too), headed
    sections with the picture's text last, text_source joined in order, OCR-only text unchanged
    (no header, no text_source); picture_text_of() recovers the picture's section; resettle()
  * a NEW item (POST /api/content with a description, and db.insert_content): OCR settles 'done'
    with "Description:" text, type_metadata.text_source = 'description', and a word found ONLY in
    the description is a search hit (db.search and GET /api/search)
  * combined sources: thumbnail text AND description -> both, under "Description:" / "Thumbnail
    text:", neither clobbering the other
  * an empty description and a thumbnail with no text -> grey 'unsupported', "video: no
    description or captions yet" (not failed); no description and no thumbnail stays 'failed'
  * never fail silently, never lose text: a thumbnail that can't be made, or tesseract raising,
    still settles 'done' with the description and a warning ocr_note
  * a description corrected after OCR settled (items.update): the text follows in the same
    change-log row, the thumbnail text is kept, search follows, one undo restores it all
  * the text_sources_724c backfill: rebuilds settled YouTube rows that have a description (keeps
    their stored OCR text as the thumbnail section), re-settles a row with no text at all as
    unsupported, leaves pending / failed / redacted / unreadable-JSON / non-YouTube rows alone, is
    idempotent, is registered and recorded; the FTS index follows (check_search_index in step)

Exits 1 if any check fails.
"""

import io
import json
import os
import sqlite3
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("youtube-descriptions-724c-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, cards, db, ingest, items, object_types, ocr, paths, text_sources  # noqa: E402
from core.text_sources import TextSource  # noqa: E402
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


def state(slug):
    r = db.get_by_slug(slug)
    tm = r.get("type_metadata") or {}
    return r["ocr_status"], tm.get("ocr_note"), tm.get("text_source"), r["extracted_text"] or ""


def hits(word):
    return sorted(r["slug"] for r in db.search(query=word, limit=1000))


try:
    from web import app as webapp
except Exception as e:  # web stack not importable here: the box run covers it
    webapp = None
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

# --- fake the thumbnail and tesseract: THUMB[slug] is what OCR "reads" off the thumbnail -------------
PNG = paths.storage_dir() / "_fake_thumb.png"
buf = io.BytesIO()
Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
PNG.write_bytes(buf.getvalue())
THUMB = {}         # slug -> OCR text of its thumbnail
NO_THUMB = set()   # slugs whose thumbnail can't be made
OCR_RAISES = set()  # slugs whose tesseract call raises
_current = {"slug": None}


def fake_source_path(row, spec):
    _current["slug"] = row["slug"]
    return None if row["slug"] in NO_THUMB else PNG


def fake_tesseract(img, timeout=None):
    if _current["slug"] in OCR_RAISES:
        raise RuntimeError("tesseract fell over")
    return THUMB.get(_current["slug"], "")


ocr._ocr_source_path = fake_source_path
ocr._load_for_ocr = lambda path, key=None: None
ocr.pytesseract.image_to_string = fake_tesseract
ocr._compute_similarity_signals = lambda *a, **k: None

YT_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
NOTE = "video: no description or captions yet"


def mkyt(slug, description=None, ocr_status="pending", extra_meta=None):
    tm = dict(extra_meta or {})
    if description is not None:
        tm["description"] = description
    db.insert_content(slug, "tester", "youtube", external_url=YT_URL, content_description=f"Video {slug}",
                      type_metadata=tm or None)
    if ocr_status != "pending":
        with db.transaction() as tx:
            tx.conn.execute("UPDATE capture_events SET ocr_status = ? WHERE slug = ?", (ocr_status, slug))
    return slug


# ============================================================================================
# 1. text_sources helpers
# ============================================================================================
d = TextSource("description", "Description", "  Built a SeaCrow.  ")
t = TextSource("transcript", "Transcript", "hello rover")
e = TextSource("transcript", "Transcript", "   ")
check("combine: one source, no picture -> its headed section, text_source = its key",
      text_sources.combine([d], "") == ("Description:\nBuilt a SeaCrow.", "description"), text_sources.combine([d], ""))
check("combine: source + picture -> both, picture last",
      text_sources.combine([d], " SPEED ") == ("Description:\nBuilt a SeaCrow.\n\nThumbnail text:\nSPEED", "description"))
check("combine: N sources (a future transcript) -> sections in order, keys joined with +",
      text_sources.combine([d, t], "X") ==
      ("Description:\nBuilt a SeaCrow.\n\nTranscript:\nhello rover\n\nThumbnail text:\nX", "description+transcript"))
check("combine: an empty source gets no section and no key", text_sources.combine([d, e], "") ==
      ("Description:\nBuilt a SeaCrow.", "description"))
check("combine: no source text -> OCR text alone, no header, no text_source",
      text_sources.combine([e], " just ocr ") == ("just ocr", None))
check("combine: nothing at all -> ('', None)", text_sources.combine([], "") == ("", None))
check("picture_text_of: no text_source -> the whole text is OCR's", text_sources.picture_text_of(" abc ", None) == "abc")
check("picture_text_of: recovers the thumbnail section",
      text_sources.picture_text_of("Description:\nx\n\nThumbnail text:\nSPEED", "description") == "SPEED")
check("picture_text_of: description only -> ''", text_sources.picture_text_of("Description:\nx", "description") == "")
check("labels_of: names the sources with text", text_sources.labels_of([d, t, e]) == "description and transcript")
check("youtube declares a text source", object_types.get_object_type("youtube").text_sources_fn is not None)

# ============================================================================================
# 2. a new item
# ============================================================================================
DESC_NEW = "Testing the Quokkatron hovercraft on the pond.\nMusic by nobody."
NEW = mkyt("yt_new", DESC_NEW)
ocr.run_ocr(NEW)
st = state(NEW)
check("new item: settles done", st[0] == "done", st)
check("new item: text_source = 'description'", st[2] == "description", st)
check("new item: extracted_text is the headed description", st[3] == "Description:\n" + DESC_NEW, st[3])
check("new item: no warning note", st[1] is None, st)
check("new item: a description-only word is a search hit", hits("Quokkatron") == [NEW], hits("Quokkatron"))
check("new item: word-start search works on description words", NEW in hits("hover"), hits("hover"))

if client is not None:
    THUMB_FOR_API = "GRAND PRIX"
    r = client.post("/api/content", data={"media_type": "youtube", "external_url": "https://youtu.be/abcdefgh123",
                                          "content_description": "API video",
                                          "type_metadata": json.dumps({"description": "The Zorblaxian rover drives."})})
    check("POST /api/content: created", r.status_code == 200, (r.status_code, r.text[:200]))
    api_slug = (r.json() or {}).get("slug") if r.status_code == 200 else None
    if api_slug:
        if db.get_by_slug(api_slug)["ocr_status"] == "pending":  # the background task may not have run it
            ocr.run_ocr(api_slug)
        st = state(api_slug)
        check("POST /api/content: description searchable after OCR",
              st[0] == "done" and st[2] == "description" and "Zorblaxian" in st[3], st)
        r2 = client.get("/api/search", params={"query": "Zorblaxian"})
        body = r2.json() if r2.status_code == 200 else {}
        found = [x.get("slug") for x in (body if isinstance(body, list) else body.get("results") or body.get("items") or [])]
        check("GET /api/search finds the description-only word", api_slug in found, (r2.status_code, str(body)[:300]))

# ============================================================================================
# 3. combined sources, empty, failures
# ============================================================================================
BOTH = mkyt("yt_both", "Infernal Robotics walker build, part 2.")
THUMB[BOTH] = "  EPIC WALKER  \n"
ocr.run_ocr(BOTH)
st = state(BOTH)
check("combined: both sources kept under headed sections",
      st[3] == "Description:\nInfernal Robotics walker build, part 2.\n\nThumbnail text:\nEPIC WALKER", st[3])
check("combined: text_source = 'description', done", st[:3] == ("done", None, "description"), st)
check("combined: description word and thumbnail word both hit", BOTH in hits("Infernal") and BOTH in hits("epic"))

OCRONLY = mkyt("yt_ocronly", None)
THUMB[OCRONLY] = "LOOP DE LOOP"
ocr.run_ocr(OCRONLY)
check("no description, thumbnail text: OCR text alone, no header, no text_source (as before)",
      state(OCRONLY) == ("done", None, None, "LOOP DE LOOP"), state(OCRONLY))

EMPTY = mkyt("yt_empty", "   ")
ocr.run_ocr(EMPTY)
check("empty description + no thumbnail text -> grey unsupported with the plain reason",
      state(EMPTY) == ("unsupported", NOTE, None, ""), state(EMPTY))
NODESC = mkyt("yt_nodesc", None)
ocr.run_ocr(NODESC)
check("no description key + no thumbnail text -> unsupported too", state(NODESC)[:2] == ("unsupported", NOTE), state(NODESC))

NOPIC_DESC = mkyt("yt_nopic_desc", "Vulture drone maiden flight.")
NO_THUMB.add(NOPIC_DESC)
ocr.run_ocr(NOPIC_DESC)
st = state(NOPIC_DESC)
check("thumbnail can't be made, description present: done with the description (text kept)",
      st[0] == "done" and st[3] == "Description:\nVulture drone maiden flight." and st[2] == "description", st)
check("... and the problem is said (warning note, not silent)",
      st[1] == f"{ocr.NO_PICTURE_NOTE}; the text is from the description only", st)

NOPIC = mkyt("yt_nopic", None)
NO_THUMB.add(NOPIC)
ocr.run_ocr(NOPIC)
check("thumbnail can't be made, no description: still a real failure with its reason",
      state(NOPIC)[:2] == ("failed", ocr.NO_PICTURE_NOTE), state(NOPIC))

RAISES = mkyt("yt_raises", "Kerbal walker of doom.")
OCR_RAISES.add(RAISES)
ocr.run_ocr(RAISES)
st = state(RAISES)
check("tesseract raises, description present: done with the description and the error as a warning",
      st[0] == "done" and "Kerbal walker" in st[3] and "tesseract fell over" in (st[1] or "")
      and (st[1] or "").endswith("; the text is from the description only"), st)
RAISES2 = mkyt("yt_raises2", None)
OCR_RAISES.add(RAISES2)
ocr.run_ocr(RAISES2)
check("tesseract raises, no description: failed (unchanged)", state(RAISES2)[0] == "failed", state(RAISES2))

ocr.run_ocr(BOTH)
check("re-running OCR (Retry) rebuilds the same text, no duplication",
      state(BOTH)[3] == "Description:\nInfernal Robotics walker build, part 2.\n\nThumbnail text:\nEPIC WALKER", state(BOTH))

# ============================================================================================
# 4. a description corrected after OCR settled (items.update)
# ============================================================================================
res = items.update(BOTH, type_metadata={"description": "Now a Pangolin crawler."})
st = state(BOTH)
check("update: text follows the new description, thumbnail text kept",
      st[3] == "Description:\nNow a Pangolin crawler.\n\nThumbnail text:\nEPIC WALKER", st[3])
check("update: search finds the new word, not the old one", BOTH in hits("Pangolin") and BOTH not in hits("Infernal"))
cards.undo(res.batch_id)
st = state(BOTH)
check("update: one undo restores the old text and search",
      st[3] == "Description:\nInfernal Robotics walker build, part 2.\n\nThumbnail text:\nEPIC WALKER"
      and BOTH in hits("Infernal") and BOTH not in hits("Pangolin"), st)

res = items.update(EMPTY, type_metadata={"description": "Finally described: Gizmoduck."})
check("update: an unsupported video given a description settles done",
      state(EMPTY)[:3] == ("done", None, "description") and EMPTY in hits("Gizmoduck"), state(EMPTY))
items.update(EMPTY, type_metadata={"description": ""})
check("update: description cleared again -> back to grey unsupported with the reason",
      state(EMPTY) == ("unsupported", NOTE, None, ""), state(EMPTY))
items.update(OCRONLY, type_metadata={"view_count": 5})
check("update: an unrelated key on a no-description video changes no text",
      state(OCRONLY) == ("done", None, None, "LOOP DE LOOP"), state(OCRONLY))
res = items.update(NEW, type_metadata={"description": "dry run words Xylophonic"}, dry_run=True)
check("update: dry_run writes nothing", "Xylophonic" not in state(NEW)[3] and not hits("Xylophonic"))

# ============================================================================================
# 5. the backfill (text_sources_724c)
# ============================================================================================
# rows as part a/b left them: OCR text only (no text_source), the description not indexed
B_BOTH = mkyt("b_both", "Infernal Robotics SeaCrow hull.", ocr_status="done")
B_DESC = mkyt("b_desc", "SeaCrow sea trials with a Vulture escort.", ocr_status="done")
B_OCR = mkyt("b_ocr", None, ocr_status="done")
B_NONE = mkyt("b_none", "", ocr_status="done")
B_UNSUP = mkyt("b_unsup", "Revived: the SeaCrow returns.", ocr_status="unsupported")
B_PEND = mkyt("b_pending", "SeaCrow pending row.")
B_FAIL = mkyt("b_failed", "SeaCrow failed row.", ocr_status="failed")
B_RED = mkyt("b_redacted", "SeaCrow redacted row.", ocr_status="done")
B_JSON = mkyt("b_badjson", None, ocr_status="done")
B_KEEP = mkyt("b_keep", "Keep my keys SeaCrow.", ocr_status="done", extra_meta={"view_count": 42, "auto_caption": "x"})
IMG = "b_image"
(paths.storage_dir() / "b_image.png").write_bytes(PNG.read_bytes())
db.insert_upload(IMG, "b_image.png", "b_image.png", "tester", media_type="image", file_size=10, ocr_status="done",
                 type_metadata={"description": "SeaCrow in an image's metadata"})
with db.transaction() as tx:
    c = tx.conn
    c.execute("UPDATE capture_events SET extracted_text = 'HULL NUMBER 7' WHERE slug = ?", (B_BOTH,))
    c.execute("UPDATE capture_events SET extracted_text = 'ONLY THUMB' WHERE slug = ?", (B_OCR,))
    c.execute("UPDATE capture_events SET extracted_text = 'kept text' WHERE slug = ?", (B_KEEP,))
    c.execute("UPDATE capture_events SET extracted_text = 'old text', redacted = 1 WHERE slug = ?", (B_RED,))
    c.execute("UPDATE capture_events SET type_metadata = 'not json {' WHERE slug = ?", (B_JSON,))
    c.execute("UPDATE capture_events SET type_metadata = json_set(type_metadata, '$.ocr_note', ?) WHERE slug = ?",
              ("video: no thumbnail or captions to read", B_UNSUP))
    c.execute("DELETE FROM schema_migrations WHERE name = 'text_sources_724c'")

COLS = "slug, ocr_status, extracted_text, type_metadata"
before = {r["slug"]: r for r in q(f"SELECT {COLS} FROM capture_events")}
seacrow_before = hits("SeaCrow")
check("backfill: before it, 'SeaCrow' (description-only) hits none of the backfill rows",
      not any(s.startswith("b_") for s in seacrow_before), seacrow_before)
ran = db.run_pending_migrations()
check("backfill: run by run_pending_migrations", ran == ["text_sources_724c"], ran)
check("backfill: recorded in schema_migrations", "text_sources_724c" in db.applied_migrations())
check("backfill: registered in MIGRATIONS", "text_sources_724c" in [n for n, _ in db.MIGRATIONS])
after = {r["slug"]: r for r in q(f"SELECT {COLS} FROM capture_events")}
changed = sorted(s for s in after if after[s] != before[s])
check("backfill: changes exactly the settled YouTube rows with a description (+ the no-text one)",
      changed == sorted([B_BOTH, B_DESC, B_NONE, B_UNSUP, B_KEEP]), changed)
check("backfill: description + stored OCR text -> both, OCR text kept as the thumbnail section",
      state(B_BOTH) == ("done", None, "description", "Description:\nInfernal Robotics SeaCrow hull.\n\nThumbnail text:\nHULL NUMBER 7"),
      state(B_BOTH))
check("backfill: description only", state(B_DESC) == ("done", None, "description",
                                                      "Description:\nSeaCrow sea trials with a Vulture escort."), state(B_DESC))
check("backfill: no description -> OCR text untouched", state(B_OCR) == ("done", None, None, "ONLY THUMB"), state(B_OCR))
check("backfill: no text at all -> grey unsupported with the reason", state(B_NONE) == ("unsupported", NOTE, None, ""),
      state(B_NONE))
check("backfill: an unsupported row with a description -> done, stale note gone",
      state(B_UNSUP) == ("done", None, "description", "Description:\nRevived: the SeaCrow returns."), state(B_UNSUP))
keep_tm = json.loads(after[B_KEEP]["type_metadata"])
check("backfill: other type_metadata keys kept",
      keep_tm.get("view_count") == 42 and keep_tm.get("auto_caption") == "x" and keep_tm.get("text_source") == "description", keep_tm)
check("backfill: the old OCR text is never lost",
      state(B_KEEP)[3] == "Description:\nKeep my keys SeaCrow.\n\nThumbnail text:\nkept text", state(B_KEEP))
for s, why in ((B_PEND, "pending"), (B_FAIL, "failed"), (B_RED, "redacted"), (B_JSON, "unreadable-JSON"), (IMG, "image")):
    check(f"backfill: the {why} row is left alone", after[s] == before[s], after[s])
seacrow_after = hits("SeaCrow")
check("backfill: 'SeaCrow' now hits the rebuilt rows (FTS follows the UPDATE trigger)",
      {B_BOTH, B_DESC, B_UNSUP, B_KEEP} <= set(seacrow_after) and B_PEND not in seacrow_after, seacrow_after)
check("backfill: 'Vulture' (description-only) hits", B_DESC in hits("Vulture"), hits("Vulture"))
idx = db.check_search_index()
check("backfill: search index in step (no missing or stale rows)", not idx["missing"] and not idx["stale"], idx)
with db.transaction():
    db._mig_text_sources_724c()
again = {r["slug"]: r for r in q(f"SELECT {COLS} FROM capture_events")}
check("backfill: idempotent (a second run changes nothing)", again == after,
      sorted(s for s in again if again[s] != after[s]))

ctx.__exit__(None, None, None)
print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
