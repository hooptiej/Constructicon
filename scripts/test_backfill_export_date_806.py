#!/usr/bin/env python3
"""Backfill déjà vu (#806): scripts/backfill_content_dates.py goes through #769's export-time rule. Throwaway DB.

    python scripts/test_backfill_export_date_806.py

Needs ffmpeg/ffprobe (the app image has both): it makes small real .mov files and JPEGs with EXIF
through Pillow, inserts them as UNDATED rows (as every row from before the embedded-metadata hook
is), and runs the backfill's own per-row loop (process_rows) against them, writing through the
real POST /api/image/{slug} route with a TestClient. No server, no network.

What the rule is (owner decision on PR #807): a file date that sits within 10 minutes of the row's
upload, after the install's first upload, is ACCEPTED and FLAGGED (suspect_export_time + a note in
type_metadata.content_date_source), not dropped. So "the backfill goes through the same rule" means:
  1. an export-time-shaped container date is written WITH the flag and the source record, exactly what
     the live path (embedded_metadata.fill_missing) writes for the same file (parity check);
  2. a legitimately old container date is written with its source, not flagged;
  3. Apple's quicktime creationdate wins over an export-time creation_time, not flagged;
  4. an export-shaped date from before the install's first upload is not flagged (the cutoff is fed);
  5. an export-prone photo (EXIF at the upload moment) is flagged the same way;
  6. --dry-run writes nothing and says what each row would get; a re-run finds nothing left to do;
  7. negative control: the OLD script (plausible_content_date alone) wrote the same date with no
     source and no flag, so it differs from the new payload.
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
TMP = _testenv.isolate("backfillexport806-")
os.environ.setdefault("CAPTION_DISABLED", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from PIL import Image  # noqa: E402

from core import actor, db, embedded_metadata, storage, timeline  # noqa: E402
import backfill_content_dates as backfill  # noqa: E402
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


def insert(slug, data, filename, media_type="video", uploaded_at=None):
    """An UNDATED row with its stored file (no fill_missing): what the backfill sees."""
    _s, stored = storage.save_file(filename, data)
    db.insert_upload(slug, filename, stored, "tester", media_type=media_type)
    setcol(slug, timestamp=uploaded_at)


def src(row):
    return (row.get("type_metadata") or {}).get("content_date_source") or {}


if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    check("ffmpeg and ffprobe are on PATH (run this in the app image)", False)
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)

db.init_db()
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app)

NOW = time.time()
FIRST = NOW - 30 * 86400
export_iso = iso_z(NOW - 60)
UNDATED = ["bf-export", "bf-old", "bf-apple", "bf-prefirst", "bf-photo-new", "bf-photo-old", "bf-twin"]
NOTE = "may be the export time (1 min before the upload): check it"

with actor.acting_as(actor.ACTOR_SCRIPT):
    db.insert_upload("first", "first.jpg", None, "tester", media_type="image")
    setcol("first", timestamp=FIRST)
    check("the cutoff is the first upload", timeline.first_upload() == FIRST, timeline.first_upload())

    export_bytes = make_mov(creation_time=export_iso)
    insert("bf-export", export_bytes, "export.mov", uploaded_at=NOW)
    insert("bf-twin", export_bytes, "twin.mov", uploaded_at=NOW)  # same file, for the live-path parity check
    insert("bf-old", make_mov(creation_time="2019-11-26T20:12:44Z"), "old.mov", uploaded_at=NOW)
    insert("bf-apple", make_mov(creation_time=export_iso, apple="2023-12-03T17:27:47-0700"), "apple.mov", uploaded_at=NOW)
    insert("bf-prefirst", make_mov(creation_time=iso_z(FIRST - 120)), "pre.mov", uploaded_at=FIRST - 60)
    stamp = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    insert("bf-photo-new", make_jpeg(stamp, offset="+00:00"), "new.jpg", media_type="image", uploaded_at=NOW)
    insert("bf-photo-old", make_jpeg("2017:10:16 18:31:47"), "old.jpg", media_type="image", uploaded_at=NOW)
    # An already-dated video: never a candidate.
    insert("bf-dated", make_mov(creation_time=export_iso), "dated.mov", uploaded_at=NOW)
    setcol("bf-dated", content_date=utc(2001, 1, 1))

    # The live path on the twin file: what the backfill must match.
    live = embedded_metadata.fill_missing("bf-twin")

    candidates = [r for r in backfill.candidate_rows(("video", "image")) if r["slug"] in UNDATED and r["slug"] != "bf-twin"]
    check("the undated rows are candidates; the dated one is not",
          sorted(r["slug"] for r in candidates) == sorted(set(UNDATED) - {"bf-twin"})
          and "bf-dated" not in {r["slug"] for r in backfill.candidate_rows(("video", "image"))},
          [r["slug"] for r in candidates])

    # --- negative control: the OLD script's decision ------------------------------------------------
    row_export = db.get_by_slug("bf-export")
    old_style = embedded_metadata.plausible_content_date(embedded_metadata.extract(row_export).get("content_date"))
    check("negative control: the old script would have written the export-time date, with no source record",
          old_style == float(int(NOW - 60)), old_style)
    new_fields = backfill.write_fields(*backfill.resolve_row(row_export, timeline.first_upload()))
    check("negative control: the old payload (content_date alone) differs from the new one, which carries the source record",
          set(new_fields) == {"content_date", "type_metadata"} and "suspect_export_time" in new_fields["type_metadata"],
          new_fields)

    # --- resolve_row = the shared function -----------------------------------------------------------
    epoch, rec = backfill.resolve_row(row_export, timeline.first_upload())
    check("resolve_row gives the same answer as embedded_metadata.resolve_file_date",
          (epoch, rec) == embedded_metadata.resolve_file_date(embedded_metadata.extract(row_export), row_export,
                                                              timeline.first_upload()), (epoch, rec))
    check("resolve_row flags an export-time-shaped creation_time (suspect_export_time + note)",
          epoch == float(int(NOW - 60)) and rec and rec.get("suspect_export_time") is True
          and rec.get("suspect_note") == NOTE and rec.get("source") == "container_creation_time", (epoch, rec))

    # --- dry run: writes nothing ---------------------------------------------------------------------
    writes = []
    print("--- dry run ---")
    st = backfill.process_rows(candidates, ("video", "image"), timeline.first_upload(), True,
                               lambda s, e, r: writes.append(s))
    check("dry run makes no writes and leaves every row undated",
          not writes and st["written"] == 0 and all(db.get_by_slug(s)["content_date"] is None for s in UNDATED if s != "bf-twin"),
          (writes, st))
    check("dry run counts the flagged rows it would write (export video + export photo)", st["flagged"] == 2, st)

    # --- the real run, through POST /api/image/{slug} -----------------------------------------------
    def write(slug, e, r):
        resp = client.post(f"/api/image/{slug}", data=backfill.write_fields(e, r))
        assert resp.status_code == 200, (slug, resp.status_code, resp.text)

    print("--- real run ---")
    st = backfill.process_rows(candidates, ("video", "image"), timeline.first_upload(), False, write)
    check("the real run wrote every row that had a usable date", st["written"] == len(candidates) and st["flagged"] == 2, st)

    ex = db.get_by_slug("bf-export")
    check("(export) an export-time-shaped container date is written", ex["content_date"] == float(int(NOW - 60)), ex["content_date"])
    check("(export) ... with content_date_source recording it: container_creation_time, suspect_export_time, note",
          src(ex).get("source") == "container_creation_time" and src(ex).get("suspect_export_time") is True
          and src(ex).get("suspect_note") == NOTE and src(ex).get("confidence") == "medium"
          and src(ex).get("guessed") is False, src(ex))
    check("(parity) the backfill wrote exactly what the live path wrote for the same file",
          ex["content_date"] == live["content_date"] and src(ex) == src(live), (src(ex), src(live)))

    od = db.get_by_slug("bf-old")
    check("(old) an old container date is written, source recorded, not flagged",
          od["content_date"] == utc(2019, 11, 26, 20, 12, 44) and src(od).get("source") == "container_creation_time"
          and not src(od).get("suspect_export_time"), (od["content_date"], src(od)))

    ap = db.get_by_slug("bf-apple")
    check("(apple) the quicktime creationdate is preferred over an export-time creation_time",
          ap["content_date"] == utc(2023, 12, 4, 0, 27, 47), ap["content_date"])
    check("(apple) ... source = quicktime_creationdate, not flagged",
          src(ap).get("source") == "quicktime_creationdate" and "2023-12-03T17:27:47-0700" in src(ap).get("evidence", "")
          and not src(ap).get("suspect_export_time"), src(ap))

    pf = db.get_by_slug("bf-prefirst")
    check("(first-upload cutoff) an export-shaped date from before the install's first upload is not flagged",
          pf["content_date"] == float(int(FIRST - 120)) and not src(pf).get("suspect_export_time"), (pf["content_date"], src(pf)))

    pn = db.get_by_slug("bf-photo-new")
    check("(photo) EXIF at the upload moment is written and flagged, source exif_datetimeoriginal",
          pn["content_date"] == float(int(NOW - 60)) and src(pn).get("source") == "exif_datetimeoriginal"
          and src(pn).get("suspect_export_time") is True, (pn["content_date"], src(pn)))
    po = db.get_by_slug("bf-photo-old")
    check("(photo) an old EXIF date is written (Mountain Time) with its source, not flagged",
          po["content_date"] == timeline.source_datetime_to_epoch(datetime(2017, 10, 16, 18, 31, 47))
          and src(po).get("source") == "exif_datetimeoriginal" and not src(po).get("suspect_export_time"),
          (po["content_date"], src(po)))

    dated = db.get_by_slug("bf-dated")
    check("an already-dated row is untouched", dated["content_date"] == utc(2001, 1, 1) and not src(dated),
          (dated["content_date"], src(dated)))

    again = [r for r in backfill.candidate_rows(("video", "image")) if r["slug"] in UNDATED]
    check("a re-run finds nothing left to do (fill-only-missing)", not again, [r["slug"] for r in again])

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
