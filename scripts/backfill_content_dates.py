"""Issue #265: backfill content_date for existing image/video rows from the
metadata embedded in their own files.

content_date is the content's real-world date (when the photo was taken,
when the clip was recorded) as distinct from timestamp (when the row was
created here). New uploads get it for free now that
core/object_types/image.py and video.py register an embedded_metadata_fn
(EXIF DateTimeOriginal; the container's creation_time) and both upload
paths — POST /api/upload and the MCP constructicon_upload tool — already
call core/embedded_metadata.fill_missing after inserting the row. Every
row uploaded before that hook existed is what this script is for: the
same extraction, run once over the backlog.

Discipline, same as scripts/full_youtube_channel_sync.py's corrections
pass (--no-import):

  - Candidates are read straight from the target's own database
    (core.db.search) — this needs the row's stored_filename and the file
    itself on disk anyway, so it has to run somewhere that shares the
    instance's DB and storage (./scripts/run_tool.sh <web container> ..., #634).
  - Extraction is core.embedded_metadata.extract — literally the code
    path a fresh upload goes through, not a re-implementation — and the
    date is then decided by core.embedded_metadata.resolve_file_date, the
    SAME function fill_missing calls (#806): the plausibility gate, the
    tag that dated the row, and #769's export-time rule (a video's
    container creation_time / a photo's EXIF date that sits within
    EXPORT_TIME_WINDOW_SECONDS of the row's own upload time, after the
    install's first upload, is applied but FLAGGED suspect_export_time;
    Apple's quicktime creationdate is never flagged). Nothing here
    re-implements that rule. The context it needs is all in the target's
    own database: the row's upload time is row["timestamp"] (what the
    live path sees at upload) and the cutoff is timeline.first_upload(),
    read once per run.
  - Writes go through POST /api/image/{slug}'s content_date field (the
    surface the YouTube sync's publishedAt correction already uses), not
    a direct UPDATE, and the same call carries
    type_metadata={"content_date_source": <record>} so the date and its
    source land together as ONE undoable change, exactly what
    fill_missing records. Only description/tags-free payloads are sent:
    api_update_image leaves every field it isn't given alone (#213).
  - Fill-only-missing: only rows whose content_date is currently NULL are
    candidates, and each is re-read immediately before its write in case
    something else (an owner edit, another pass) set it in the meantime.
    A value already there — owner-typed, a YouTube sync's, an earlier run
    of this script — is never overwritten, so re-running is a no-op for
    everything already dated, exactly like fill_missing itself.

A naive source date (an EXIF timestamp with no OffsetTimeOriginal) is
interpreted as Mountain Time — core/timeline.py's source_datetime_to_epoch
is the one implementation of that convention; this script never parses a
date itself. A row whose file has no usable date (a screenshot, a render,
a re-encoded social-media download) is reported and skipped: it keeps
falling back to source_modified_at/timestamp as before.

Scope: --media-type defaults to image and video. Any registered type
whose embedded_metadata_fn returns a content_date would work, but PDF/STL/
the rest are deliberately out of #265's scope (no reliable real-world
date inside those files) and audio's hook deliberately returns none (a
bare ID3 year isn't a date — see core/embedded_metadata.py).

Usage:
    python scripts/backfill_content_dates.py --base-url http://localhost:80 --dry-run
    python scripts/backfill_content_dates.py --base-url http://localhost:80
    python scripts/backfill_content_dates.py --base-url http://localhost:80 --slug <slug> [--slug ...]

    --dry-run lists every candidate with the date that would be written
    (Mountain Time and UTC), the tag that dated it, and #769's possible-
    export-time note when the rule flags it, and makes no writes. --slug restricts the run
    to specific rows — for checking one real file end to end before
    running the whole backlog. --media-type (repeatable) overrides the
    default image+video set.

DO NOT run this against the real production instance without the owner's
separate go-ahead — test against the isolated constructicon-test
container first (see CLAUDE.md's live-testing section), same as every
other script here that writes data.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, embedded_metadata, object_types, timeline  # noqa: E402

# #448: derived from the specs; was hard-coded image+video
DEFAULT_MEDIA_TYPES = tuple(sorted(k for k, s in object_types.OBJECT_TYPES.items() if s.embedded_metadata_fn))


def candidate_rows(media_types, slugs=None):
    """Every row of the given media_types with a stored file and no
    content_date yet — optionally narrowed to specific slugs. Oldest
    first, so a partial run (Ctrl-C) leaves the backlog's tail, not its
    head, for next time."""
    rows = []
    for row in db.search(limit=1000000, include_brand=True):
        if row.get("media_type") not in media_types:
            continue
        if slugs and row["slug"] not in slugs:
            continue
        if row.get("content_date") is not None or not row.get("stored_filename"):
            continue
        rows.append(row)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def resolve_row(row, first_upload_at):
    """(#806) (epoch, content_date_source record) for one candidate row, decided by the SAME function
    the upload path uses (core.embedded_metadata.resolve_file_date): the plausibility gate, the tag
    that dated the row and #769's export-time flag. (None, None) = no usable embedded date. The
    row's own upload time (row["timestamp"]) and the install's first upload are the context the
    export-time rule needs; both come from the target's database, as they did at upload."""
    return embedded_metadata.resolve_file_date(embedded_metadata.extract(row), row, first_upload_at)


def write_fields(epoch, record=None):
    """The form fields POSTed to /api/image/{slug}: content_date, plus the content_date_source
    record (type_metadata is merged into the row's, never replaced) when the rule produced one."""
    fields = {"content_date": repr(epoch)}
    if record is not None:
        fields["type_metadata"] = json.dumps({"content_date_source": record})
    return fields


def write_content_date(base_url, slug, epoch, record=None):
    """POST /api/image/{slug} with only content_date (and its source record) — see the module
    docstring for why nothing else is resubmitted."""
    url = f"{base_url.rstrip('/')}/api/image/{slug}"
    data = urllib.parse.urlencode(write_fields(epoch, record)).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach {url} ({e.reason}) — is the target instance running and --base-url correct?") from e


def _describe(epoch):
    """'2017-10-16 18:31:47 MDT (2017-10-17T00:31:47Z)' — the local form is
    what the owner will recognize against the photo; the UTC form is what
    actually gets stored."""
    utc = datetime.fromtimestamp(epoch, timezone.utc)
    local = utc.astimezone(ZoneInfo(timeline.LOCAL_TIMEZONE))
    return f"{local.strftime('%Y-%m-%d %H:%M:%S %Z')} ({utc.strftime('%Y-%m-%dT%H:%M:%SZ')})"


def main():
    parser = argparse.ArgumentParser(
        description="Backfill content_date for existing image/video rows from their files' own "
                    "embedded metadata (EXIF DateTimeOriginal, container creation_time) — #265."
    )
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL of the running Constructicon instance to POST /api/image/<slug> against "
             "(must share a database and storage directory with this process)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be written; make no writes.")
    parser.add_argument(
        "--media-type", action="append", dest="media_types", metavar="TYPE",
        help=f"Restrict to this media_type (repeatable). Default: {', '.join(DEFAULT_MEDIA_TYPES)}.",
    )
    parser.add_argument(
        "--slug", action="append", dest="slugs", metavar="SLUG",
        help="Only this row (repeatable) — for verifying one real file end to end.",
    )
    args = parser.parse_args()
    import _http  # #467 step 2: send the install token to the app (scripts/_http.py)
    _http.install(args.base_url)

    media_types = tuple(args.media_types) if args.media_types else DEFAULT_MEDIA_TYPES
    slugs = set(args.slugs) if args.slugs else None

    db.init_db()
    rows = candidate_rows(media_types, slugs)
    by_type = {mt: sum(1 for r in rows if r["media_type"] == mt) for mt in media_types}
    print("=" * 70)
    print(f"Undated {'/'.join(media_types)} rows with a stored file: {len(rows)}  "
          + "  ".join(f"{mt}={n}" for mt, n in by_type.items()))
    if slugs:
        unknown = sorted(slugs - {r["slug"] for r in rows})
        if unknown:
            print(f"  (--slug not among candidates — unknown, already dated, or not {'/'.join(media_types)}: {unknown})")
    if args.dry_run:
        print("--dry-run set: no writes will be made.")
    print("=" * 70)

    # #806: the same cutoff the live upload path hands resolve_file_date (timeline.first_upload()),
    # read once. It comes from the target's own database, which this process shares by design.
    first_upload_at = timeline.first_upload()
    stats = process_rows(rows, media_types, first_upload_at, args.dry_run,
                         lambda slug, epoch, record: write_content_date(args.base_url, slug, epoch, record))
    written, no_date, skipped_raced, flagged = (stats["written"], stats["no_date"],
                                                stats["skipped_raced"], stats["flagged"])

    print("=" * 70)
    would = "Would write" if args.dry_run else "Wrote"
    dated = sum(1 for r in rows) - sum(no_date.values()) - skipped_raced
    print(f"{would} content_date on {dated if args.dry_run else written} row(s).  "
          + "  ".join(f"{mt}: {by_type[mt] - no_date[mt]} dated / {no_date[mt]} without embedded date" for mt in media_types))
    if flagged:
        print(f"{flagged} of those carry #769's possible-export-time flag (suspect_export_time): the date is "
              "applied, and the item page shows the note beside it so it can be checked.")
    if skipped_raced:
        print(f"Skipped {skipped_raced} row(s) that gained a content_date between enumeration and write.")
    print("=" * 70)


def process_rows(rows, media_types, first_upload_at, dry_run, write):
    """The per-row loop, split out of main() so a test can drive it with its own `write`
    (slug, epoch, record). Returns {"written", "no_date", "skipped_raced", "flagged"}. `flagged`
    counts rows whose record says suspect_export_time (dry run: would be; real run: were)."""
    written = 0
    flagged = 0
    no_date = {mt: 0 for mt in media_types}
    skipped_raced = 0
    for row in rows:
        slug, media_type, filename = row["slug"], row["media_type"], row.get("filename")
        epoch, record = resolve_row(row, first_upload_at)
        if epoch is None:
            no_date[media_type] += 1
            print(f"  -    {slug} {media_type:5} {filename!r}: no usable embedded date")
            continue
        suspect = bool(record and record.get("suspect_export_time"))
        source = f" [{record['source']}]" if record else ""
        note = f"  ** {record['suspect_note']}" if suspect else ""
        print(f"  {'DRY' if dry_run else 'SET'}  {slug} {media_type:5} {filename!r}: {_describe(epoch)}{source}{note}")
        if dry_run:
            flagged += suspect
            continue
        # Re-read right before writing: the candidate list above is a
        # snapshot, and the whole point is never to overwrite a date that
        # got there by any other route in the meantime.
        current = db.get_by_slug(slug)
        if current is None or current.get("content_date") is not None:
            skipped_raced += 1
            print(f"       ^ skipped: row gone or content_date set since enumeration")
            continue
        write(slug, epoch, record)
        written += 1
        flagged += suspect
    return {"written": written, "no_date": no_date, "skipped_raced": skipped_raced, "flagged": flagged}


if __name__ == "__main__":
    main()
