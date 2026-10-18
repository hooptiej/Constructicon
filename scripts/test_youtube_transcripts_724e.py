#!/usr/bin/env python3
"""#724 part e: a YouTube video's transcript (captions) is an extra searchable text source.

Throwaway DB + storage (scripts/_testenv.py), the real app through TestClient, no server. The
transcript library (youtube-transcript-api) is MOCKED: a fake module stands in for it, so the test
decides what "YouTube" answers (tracks, no captions, a hang, a network error). The thumbnail and
tesseract are faked as in test_youtube_descriptions_724c.py. Where the parser sandbox exists (Linux,
the app image) the hard deadline is also proven on a real child process, and the REAL library is run
once in a child with no network: it must come back as a reason, never a crash or a hang.

    python scripts/test_youtube_transcripts_724e.py

Covers:
  * found: manual captions preferred over auto-generated ones (any language, recorded); the text
    becomes a "Transcript:" section between the description and the thumbnail text, text_source
    'description+transcript', and a transcript-only word is a search hit (db.search, GET /api/search)
  * the noise filter: [Music] / foreign / [Applause] dropped; under 30 real words = no transcript
  * no captions (TranscriptsDisabled / NoTranscriptFound): settled 'none', existing text kept; with
    nothing else to read the grey reason says "no captions"
  * a hang: the hard deadline fires (thread path, and a real killed child where there's a sandbox),
    'retry' with the reason and a backoff time, OCR never 'failed', existing text (and an earlier
    transcript) intact; a network error is 'retry' too; the backoff grows and stops by itself
  * the off switch (CONSTRUCTICON_TRANSCRIPTS=off): nothing is asked, nothing written
  * the background pass: queued and due rows are worked one at a time, future retries and pending
    rows are skipped, the text is rebuilt without OCR
  * the transcripts_724e backfill: queues existing YouTube rows, leaves others alone, idempotent
  * the "Captions" line on the properties panel

Exits 1 if any check fails.
"""

import io
import json
import os
import sqlite3
import sys
import time
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("youtube-transcripts-724e-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ["CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS"] = "0"  # no polite pause inside a test

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, db, ingest, object_types, ocr, parse_limits, paths, transcripts  # noqa: E402
from core.object_types import youtube  # noqa: E402
_testenv.assert_isolated()

SANDBOX = parse_limits.sandbox_available()  # checked before the mocked part turns it off
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


def meta(slug):
    return db.get_by_slug(slug).get("type_metadata") or {}


def state(slug):
    r = db.get_by_slug(slug)
    tm = r.get("type_metadata") or {}
    return r["ocr_status"], tm.get("ocr_note"), tm.get("text_source"), r["extracted_text"] or ""


def hits(word):
    return sorted(r["slug"] for r in db.search(query=word, limit=1000))


# --- the fake youtube_transcript_api -------------------------------------------------------------
fake = types.ModuleType("youtube_transcript_api")


class CouldNotRetrieveTranscript(Exception):
    pass


for _name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "InvalidVideoId", "AgeRestricted",
              "VideoUnplayable", "RequestBlocked", "YouTubeRequestFailed"):
    setattr(fake, _name, type(_name, (CouldNotRetrieveTranscript,), {}))
fake.IpBlocked = type("IpBlocked", (fake.RequestBlocked,), {})
fake.CouldNotRetrieveTranscript = CouldNotRetrieveTranscript


class ConnectionError(Exception):  # named like requests' own, which is what the classifier reads
    pass


class Snip:
    def __init__(self, text):
        self.text = text


class Track:
    def __init__(self, language, code, generated, lines):
        self.language, self.language_code, self.is_generated, self._lines = language, code, generated, lines

    def fetch(self):
        CALLS["fetched"].append((self.language_code, self.is_generated))
        return [Snip(x) for x in self._lines]


ANSWERS = {}  # video id -> list of Tracks, an exception instance, or "hang"
CALLS = {"list": [], "fetched": [], "clients": []}


class YouTubeTranscriptApi:
    def __init__(self, proxy_config=None, http_client=None):
        CALLS["clients"].append(http_client)

    def list(self, video_id):
        CALLS["list"].append(video_id)
        answer = ANSWERS.get(video_id, [])
        if answer == "hang":
            time.sleep(30)
            return []
        if isinstance(answer, Exception):
            raise answer
        return list(answer)


fake.YouTubeTranscriptApi = YouTubeTranscriptApi
sys.modules["youtube_transcript_api"] = fake

# The mocked library runs in-process (the parser child would import the real one): the hard deadline
# there is the thread path. The real-child path is proven separately below with the sandbox on.
os.environ["CONSTRUCTICON_PARSE_SANDBOX"] = "off"


def speech(n, word="rover"):
    """n real words of speech, a caption line per 5 words."""
    words = [f"{word}" if i % 7 == 0 else ["the", "walker", "turns", "left", "again", "slowly"][i % 6] for i in range(n)]
    return [" ".join(words[i:i + 5]) for i in range(0, n, 5)]


# --- the app, and fake thumbnails / tesseract (test_youtube_descriptions_724c.py's harness) ----------
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

PNG = paths.storage_dir() / "_fake_thumb.png"
buf = io.BytesIO()
Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
PNG.write_bytes(buf.getvalue())
THUMB = {}
_current = {"slug": None}
TESS = {"calls": 0}


def fake_source_path(row, spec):
    _current["slug"] = row["slug"]
    return PNG


def fake_tesseract(img, timeout=None):
    TESS["calls"] += 1
    return THUMB.get(_current["slug"], "")


ocr._ocr_source_path = fake_source_path
ocr._load_for_ocr = lambda path, key=None: None
ocr.pytesseract.image_to_string = fake_tesseract
ocr._compute_similarity_signals = lambda *a, **k: None

_n = [0]


def mkyt(video_id, description=None, ocr_status="pending", extra_meta=None, slug=None):
    _n[0] += 1
    slug = slug or f"yt_{_n[0]}_{video_id}"
    tm = dict(extra_meta or {})
    if description is not None:
        tm["description"] = description
    db.insert_content(slug, "tester", "youtube", external_url=f"https://www.youtube.com/watch?v={video_id}",
                      content_description=f"Video {slug}", type_metadata=tm or None)
    if ocr_status != "pending":
        with db.transaction() as tx:
            tx.conn.execute("UPDATE capture_events SET ocr_status = ? WHERE slug = ?", (ocr_status, slug))
    return slug


def turn_on():
    os.environ["CONSTRUCTICON_TRANSCRIPTS"] = "on"


def turn_off():
    os.environ["CONSTRUCTICON_TRANSCRIPTS"] = "off"


# ============================================================================================
# 1. the noise filter and the threshold
# ============================================================================================
text, words = transcripts.clean(["[Music]", "foreign", "[Applause]", "♪ ♪", "[Music] foreign [Applause]", ">> um"])
check("noise: [Music] / foreign / [Applause] / music notes / filler -> nothing left", (text, words) == ("", 0), (text, words))
text, words = transcripts.clean(["[Music] so this is the walker", "(applause) it turns left", "foreign"])
check("noise: tags stripped from inside real lines, real words kept",
      text == "so this is the walker it turns left" and words == 8, (text, words))
check("threshold: 30 real words is a transcript",
      transcripts.fields_for({"outcome": "found", "snippets": speech(30)}, {})["transcript_status"] == "found")
f29 = transcripts.fields_for({"outcome": "found", "snippets": speech(29) + ["[Music]"] * 20}, {})
check("threshold: 29 real words (plus noise) is NO transcript, with the reason",
      f29["transcript_status"] == "none" and "only music or sound tags (29 real words" in f29["transcript_note"]
      and f29["transcript"] is None, f29)

# ============================================================================================
# 2. found: manual over auto, sections, text_source, search
# ============================================================================================
turn_on()
ANSWERS["manualPick1"] = [Track("English (auto-generated)", "en", True, speech(40, "autoword")),
                          Track("German", "de", False, speech(40, "Zwiebelkuchen"))]
FOUND = mkyt("manualPick1", "Building the Quillback crawler.")
THUMB[FOUND] = "PART 3"
ocr.run_ocr(FOUND)
st, m = state(FOUND), meta(FOUND)
check("found: manual captions chosen over auto-generated (even in another language)",
      m.get("transcript_kind") == "manual" and m.get("transcript_language") == "German"
      and m.get("transcript_language_code") == "de", m)
check("found: transcript_status 'found', words counted", m.get("transcript_status") == "found"
      and m.get("transcript_words") == 40, m)
check("found: OCR settles done, no warning", st[0] == "done" and st[1] is None, st)
check("found: text_source = 'description+transcript'", st[2] == "description+transcript", st)
check("found: sections in order Description / Transcript / Thumbnail text",
      st[3].startswith("Description:\nBuilding the Quillback crawler.\n\nTranscript:\n")
      and st[3].endswith("\n\nThumbnail text:\nPART 3") and "Zwiebelkuchen" in st[3] and "autoword" not in st[3], st[3])
check("found: a transcript-only word is a search hit", hits("Zwiebelkuchen") == [FOUND], hits("Zwiebelkuchen"))
check("found: the library got a session with a default timeout (belt and braces under the deadline)",
      CALLS["clients"] and type(CALLS["clients"][-1]).__name__ == "_TimeoutSession", CALLS["clients"][-1:])
props = youtube.get_properties(db.get_by_slug(FOUND))
check("found: the properties panel says which captions", props.get("Captions") == "German (manual), 40 words", props)

ANSWERS["enFirst22"] = [Track("French", "fr", False, speech(35, "baguette")),
                        Track("English", "en", False, speech(35, "Snorkelwagon")),
                        Track("English (auto-generated)", "en", True, speech(35, "autoz"))]
EN = mkyt("enFirst22")
ocr.run_ocr(EN)
check("found: among manual tracks English is preferred", meta(EN).get("transcript_language_code") == "en"
      and "Snorkelwagon" in state(EN)[3] and state(EN)[2] == "transcript", (meta(EN), state(EN)))

ANSWERS["autoOnly33"] = [Track("English (auto-generated)", "en", True, speech(31, "Gravelbot"))]
AUTO = mkyt("autoOnly33")
ocr.run_ocr(AUTO)
check("found: auto-generated captions when there are no manual ones (recorded as auto)",
      meta(AUTO).get("transcript_kind") == "auto" and "Gravelbot" in state(AUTO)[3], meta(AUTO))
check("found: properties line for auto captions",
      youtube.get_properties(db.get_by_slug(AUTO)).get("Captions") == "English (auto-generated), 31 words",
      youtube.get_properties(db.get_by_slug(AUTO)))

if client is not None:
    r = client.get("/api/search", params={"query": "Gravelbot"})
    body = r.json() if r.status_code == 200 else {}
    found = [x.get("slug") for x in (body if isinstance(body, list) else body.get("results") or body.get("items") or [])]
    check("GET /api/search finds the transcript-only word", AUTO in found, (r.status_code, str(body)[:300]))

# ============================================================================================
# 3. noise only, no captions
# ============================================================================================
ANSWERS["rcNoise44"] = [Track("English (auto-generated)", "en", True, ["[Music]", "foreign", "[Applause]"] * 40)]
NOISE = mkyt("rcNoise44", "RC crawler on the rocks.")
THUMB[NOISE] = "TRAXXAS"
ocr.run_ocr(NOISE)
st, m = state(NOISE), meta(NOISE)
check("noise-only captions: transcript_status 'none' with the reason",
      m.get("transcript_status") == "none" and "music or sound tags (0 real words" in (m.get("transcript_note") or ""), m)
check("noise-only captions: no Transcript section, the description and thumbnail text kept",
      st == ("done", None, "description", "Description:\nRC crawler on the rocks.\n\nThumbnail text:\nTRAXXAS"), st)

ANSWERS["disabled55"] = fake.TranscriptsDisabled("disabled55")
DIS = mkyt("disabled55", "Has a description.")
ocr.run_ocr(DIS)
m = meta(DIS)
check("captions disabled: settled 'none' (not an error), reason recorded",
      m.get("transcript_status") == "none" and "turned off for this video" in m.get("transcript_note", "")
      and m.get("transcript_retry_at") is None, m)
check("captions disabled: the existing text is left alone", state(DIS)[0] == "done"
      and state(DIS)[3] == "Description:\nHas a description.", state(DIS))

ANSWERS["nothing66"] = fake.NoTranscriptFound("nothing66")
BARE = mkyt("nothing66")
ocr.run_ocr(BARE)
st = state(BARE)
check("no captions + no description + no thumbnail text: grey unsupported, the reason says 'no captions'",
      st[0] == "unsupported" and "no captions" in (st[1] or "") and "YouTube has no captions" in (st[1] or ""), st)
check("no captions: properties say none, with the reason",
      youtube.get_properties(db.get_by_slug(BARE)).get("Captions", "").startswith("none: "),
      youtube.get_properties(db.get_by_slug(BARE)))

ANSWERS["gone77"] = fake.VideoUnavailable("gone77")
GONE = mkyt("gone77", "Old video.")
ocr.run_ocr(GONE)
check("video unavailable: settled 'none' with the reason, OCR done", meta(GONE).get("transcript_status") == "none"
      and "unavailable" in meta(GONE).get("transcript_note", "") and state(GONE)[0] == "done", meta(GONE))

# ============================================================================================
# 4. a hang: the hard deadline -> retry, text intact; network errors; backoff
# ============================================================================================
ANSWERS["hangs88"] = "hang"
transcripts.DEADLINE_SECONDS = 1.0
HANG = mkyt("hangs88", "Live stream archive.")
THUMB[HANG] = "LIVE"
t0 = time.monotonic()
ocr.run_ocr(HANG)
took = time.monotonic() - t0
st, m = state(HANG), meta(HANG)
check("hang: the hard deadline fires (OCR returned in seconds, not the library's 30)", took < 8, f"{took:.1f}s")
check("hang: transcript_status 'retry' with the timeout as the reason",
      m.get("transcript_status") == "retry" and "timed out after 1 s" in (m.get("transcript_note") or ""), m)
check("hang: a backoff time is set (about 15 min out) and the attempt counted",
      m.get("transcript_attempts") == 1 and 14 * 60 <= (m.get("transcript_retry_at") or 0) - time.time() <= 16 * 60, m)
check("hang: OCR is NOT failed and the existing text is intact",
      st == ("done", None, "description", "Description:\nLive stream archive.\n\nThumbnail text:\nLIVE"), st)
check("hang: properties say it will be tried again",
      youtube.get_properties(db.get_by_slug(HANG)).get("Captions", "").startswith("not fetched yet: timed out"),
      youtube.get_properties(db.get_by_slug(HANG)))

# an earlier transcript survives a later hang (never lose text)
ANSWERS["keepMe99"] = [Track("English", "en", False, speech(45, "Pemberwick"))]
KEEP = mkyt("keepMe99", "Keeps its words.")
ocr.run_ocr(KEEP)
before = state(KEEP)
ANSWERS["keepMe99"] = "hang"
ocr.run_ocr(KEEP)  # a Retry OCR while YouTube hangs
after, m = state(KEEP), meta(KEEP)
check("hang after a found transcript: the transcript and the text are kept",
      "Pemberwick" in after[3] and after == before and m.get("transcript") and m.get("transcript_status") == "retry", (after, m))
check("hang after a found transcript: still a search hit", hits("Pemberwick") == [KEEP], hits("Pemberwick"))
check("hang after a found transcript: properties say an earlier one is kept",
      "earlier transcript is kept" in youtube.get_properties(db.get_by_slug(KEEP)).get("Captions", ""))

ANSWERS["netErr10"] = ConnectionError("Max retries exceeded")
NET = mkyt("netErr10")
ocr.run_ocr(NET)
m, st = meta(NET), state(NET)
check("network error: 'retry' (couldn't reach YouTube), never failed",
      m.get("transcript_status") == "retry" and "couldn't reach YouTube" in m.get("transcript_note", "") and st[0] != "failed",
      (m, st))
check("network error, nothing else to read: grey unsupported, the reason says captions weren't fetched yet",
      st[0] == "unsupported" and "captions not fetched yet" in (st[1] or ""), st)
ANSWERS["blocked11"] = fake.IpBlocked("blocked11")
BLK = mkyt("blocked11", "x y z")
ocr.run_ocr(BLK)
check("YouTube blocking us: 'retry' with the reason", meta(BLK).get("transcript_status") == "retry"
      and "blocking" in meta(BLK).get("transcript_note", ""), meta(BLK))

# backoff grows, then stops by itself
m_ = {}
waits = []
for i in range(transcripts.MAX_AUTO_ATTEMPTS):
    f = transcripts.fields_for({"outcome": "retry", "reason": "timed out after 20 s"}, m_, now=1000.0)
    m_ = {**m_, **{k: v for k, v in f.items() if v is not None}}
    if f.get("transcript_retry_at") is None:
        m_.pop("transcript_retry_at", None)
    waits.append(f.get("transcript_retry_at"))
check("backoff: the wait doubles (15 min, 30 min, 1 h, ...)", waits[0] == 1000 + 900 and waits[1] == 1000 + 1800
      and waits[2] == 1000 + 3600, waits)
check("backoff: capped at a day", all(w is None or w - 1000 <= 24 * 3600 for w in waits), waits)
check("backoff: after MAX_AUTO_ATTEMPTS it stops retrying by itself and says so",
      waits[-1] is None and "stopped retrying by itself" in f["transcript_note"] and "Retry OCR" in f["transcript_note"], f)

transcripts.DEADLINE_SECONDS = 20.0

# ============================================================================================
# 5. the off switch
# ============================================================================================
turn_off()
ANSWERS["offSwitch12"] = [Track("English", "en", False, speech(40, "Offword"))]
n_list = len(CALLS["list"])
OFF = mkyt("offSwitch12", "Offline install.")
ocr.run_ocr(OFF)
check("off: YouTube is never asked", len(CALLS["list"]) == n_list, CALLS["list"][n_list:])
check("off: no transcript fields written, the description still settles",
      not any(k.startswith("transcript") for k in meta(OFF)) and state(OFF)[:3] == ("done", None, "description"), meta(OFF))
check("off: stored transcripts stay searchable", hits("Zwiebelkuchen") == [FOUND])
check("off: the background pass does nothing", transcripts.work_due(10)["tried"] == 0)

# ============================================================================================
# 6. the background pass (web's _transcript_loop) and the backfill
# ============================================================================================
turn_on()
ANSWERS["queued13"] = [Track("English", "en", False, speech(33, "Kettlebot"))]
ANSWERS["queued14"] = fake.TranscriptsDisabled("queued14")
ANSWERS["dueRetry15"] = [Track("English", "en", False, speech(33, "Duebot"))]
ANSWERS["futureRetry16"] = [Track("English", "en", False, speech(33, "Futurebot"))]
ANSWERS["pendingRow17"] = [Track("English", "en", False, speech(33, "Pendbot"))]
Q1 = mkyt("queued13", None, ocr_status="unsupported",
          extra_meta={"ocr_note": "video: no description or captions yet"})
with db.transaction() as tx:  # a settled thumbnail-only row from before part e
    tx.conn.execute("UPDATE capture_events SET extracted_text = 'OLD THUMB', ocr_status = 'done' WHERE slug = ?", (Q1,))
Q2 = mkyt("queued14", None, ocr_status="unsupported", extra_meta={"ocr_note": "video: no description or captions yet"})
DUE = mkyt("dueRetry15", "Due row.", ocr_status="done",
           extra_meta={"transcript_status": "retry", "transcript_retry_at": time.time() - 5, "transcript_attempts": 2})
FUT = mkyt("futureRetry16", "Future row.", ocr_status="done",
           extra_meta={"transcript_status": "retry", "transcript_retry_at": time.time() + 3600, "transcript_attempts": 1})
PEND = mkyt("pendingRow17", "Pending row.")  # OCR still pending: its own run fetches
RED = mkyt("redacted18", "Redacted.", ocr_status="done")
OTHER_SLUG = "img_not_youtube"
db.insert_content(OTHER_SLUG, "tester", "url", external_url="https://example.com/x", content_description="Link")
with db.transaction() as tx:
    tx.conn.execute("UPDATE capture_events SET redacted = 1 WHERE slug = ?", (RED,))

# the backfill (transcripts_724e): run it again by hand on these rows (startup ran it on an empty DB)
check("backfill: registered in MIGRATIONS", "transcripts_724e" in [n for n, _ in db.MIGRATIONS])
check("backfill: recorded in schema_migrations at startup", "transcripts_724e" in db.applied_migrations()
      if client is not None else True)
COLS = "slug, type_metadata, extracted_text, ocr_status"
snap0 = {r["slug"]: r for r in q(f"SELECT {COLS} FROM capture_events")}
with db.transaction():
    db._mig_transcripts_724e()
snap1 = {r["slug"]: r for r in q(f"SELECT {COLS} FROM capture_events")}
st_of = lambda s: (json.loads(snap1[s]["type_metadata"] or "{}") or {}).get("transcript_status")  # noqa: E731
check("backfill: never-tried YouTube rows are queued (incl. one OCR'd while transcripts were off)",
      st_of(Q1) == "queued" and st_of(Q2) == "queued" and st_of(PEND) == "queued" and st_of(OFF) == "queued",
      (st_of(Q1), st_of(Q2), st_of(PEND), st_of(OFF)))
check("backfill: rows that already have a status keep it (found / none / retry untouched)",
      all(snap1[s] == snap0[s] for s in (FOUND, BARE, HANG, DUE, FUT))
      and (st_of(FOUND), st_of(BARE), st_of(HANG), st_of(DUE)) == ("found", "none", "retry", "retry"))
check("backfill: redacted and non-YouTube rows are left alone", snap1[RED] == snap0[RED]
      and snap1[OTHER_SLUG] == snap0[OTHER_SLUG])
check("backfill: no network, no OCR (text and statuses unchanged)",
      all(snap1[s]["extracted_text"] == snap0[s]["extracted_text"] and snap1[s]["ocr_status"] == snap0[s]["ocr_status"]
          for s in snap0))
check("backfill: other type_metadata keys kept", json.loads(snap1[Q1]["type_metadata"]).get("ocr_note")
      == "video: no description or captions yet")
with db.transaction():
    db._mig_transcripts_724e()
snap2 = {r["slug"]: r for r in q(f"SELECT {COLS} FROM capture_events")}
check("backfill: idempotent (a second run changes nothing)", snap2 == snap1,
      sorted(s for s in snap2 if snap2[s] != snap1[s]))

due = db.list_due_text_fetches(["youtube"], time.time(), 50)
check("pass: due = queued + retry-now; not a future retry, a pending row or a redacted one",
      set(due) == {Q1, Q2, DUE, OFF} and FUT not in due and PEND not in due and RED not in due, due)

tess_before = TESS["calls"]
n_list = len(CALLS["list"])
result = transcripts.work_due(10)
check("pass: worked each due row once, one at a time", result == {"tried": 4, "found": 3, "none": 1, "retry": 0}
      and len(CALLS["list"]) - n_list == 4, (result, CALLS["list"][n_list:]))
check("pass: no OCR re-run (tesseract not called)", TESS["calls"] == tess_before)
st = state(Q1)
check("pass: a found transcript is folded into the stored text, the old thumbnail text kept",
      st[0] == "done" and st[2] == "transcript" and "Kettlebot" in st[3]
      and st[3].endswith("Thumbnail text:\nOLD THUMB"), st)
check("pass: ... and is a search hit", hits("Kettlebot") == [Q1], hits("Kettlebot"))
st = state(Q2)
check("pass: no captions on a row with nothing else -> stays grey, reason now says 'no captions'",
      st[0] == "unsupported" and "no captions" in (st[1] or ""), st)
check("pass: a due retry that now succeeds -> found, attempts reset",
      meta(DUE).get("transcript_status") == "found" and "transcript_attempts" not in meta(DUE)
      and "Duebot" in state(DUE)[3], meta(DUE))
check("pass: the future retry is untouched", meta(FUT).get("transcript_status") == "retry" and "Futurebot" not in state(FUT)[3])
check("pass: a second pass finds nothing due", transcripts.work_due(10)["tried"] == 0)
check("pass: the search index is in step", not db.check_search_index()["missing"] and not db.check_search_index()["stale"],
      db.check_search_index())

# an undo-safe edit after a transcript: a corrected description keeps the transcript section
if True:
    from core import items
    items.update(FOUND, type_metadata={"description": "Corrected Quillback text."})
    st = state(FOUND)
    check("edit: a corrected description rebuilds the text and keeps the transcript",
          st[2] == "description+transcript" and "Corrected Quillback" in st[3] and "Zwiebelkuchen" in st[3], st)

# ============================================================================================
# 7. the real deadline in a real child, and the real library in a child (sandbox only)
# ============================================================================================
os.environ.pop("CONSTRUCTICON_PARSE_SANDBOX", None)
if SANDBOX and parse_limits.sandbox_available():
    t0 = time.monotonic()
    out = transcripts.run_with_deadline(time.sleep, (30,), 1.0)
    took = time.monotonic() - t0
    check("sandbox: a child that hangs is killed at the deadline -> retry", out == {"outcome": "retry",
          "reason": "timed out after 1 s"} and took < 8, (out, f"{took:.1f}s"))
    sys.modules.pop("youtube_transcript_api", None)  # the child imports the real package anyway
    t0 = time.monotonic()
    out = transcripts.fetch("aaaaaaaaaaa", deadline=15.0)
    took = time.monotonic() - t0
    check("sandbox: the REAL library runs in a child and answers with a reason (no network here: retry)",
          isinstance(out, dict) and out.get("outcome") in ("retry", "none", "found") and took < 20, (out, f"{took:.1f}s"))
    print(f"   real library, no network: {out}")
else:
    print("SKIP sandbox checks (no parser sandbox on this platform: the thread deadline above covered it)")

ctx.__exit__(None, None, None)
print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
