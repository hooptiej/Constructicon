"""Issue #22: import any hooptiej YouTube channel videos that exist on the
real channel but are NOT already represented in Constructicon, using the
new object-type content path (POST /api/content) rather than any legacy
backfill shortcut.

Why this exists / what it deliberately does NOT do
----------------------------------------------------
#22 asked for a full re-harvest of "the YouTube channel", separate from
#21's site-sourced backfill (scripts/backfill_from_hooptiej_site.py), which
only imports videos that happen to be referenced on hooptiej.github.io
(embedded in blog posts, or listed under a project category's "Where to
look"/standalone-video sections) -- a curated subset the site's owner chose
to write up, not necessarily every video that has ever existed on the
channel.

A true full-history channel harvest needs the YouTube Data API (an API key
this project doesn't have). The one no-key option is YouTube's public Atom
feed:

    https://www.youtube.com/feeds/videos.xml?channel_id=<UC...>
    https://www.youtube.com/feeds/videos.xml?user=<legacy username>

CONFIRMED LIMITATION (investigated before writing this script, same
discipline as #13/#14/#21's site-sourcing investigations): this feed is
capped at roughly the 15 most recent uploads. It is NOT a full-history
list. For the hooptiej channel (~78 videos historically, per #7/#21's own
research), this feed cannot replace a real API-key-based harvest -- it can
only ever surface recent uploads, and as of this script being written every
video in it turned out to already predate #21's import (the feed's oldest
entry is from December 2017; #21 already covers the channel's full
2017-2021 site-documented history). Cross-checking the feed's 15 video IDs
against every video ID referenced anywhere in hooptiej.github.io's blog/
and projects/ pages found exactly TWO genuinely new videos -- both titled
"BetaFlight Custom BootSplash How-to! - Cmon in and we'll make yours!"
(uploaded Feb 12 and Feb 16, 2018; ids aAYQKauKA8M and _Trkb3k0UI0) -- real
channel content the site's owner apparently never linked from the site
(likely a near-duplicate re-upload pair), not something "too new to be on
the site yet". Confirmed via a GitHub code search across
hooptiej/hooptiej.github.io for both IDs (zero hits).

Given that ~13 of the feed's 15 entries are pure overlap with #21, this
script is NOT a general-purpose "channel sync" -- it is a narrow, one-time
top-up for whatever handful of recent-upload gaps exist right now. A real
full-catalog re-harvest (verifying nothing OLDER than the RSS cap is
missing) needs a YouTube Data API key; this script cannot provide that, and
does not pretend to.

What it does
------------
1. Fetches the channel's RSS/Atom feed (--channel-id or --user; hooptiej's
   channel id is used by default: UCiPAeBVwRyCe5La0EVeSGYw).
2. Reads every existing capture_events row with media_type='youtube'
   directly from the target's own database (same DB-file-sharing
   requirement as #21/#46's script -- see their docstrings for why this
   still isn't done over HTTP: there's no read endpoint that exposes a
   row's external_url, only the write path (/api/content) has to go over
   HTTP to get real dispatch scheduling) and extracts each row's video ID
   via object_types.extract_youtube_id.
3. For every feed entry whose video ID ISN'T already present, POSTs to the
   target instance's own /api/content -- same as #46's rework of the #21
   script -- so the row gets a real ocr_status='pending' and picks up
   thumbnail fetch + OCR dispatch the same way anything a human clicks
   "add" on does. No direct db.insert_content call for content rows.
4. Prints a summary: how many feed entries were found, how many already
   existed, how many were newly imported (and their slugs).

Usage:
    python scripts/import_new_youtube_from_channel_rss.py \\
        --base-url http://localhost:80

    Must run somewhere that can also see the target's database at
    core.db.DB_PATH (e.g. docker exec into the app's own container) --
    see backfill_from_hooptiej_site.py's docstring for the identical
    constraint and reasoning.

Safe to re-run: it always re-derives "already imported" from the live DB
before deciding what to POST, so running it again after it already
imported the gap videos is a no-op (finds them already present, imports
nothing new) rather than duplicating rows the way a re-run of #21's script
would.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, object_types  # noqa: E402

UPLOADED_BY = db.source_migrated_from("youtube.com/hooptiej (channel RSS)")
CAPTURE_NOTE = "Imported from the hooptiej YouTube channel's public RSS feed (issue #22)"

# hooptiej's channel id, resolved via the channel's own RSS feed
# (?user=hooptiej redirects/resolves to this) during this issue's
# investigation -- see module docstring.
DEFAULT_CHANNEL_ID = "UCiPAeBVwRyCe5La0EVeSGYw"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def fetch_feed_entries(channel_id=None, user=None):
    """Returns an ordered list of dicts: video_id, title, published_epoch.
    Raises RuntimeError with a clear message if the feed can't be fetched
    or parsed -- this is a small, occasional script, not something that
    needs to degrade gracefully."""
    if channel_id:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    elif user:
        url = f"https://www.youtube.com/feeds/videos.xml?user={user}"
    else:
        raise ValueError("Need either channel_id or user")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't fetch channel feed ({url}): {e}") from e
    root = ET.fromstring(raw)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        video_id_el = entry.find("yt:videoId", ATOM_NS)
        title_el = entry.find("atom:title", ATOM_NS)
        published_el = entry.find("atom:published", ATOM_NS)
        if video_id_el is None or not video_id_el.text:
            continue
        entries.append({
            "video_id": video_id_el.text.strip(),
            "title": title_el.text.strip() if title_el is not None and title_el.text else "",
            "published_epoch": _parse_published(published_el.text) if published_el is not None else None,
        })
    return entries


def _parse_published(text):
    """Atom <published> is ISO-8601 with a timezone, e.g.
    '2021-01-06T20:00:00+00:00' -- stdlib datetime.fromisoformat handles
    that directly on the Python versions this project targets."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except (ValueError, AttributeError):
        return None


def existing_youtube_video_ids():
    """Every video ID already present in the target's own database, read
    directly (see module docstring for why this one read has to bypass
    HTTP -- no existing endpoint exposes external_url in its JSON shape).
    """
    ids = set()
    for row in db.search(limit=1000000):
        if row.get("media_type") != "youtube":
            continue
        vid = object_types.extract_youtube_id(row.get("external_url"))
        if vid:
            ids.add(vid)
    return ids


def create_content_row(base_url, **fields):
    """Same shape as backfill_from_hooptiej_site.py's create_content_row --
    POSTs to the target's real /api/content so thumbnail+OCR dispatch is
    scheduled the same way a human "add link" would trigger it."""
    payload = {k: v for k, v in fields.items() if v is not None}
    data = urllib.parse.urlencode(payload).encode("utf-8")
    url = f"{base_url.rstrip('/')}/api/content"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Couldn't reach {url} ({e.reason}) — is the target instance running and --base-url correct?"
        ) from e


def main():
    parser = argparse.ArgumentParser(
        description="Import any hooptiej YouTube channel videos (via the channel's public RSS "
                     "feed) not already present in Constructicon. See module docstring for the "
                     "feed's ~15-most-recent-uploads limitation."
    )
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID, help="YouTube channel id (UC...)")
    parser.add_argument("--user", default=None, help="Legacy YouTube username, instead of --channel-id")
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL of the running Constructicon instance to POST /api/content against "
             "(must share a database with this process — see module docstring)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only report what would be imported; don't POST anything.",
    )
    args = parser.parse_args()

    db.init_db()

    entries = fetch_feed_entries(channel_id=None if args.user else args.channel_id, user=args.user)
    existing_ids = existing_youtube_video_ids()

    new_entries = [e for e in entries if e["video_id"] not in existing_ids]

    print("=" * 60)
    print(f"Channel feed entries fetched: {len(entries)}")
    print(f"Already present in this instance: {len(entries) - len(new_entries)}")
    print(f"Genuinely new: {len(new_entries)}")
    for e in new_entries:
        print(f"  - {e['video_id']}: {e['title']}")
    print("=" * 60)

    if not new_entries:
        print("Nothing to import — every feed entry is already represented.")
        return
    if args.dry_run:
        print("--dry-run set: not importing.")
        return

    imported = []
    for e in new_entries:
        row = create_content_row(
            args.base_url,
            media_type="youtube",
            external_url=f"https://www.youtube.com/watch?v={e['video_id']}",
            content_description=e["title"],
            content_date=str(e["published_epoch"]) if e["published_epoch"] is not None else None,
            description=CAPTURE_NOTE,
        )
        imported.append(row["slug"])
        print(f"Imported {e['video_id']} -> slug {row['slug']}")

    print("=" * 60)
    print(f"Done. Imported {len(imported)} new row(s): {', '.join(imported)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
