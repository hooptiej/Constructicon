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
    instance's DB and storage (docker exec into the app's own container).
  - Extraction is core.embedded_metadata.extract — literally the code
    path a fresh upload goes through, not a re-implementation — filtered
    through the same plausibility gate fill_missing applies.
  - Writes go through POST /api/image/{slug}'s content_date field (the
    surface the YouTube sync's publishedAt correction already uses), not
    a direct UPDATE. Only description/tags/client-free payloads are sent:
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
    (Mountain Time and UTC) and makes no writes. --slug restricts the run
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

from core import db, embedded_metadata, timeline  # noqa: E402

DEFAULT_MEDIA_TYPES = ("image", "video")


def candidate_rows(media_types, slugs=None):
    """Every row of the given media_types with a stored file and no
    content_date yet — optionally narrowed to specific slugs. Oldest
    first, so a partial run (Ctrl-C) leaves the backlog's tail, not its
    head, for next time."""
    rows = []
    for row in db.search(limit=1000000):
        if row.get("media_type") not in media_types:
            continue
        if slugs and row["slug"] not in slugs:
            continue
        if row.get("content_date") is not None or not row.get("stored_filename"):
            continue
        rows.append(row)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def write_content_date(base_url, slug, epoch):
    """POST /api/image/{slug} with only content_date — see the module
    docstring for why nothing else is resubmitted."""
    url = f"{base_url.rstrip('/')}/api/image/{slug}"
    data = urllib.parse.urlencode({"content_date": repr(epoch)}).encode("utf-8")
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

    written = 0
    no_date = {mt: 0 for mt in media_types}
    skipped_raced = 0
    for row in rows:
        slug, media_type, filename = row["slug"], row["media_type"], row.get("filename")
        found = embedded_metadata.extract(row)
        epoch = embedded_metadata.plausible_content_date(found.get("content_date"))
        if epoch is None:
            no_date[media_type] += 1
            print(f"  -    {slug} {media_type:5} {filename!r}: no usable embedded date")
            continue
        print(f"  {'DRY' if args.dry_run else 'SET'}  {slug} {media_type:5} {filename!r}: {_describe(epoch)}")
        if args.dry_run:
            continue
        # Re-read right before writing: the candidate list above is a
        # snapshot, and the whole point is never to overwrite a date that
        # got there by any other route in the meantime.
        current = db.get_by_slug(slug)
        if current is None or current.get("content_date") is not None:
            skipped_raced += 1
            print(f"       ^ skipped: row gone or content_date set since enumeration")
            continue
        write_content_date(args.base_url, slug, epoch)
        written += 1

    print("=" * 70)
    would = "Would write" if args.dry_run else "Wrote"
    dated = sum(1 for r in rows) - sum(no_date.values()) - skipped_raced
    print(f"{would} content_date on {dated if args.dry_run else written} row(s).  "
          + "  ".join(f"{mt}: {by_type[mt] - no_date[mt]} dated / {no_date[mt]} without embedded date" for mt in media_types))
    if skipped_raced:
        print(f"Skipped {skipped_raced} row(s) that gained a content_date between enumeration and write.")
    print("=" * 70)


if __name__ == "__main__":
    main()
