"""Issue #165: backfill real tag-tree entries for items tagged before the fix.

Background
----------
Before #165, typing a tag into an item's TAGS box (single-item save or the
Unfiled bulk-tag flow) only ever wrote to `capture_events.tags` -- a
free-text JSON column invisible to /api/tags (autocomplete) and the home
page's tag-tree browsing/pills. db.update_tags() now also syncs the real
blog_tags/post_tags tables (see db.sync_real_tags_for_post), but that only
takes effect going forward. Items tagged before the fix have tags sitting
only in the free-text column with no corresponding blog_tags row.

This script finds every such item and re-syncs it.

Matching strategy
------------------
Reads directly via core.db (in-process, same pattern as
apply_project_groupings.py's db.search(limit=...) discovery pass) rather
than a paginated HTTP listing endpoint -- this repo doesn't have a "list
every item with its tags" API, and this is a one-off backfill, not a
recurring write path. The actual mutation goes through the running
instance's own POST /api/image/{slug} (same call an object detail page's
"Save changes" button makes), so it exercises the exact write path
db.update_tags()/sync_real_tags_for_post go through -- no direct DB writes.

Idempotency
-----------
Safe to re-run: db.sync_real_tags_for_post's full-replace semantics mean
re-submitting an item's already-synced tags is a no-op (attach_tags uses
INSERT OR IGNORE, detach_tag only removes tags no longer in the list).

Usage
-----
Run against a running instance:
    python scripts/sync_free_text_tags_to_tag_tree.py --base-url http://localhost:8000
"""

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402


def resync_tags(base_url, slug, description, tags, client):
    payload = {
        "description": description or "",
        "tags": __import__("json").dumps(tags),
    }
    if client:
        payload["client"] = client
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/image/{slug}", data=data, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"POST /api/image/{slug} failed ({e.code}): {e.read().decode('utf-8', errors='replace')}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach {base_url} ({e.reason})") from e


def main():
    parser = argparse.ArgumentParser(
        description="Backfill real tag-tree entries for items tagged before #165."
    )
    parser.add_argument(
        "--base-url", default="http://localhost:8000",
        help="Base URL of the running Constructicon instance (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    rows = [r for r in db.search(limit=1000000) if r.get("tags")]
    if not rows:
        print("No items with free-text tags found.")
        return

    synced, failed = 0, 0
    for row in rows:
        slug = row["slug"]
        print(f"  ⇨ {slug} ({row.get('filename') or row.get('content_description') or 'untitled'}): tags={row['tags']}")
        try:
            resync_tags(args.base_url, slug, row.get("description"), row["tags"], row.get("client"))
            synced += 1
        except RuntimeError as e:
            print(f"    ✗ Failed: {e}", file=sys.stderr)
            failed += 1

    print()
    print(f"Summary: synced {synced}, failed {failed}")


if __name__ == "__main__":
    main()
