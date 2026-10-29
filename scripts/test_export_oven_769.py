#!/usr/bin/env python3
"""Fresh from the export oven (#769): a video's container date can be its export time. Throwaway DB.

    python scripts/test_export_oven_769.py

Needs ffmpeg/ffprobe (the app image has both): it makes small real .mov files and runs them through
the upload-time path (core/embedded_metadata.fill_missing). No server, no network. Covers:
  1. the parser: the Apple tag's colon-less offsets (-0700, -0600), Z, a ";"-joined value, naive;
  2. the pure export-time rule (EXPORT_TIME_WINDOW_SECONDS, the first-upload cutoff);
  3. (a) creation_time = export time + com.apple.quicktime.creationdate = the real date -> the real
     date, source recorded as the QuickTime tag;
     (b) only creation_time, at the upload moment, after the first upload -> not applied, the refusal
     recorded and shown on the object page; with a dated file name the name's date wins and the
     refusal stays recorded under it;
     (c) only creation_time, old -> applied as before, source container_creation_time;
     (d) both tags agree -> the same date as before;
     an export-time date from BEFORE the first upload is still applied; fill_missing never overwrites
     an existing content_date;
  4. the guesser (item_dates.guesses_for, read_file=True) never offers the refused export time.
Exits 1 if any check fails.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("exportoven769-")
os.environ.setdefault("CAPTION_DISABLED", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from core import actor, db, embedded_metadata, item_dates, storage, timeline  # noqa: E402
from core.object_types import video  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def iso_z(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_mov(creation_time=None, apple=None):
    """A 1-second 32x32 .mov with the given tags; returns its bytes."""
    out = os.path.join(tempfile.mkdtemp(dir=TMP), "clip.mov")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=32x32:d=1", "-c:v", "mpeg4"]
    if apple:
        cmd += ["-movflags", "use_metadata_tags", "-metadata", f"com.apple.quicktime.creationdate={apple}"]
    if creation_time:
        cmd += ["-metadata", f"creation_time={creation_time}"]
    subprocess.run(cmd + [out], check=True, capture_output=True)
    with open(out, "rb") as f:
        return f.read()


def upload(slug, data, filename="clip.mov", uploaded_at=None, content_date=None):
    """Insert a video row with its stored file, as the upload path does, then fill_missing."""
    _s, stored = storage.save_file(filename, data)
    db.insert_upload(slug, filename, stored, "tester", media_type="video", content_date=content_date)
    import sqlite3
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (uploaded_at, slug))
    c.commit()
    c.close()
    return embedded_metadata.fill_missing(slug)


def src(row):
    return (row.get("type_metadata") or {}).get("content_date_source") or {}


# --- 1. the parser -------------------------------------------------------------------------------
print("--- 1. the parser ---")
p = video.parse_creation_time
check("Apple -0700 offset honoured", p("2023-12-03T17:27:47-0700").timestamp() == utc(2023, 12, 4, 0, 27, 47))
check("Apple -0600 offset honoured", p("2023-10-21T14:25:08-0600").timestamp() == utc(2023, 10, 21, 20, 25, 8))
check("Z suffix with microseconds", p("2019-11-26T20:12:44.000000Z").timestamp() == utc(2019, 11, 26, 20, 12, 44))
check("a ';'-joined value reads its first part", p("2026-10-18T16:08:55.000000Z;2026-10-18T16:08:55Z").timestamp()
      == utc(2026, 10, 18, 16, 8, 55))
check("a bare value stays naive (Mountain Time is applied by the caller)", p("2019-05-12 10:15:00").tzinfo is None)
try:
    p("not a date")
    check("garbage raises ValueError", False)
except ValueError:
    check("garbage raises ValueError", True)

# --- 2. the pure rule ---------------------------------------------------------------------------
print("--- 2. the export-time rule ---")
W = embedded_metadata.EXPORT_TIME_WINDOW_SECONDS
lx = embedded_metadata.looks_like_export_time
check("the window is 10 minutes", W == 600.0, W)
check("1 min before the upload, after the first upload: an export time", lx(1000.0 - 60, 1000.0, 0.0))
check("1 min after the upload: an export time too", lx(1000.0 + 60, 1000.0, 0.0))
check("11 min before: a real date", not lx(1000.0 - 660, 1000.0, 0.0))
check("before the install's first upload: a real date", not lx(1000.0 - 60, 1000.0, 999.0))
check("no first upload known: only the window decides", lx(1000.0 - 60, 1000.0, None))

# --- 3. real files through fill_missing --------------------------------------------------------
print("--- 3. upload-time fill ---")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    check("ffmpeg and ffprobe are on PATH (run this in the app image)", False)
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)

db.init_db()
NOW = time.time()
FIRST = NOW - 30 * 86400
with actor.acting_as(actor.ACTOR_SCRIPT):
    db.insert_upload("first", "first.jpg", None, "tester", media_type="image")
    import sqlite3
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.execute("UPDATE capture_events SET timestamp = ? WHERE slug = 'first'", (FIRST,))
    c.commit()
    c.close()
    check("the cutoff is the first upload", db.first_upload_epoch() == FIRST, db.first_upload_epoch())

    export_iso = iso_z(NOW - 60)
    data_a = make_mov(creation_time=export_iso, apple="2023-12-03T17:27:47-0700")
    with open(os.path.join(TMP, "a.mov"), "wb") as f:
        f.write(data_a)
    tags = video.read_date_tags(os.path.join(TMP, "a.mov"))
    check("ffprobe reads the Apple tag back from the fixture", tags["creationdate"] == "2023-12-03T17:27:47-0700", tags)

    a = upload("vid-a", data_a, filename="IMG_7124.mov", uploaded_at=NOW)
    check("(a) the Apple capture date wins over an export-time creation_time",
          a["content_date"] == utc(2023, 12, 4, 0, 27, 47), a["content_date"])
    check("(a) source recorded: quicktime creationdate", src(a).get("source") == "quicktime_creationdate"
          and "2023-12-03T17:27:47-0700" in src(a).get("evidence", "") and src(a).get("guessed") is False, src(a))

    b = upload("vid-b", make_mov(creation_time=export_iso), filename="clip.mov", uploaded_at=NOW)
    check("(b) an export-time creation_time is not applied", b["content_date"] is None
          and timeline.resolve_item_date(b) is None, b["content_date"])
    check("(b) the refusal is recorded with the value and why",
          src(b).get("source") == "rejected_export_time" and src(b).get("rejected") is True
          and src(b).get("rejected_tag") == "container_creation_time"
          and abs(src(b).get("rejected_value", 0) - (NOW - 60)) < 2 and "exported" in src(b).get("evidence", ""), src(b))

    b2 = upload("vid-b2", make_mov(creation_time=export_iso), filename="VID_20190512_101500.mov", uploaded_at=NOW)
    check("(b) with a dated file name, the name's date applies instead",
          b2["content_date"] == timeline.source_datetime_to_epoch(datetime(2019, 5, 12, 10, 15)), b2["content_date"])
    check("(b) ... the guess is recorded and the refused file date stays under it",
          src(b2).get("source") == "filename" and src(b2).get("rejected_file_date", {}).get("rejected") is True, src(b2))

    c_row = upload("vid-c", make_mov(creation_time="2019-11-26T20:12:44Z"), uploaded_at=NOW)
    check("(c) an old creation_time is applied as before", c_row["content_date"] == utc(2019, 11, 26, 20, 12, 44),
          c_row["content_date"])
    check("(c) source recorded: container creation_time", src(c_row).get("source") == "container_creation_time", src(c_row))

    d = upload("vid-d", make_mov(creation_time="2023-10-21T20:25:08Z", apple="2023-10-21T14:25:08-0600"), uploaded_at=NOW)
    check("(d) both tags agree: the same date", d["content_date"] == utc(2023, 10, 21, 20, 25, 8), d["content_date"])

    e = upload("vid-e", make_mov(creation_time=iso_z(FIRST - 120)), uploaded_at=FIRST - 60)
    check("an export-time-shaped date from before the first upload is still applied",
          e["content_date"] == float(int(FIRST - 120)), (e["content_date"], FIRST))

    kept = upload("vid-kept", make_mov(creation_time="2019-11-26T20:12:44Z"), uploaded_at=NOW, content_date=utc(2001, 1, 1))
    check("fill_missing never overwrites an existing content_date", kept["content_date"] == utc(2001, 1, 1)
          and not src(kept), (kept["content_date"], src(kept)))

# --- 4. the guesser and the page ----------------------------------------------------------------
print("--- 4. the guesser and the object page ---")
guesses = item_dates.guesses_for(db.get_by_slug("vid-b"), read_file=True)
check("the guesser offers no file_metadata guess for the refused export time",
      not any(g.source == "file_metadata" for g in guesses), [(g.source, g.epoch) for g in guesses])
guesses_c = item_dates.guesses_for(dict(db.get_by_slug("vid-c"), content_date=None), read_file=True)
check("... but still offers an old container date", any(g.source == "file_metadata" for g in guesses_c),
      [(g.source, g.epoch) for g in guesses_c])

from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app)
html_b = client.get("/object/vid-b").text
check("object page: an undated video says why its file date was ignored", "no real date: ignored the file" in html_b
      and "exported, not when it was shot" in html_b)
html_a = client.get("/object/vid-a").text
check("object page: says the date is from the QuickTime tag (not 'guessed')",
      "from the quicktime creationdate 2023-12-03T17:27:47-0700" in html_a
      and "guessed from quicktime" not in html_a)

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
