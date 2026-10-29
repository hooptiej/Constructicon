#!/usr/bin/env python3
"""Fresh from the export oven (#769): a file's own date can be its export time. Throwaway DB.

    python scripts/test_export_oven_769.py

Needs ffmpeg/ffprobe (the app image has both): it makes small real .mov files, and JPEGs with EXIF
through Pillow, and runs them through the upload-time path (core/embedded_metadata.fill_missing).
No server, no network. Owner decision on PR #807: a possible export time is ACCEPTED, then FLAGGED.
Covers:
  1. the parser: the Apple tag's colon-less offsets (-0700, -0600), Z, a ";"-joined value, naive;
  2. the pure export-time rule (EXPORT_TIME_WINDOW_SECONDS, the first-upload cutoff);
  3. videos: (a) creation_time = export time + com.apple.quicktime.creationdate = the real date ->
     the real date, source = the QuickTime tag, not flagged;
     (b) only creation_time, at the upload moment, after the first upload -> applied AND flagged
     (suspect_export_time + the note), also with a dated file name (the file date is not replaced);
     (c) only creation_time, old -> applied, not flagged; (d) both tags agree -> the same date;
     an export-time-shaped date from BEFORE the first upload is not flagged; fill_missing never
     overwrites an existing content_date;
  4. photos: EXIF DateTimeOriginal within the window -> applied and flagged; an old one -> applied,
     source exif_datetimeoriginal, not flagged;
  5. the item page: the flag after a suspect date; none on an Apple-tag date.
Exits 1 if any check fails.
"""

import io
import os
import shutil
import sqlite3
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

from PIL import Image  # noqa: E402

from core import actor, db, embedded_metadata, storage, timeline  # noqa: E402
from core.object_types import image as image_type  # noqa: E402
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


def make_jpeg(original, offset=None):
    """An 8x8 JPEG whose EXIF DateTimeOriginal is `original` ("YYYY:MM:DD HH:MM:SS"), with
    OffsetTimeOriginal when `offset` is given."""
    exif = Image.Exif()
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = original
    if offset:
        ifd[0x9011] = offset
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "olive").save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def setcol(slug, **cols):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    try:
        for k, v in cols.items():
            c.execute(f"UPDATE capture_events SET {k} = ? WHERE slug = ?", (v, slug))
        c.commit()
    finally:
        c.close()


def upload(slug, data, filename, media_type="video", uploaded_at=None, content_date=None):
    """Insert a row with its stored file, as the upload path does, then fill_missing."""
    _s, stored = storage.save_file(filename, data)
    db.insert_upload(slug, filename, stored, "tester", media_type=media_type, content_date=content_date)
    setcol(slug, timestamp=uploaded_at)
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
check("1 min before the upload, after the first upload: may be an export time", lx(1000.0 - 60, 1000.0, 0.0))
check("1 min after the upload: may be one too", lx(1000.0 + 60, 1000.0, 0.0))
check("11 min before: not suspect", not lx(1000.0 - 660, 1000.0, 0.0))
check("before the install's first upload: not suspect", not lx(1000.0 - 60, 1000.0, 999.0))
check("no first upload known: only the window decides", lx(1000.0 - 60, 1000.0, None))

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    check("ffmpeg and ffprobe are on PATH (run this in the app image)", False)
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)

db.init_db()
NOW = time.time()
FIRST = NOW - 30 * 86400
NOTE = "may be the export time (1 min before the upload): check it"
with actor.acting_as(actor.ACTOR_SCRIPT):
    db.insert_upload("first", "first.jpg", None, "tester", media_type="image")
    setcol("first", timestamp=FIRST)
    check("the cutoff is the first upload", db.first_upload_epoch() == FIRST, db.first_upload_epoch())

    # --- 3. videos ------------------------------------------------------------------------------
    print("--- 3. videos ---")
    export_iso = iso_z(NOW - 60)
    data_a = make_mov(creation_time=export_iso, apple="2023-12-03T17:27:47-0700")
    with open(os.path.join(TMP, "a.mov"), "wb") as f:
        f.write(data_a)
    tags = video.read_date_tags(os.path.join(TMP, "a.mov"))
    check("ffprobe reads the Apple tag back from the fixture", tags["creationdate"] == "2023-12-03T17:27:47-0700", tags)

    a = upload("vid-a", data_a, "IMG_7124.mov", uploaded_at=NOW)
    check("(a) the Apple capture date wins over an export-time creation_time",
          a["content_date"] == utc(2023, 12, 4, 0, 27, 47), a["content_date"])
    check("(a) source recorded: quicktime creationdate, not flagged", src(a).get("source") == "quicktime_creationdate"
          and "2023-12-03T17:27:47-0700" in src(a).get("evidence", "") and src(a).get("guessed") is False
          and not src(a).get("suspect_export_time"), src(a))

    b = upload("vid-b", make_mov(creation_time=export_iso), "clip.mov", uploaded_at=NOW)
    check("(b) an export-time-shaped creation_time is applied", b["content_date"] == float(int(NOW - 60))
          and timeline.resolve_item_date(b) == b["content_date"], (b["content_date"], NOW))
    check("(b) ... and flagged, with a note saying how close to the upload it is",
          src(b).get("source") == "container_creation_time" and src(b).get("suspect_export_time") is True
          and src(b).get("suspect_note") == NOTE and src(b).get("confidence") == "medium", src(b))

    b2 = upload("vid-b2", make_mov(creation_time=export_iso), "VID_20190512_101500.mov", uploaded_at=NOW)
    check("(b) with a dated file name, the flagged file date stays (the name doesn't replace it)",
          b2["content_date"] == float(int(NOW - 60)) and src(b2).get("suspect_export_time") is True, src(b2))

    c_row = upload("vid-c", make_mov(creation_time="2019-11-26T20:12:44Z"), "clip.mov", uploaded_at=NOW)
    check("(c) an old creation_time is applied, not flagged", c_row["content_date"] == utc(2019, 11, 26, 20, 12, 44)
          and src(c_row).get("source") == "container_creation_time" and not src(c_row).get("suspect_export_time"),
          (c_row["content_date"], src(c_row)))

    d = upload("vid-d", make_mov(creation_time="2023-10-21T20:25:08Z", apple="2023-10-21T14:25:08-0600"),
               "clip.mov", uploaded_at=NOW)
    check("(d) both tags agree: the same date", d["content_date"] == utc(2023, 10, 21, 20, 25, 8), d["content_date"])

    e = upload("vid-e", make_mov(creation_time=iso_z(FIRST - 120)), "clip.mov", uploaded_at=FIRST - 60)
    check("an export-time-shaped date from before the first upload is applied, not flagged",
          e["content_date"] == float(int(FIRST - 120)) and not src(e).get("suspect_export_time"), (e["content_date"], src(e)))

    kept = upload("vid-kept", make_mov(creation_time=export_iso), "clip.mov", uploaded_at=NOW, content_date=utc(2001, 1, 1))
    check("fill_missing never overwrites an existing content_date (nor records a source for it)",
          kept["content_date"] == utc(2001, 1, 1) and not src(kept), (kept["content_date"], src(kept)))

    # --- 4. photos ------------------------------------------------------------------------------
    print("--- 4. photos ---")
    stamp = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    jpg_new = make_jpeg(stamp, offset="+00:00")
    with Image.open(io.BytesIO(jpg_new)) as im:
        read = image_type.read_capture(im)
    check("Pillow wrote the EXIF date the reader sees", read is not None and read[1] == "exif_datetimeoriginal"
          and read[2] == f"{stamp} +00:00", read)
    pn = upload("img-new", jpg_new, "IMG_0001.jpg", media_type="image", uploaded_at=NOW)
    check("a photo whose EXIF date sits at the upload is applied", pn["content_date"] == float(int(NOW - 60)),
          (pn["content_date"], NOW))
    check("... and flagged", src(pn).get("source") == "exif_datetimeoriginal" and src(pn).get("suspect_export_time") is True
          and src(pn).get("suspect_note") == NOTE, src(pn))
    po = upload("img-old", make_jpeg("2017:10:16 18:31:47"), "IMG_0002.jpg", media_type="image", uploaded_at=NOW)
    check("an old EXIF date is applied (Mountain Time), source recorded, not flagged",
          po["content_date"] == timeline.source_datetime_to_epoch(datetime(2017, 10, 16, 18, 31, 47))
          and src(po).get("source") == "exif_datetimeoriginal" and src(po).get("evidence") == "exif datetimeoriginal 2017:10:16 18:31:47"
          and not src(po).get("suspect_export_time"), (po["content_date"], src(po)))

# --- 5. the item page ---------------------------------------------------------------------------
print("--- 5. the item page ---")
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app)
html_b = client.get("/object/vid-b").text
check("item page: a suspect video date shows the flag after it",
      f'<span class="dp-flag date-suspect">{NOTE}</span>' in html_b and "from the container creation time" in html_b)
html_p = client.get("/object/img-new").text
check("item page: a suspect photo date shows the flag too", NOTE in html_p and "from the exif datetimeoriginal" in html_p)
html_a = client.get("/object/vid-a").text
check("item page: an Apple-tag date names the tag and carries no flag",
      "from the quicktime creationdate 2023-12-03T17:27:47-0700" in html_a and "may be the export time" not in html_a
      and "guessed from quicktime" not in html_a)
html_c = client.get("/object/vid-c").text
check("item page: an old container date carries no flag", "may be the export time" not in html_c)

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
