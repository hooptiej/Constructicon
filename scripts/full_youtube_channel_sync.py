"""Issue #54: full-channel re-fetch of the hooptiej YouTube channel using the
real YouTube Data API v3, now that a real key exists (#55's app_settings
store).

This supersedes #22's RSS-based top-up (scripts/import_new_youtube_from_channel_rss.py),
which was explicitly a stopgap: YouTube's public Atom feed only ever exposes
the ~15 most recent uploads, so it could never do a real full-history
harvest — see that script's own docstring. With a real API key now
available, this script does the real thing #22 could not:

  1. ENUMERATE THE ENTIRE CHANNEL CATALOG. channels.list (part=contentDetails,
     snippet) resolves the channel id (default: hooptiej's channel,
     UCiPAeBVwRyCe5La0EVeSGYw — the same id #22's script already uses,
     confirmed via that script's own investigation) to its "uploads"
     playlist id and the channel's own title (needed below for the
     author-omission rule). playlistItems.list is then paginated
     (maxResults=50 per page, following nextPageToken) against that uploads
     playlist to collect literally every video id the channel has ever
     published — this is the standard low-quota-cost way to get a full
     channel's video list (1 unit per page of up to 50 items), and is used
     INSTEAD OF search.list (which the issue explicitly calls out as far
     more quota-expensive for the same result).

  2. FETCH FULL METADATA. videos.list (part=snippet,statistics), batched up
     to 50 ids per call (1 unit per call regardless of batch size) — real
     title, full description, channelTitle, publishedAt, and
     view/like/comment counts for every video found in step 1.

  3. CORRECT existing rows. Every video id already present in this
     instance's database (imported by #21/#22's site/RSS-based scripts,
     which only had a scraped blog excerpt or a bare feed title to work
     with — see #51's own investigation into which of those were wrong)
     gets its content_description (the field the rest of the app uses as
     the video's title — see core/db.py's insert_content docstring and
     object_detail.html) overwritten with the real YouTube title, via the
     new content_description/type_metadata fields on POST /api/image/{slug}
     (added alongside this script — see web/app.py's api_update_image and
     core/db.py's update_content_metadata). Matched by video id, extracted
     from external_url via object_types.extract_youtube_id — stable across
     a title/description change, so this survives #51's grouping being
     applied first and doesn't need to touch it.

  4. IMPORT everything else. Every video id from step 1 NOT already present
     gets a new capture_events row via POST /api/content — same
     server-minted-slug, real-thumbnail-and-OCR-dispatch discipline as
     #21/#22/#46/#50's scripts, no direct db.insert_content call for
     content rows. type_metadata (view/like/comment counts, full
     description, author-if-applicable) is set from the start via the new
     type_metadata form field on POST /api/content, rather than needing a
     separate follow-up call the way an existing row's correction does.

  5. RE-VERIFY #51's project groupings. Matching in #51's
     apply_project_groupings.py was by video id, which step 3 never
     changes — but "should still hold" is a claim worth checking, not
     assuming. verify_project_groupings() below re-reads projects/
     project_items after a real run and confirms the 8 projects and their
     2/5/4/5/3/3/3/3 = 28 member counts are unchanged (see that issue's own
     PR for where those numbers came from).

type_metadata schema (see core/object_types.py's "youtube" metadata_fields
for the documented, authoritative version of this):
    {
        "view_count": int,
        "like_count": int,
        "comment_count": int,
        "description": str,       # the video's full YouTube description
        "author": str,            # ONLY present when channelTitle != the
                                   # channel's own title (see below) — never
                                   # set for the channel's own uploads
    }

On "author": because every video this script ever sees comes from
enumerating the channel's OWN uploads playlist (step 1), channelTitle will
always equal the channel's own title for every real run against a real
channel — there is no code path here that could ever see a foreign
channelTitle. The comparison is still implemented (build_type_metadata
below) for correctness/documentation and in case this script is ever
pointed at a differently-populated video id list in the future, but expect
it to be a no-op (no "author" key ever written) on every real run: this is
the intended behavior per the issue's own instruction to avoid a redundant
"author: hooptiej" on every single video, not a bug.

WRITE-PATH DECISION (per the issue's explicit "investigate, don't assume"
instruction): before this script existed, there was NO way to set
type_metadata or correct content_description through the HTTP API at all —
db.set_type_metadata and content_description-on-creation both existed, but
nothing exposed either for an ALREADY-CREATED row. Rather than have this
script reach into the database directly for that (the pattern #21/#51 use
ONLY for tag_id/project_items, which the docstrings on those scripts are
explicit has "no HTTP equivalent" and "no thumbnail/OCR/dispatch behavior
riding on them" — a genuinely different situation from correcting a field
the UI itself displays), this adds two small, focused HTTP surfaces instead
(see web/app.py):
  - POST /api/content gained an optional `type_metadata` form field (JSON
    object string), threaded straight through to db.insert_content, which
    already accepted the parameter but had nothing wiring it up.
  - POST /api/image/{slug} gained optional `content_description` and
    `type_metadata` form fields, backed by a new core/db.py function,
    update_content_metadata — content_description is a plain "leave alone
    if not given" partial update (matching update_tags/rename_object's
    existing convention on that endpoint), while type_metadata is MERGED
    into whatever the row already has rather than replacing it wholesale
    (db.set_type_metadata's existing contract), so re-running this script's
    correction pass, or some future second writer of type_metadata, can't
    silently clobber a field it doesn't know about.
This keeps the "write real content fields through the real API, same as a
human editing it would" discipline #21/#46's script docstrings establish,
while being a genuinely small, additive change (two new optional Form
fields on two existing endpoints, one new db.py function) rather than a
bespoke direct-to-database write path for content that the UI itself reads
and displays.

Usage:
    python scripts/full_youtube_channel_sync.py --base-url http://localhost:80 [--dry-run]

    Must run somewhere that can also see the target's database at
    core.db.DB_PATH (e.g. docker exec into the app's own container) AND
    reach the real internet (the YouTube Data API, not just the target
    instance) — see #21/#22's scripts for the identical DB-sharing
    constraint and reasoning.

    Reads the API key via core.db.get_setting("youtube_data_api_key")
    directly — NOT via GET /api/settings, which deliberately only ever
    reports presence, never the real value (see #55's db.get_setting
    docstring and web/app.py's api_get_settings).

Idempotent / safe to re-run: corrections always re-derive from the live
YouTube API response and overwrite with the (possibly identical) current
values — running this again immediately is a real no-op write, not a
skip, but produces the same end state either way. Imports always re-check
the live DB's existing video ids first (same as #22's script), so a video
already imported by a prior run is never duplicated.

DO NOT run this against the real production instance/database without the
owner's separate go-ahead — see this script's own PR description for the
exact command that would eventually do so; issue #54's implementation work
is scoped to testing against the isolated constructicon-test container.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, object_types  # noqa: E402

UPLOADED_BY_NOTE = db.source_migrated_from("YouTube Data API v3 (full channel sync)")
CAPTURE_NOTE = "Imported via the YouTube Data API v3 full-channel sync (issue #54)"

# hooptiej's channel id — same one #22's RSS-based script resolved and used
# (see that script's docstring for how it was confirmed).
DEFAULT_CHANNEL_ID = "UCiPAeBVwRyCe5La0EVeSGYw"

API_BASE = "https://www.googleapis.com/youtube/v3"
PLAYLIST_ITEMS_PAGE_SIZE = 50  # YouTube Data API's own max per playlistItems.list call
VIDEOS_BATCH_SIZE = 50  # YouTube Data API's own max ids per videos.list call

# #51's confirmed groupings (see scripts/apply_project_groupings.py) —
# re-checked after this script runs a real import/correction pass, since
# matching is by video id (stable across a title/description correction)
# but "should still hold" deserves verification, not assumption.
EXPECTED_PROJECT_COUNT = 8
EXPECTED_PROJECT_MEMBER_COUNTS = sorted([2, 5, 4, 5, 3, 3, 3, 3])
EXPECTED_PROJECT_TOTAL_MEMBERS = sum(EXPECTED_PROJECT_MEMBER_COUNTS)


def _api_get(endpoint, params):
    url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YouTube Data API {endpoint} call failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach the YouTube Data API ({e.reason})") from e


def get_channel_info(api_key, channel_id=None, handle=None):
    """Resolves a channel (by id or handle) to its uploads playlist id and
    its own title — channels.list part=contentDetails,snippet, 1 unit."""
    params = {"part": "contentDetails,snippet", "key": api_key}
    if handle:
        params["forHandle"] = handle
    else:
        params["id"] = channel_id
    data = _api_get("channels", params)
    items = data.get("items") or []
    if not items:
        target = handle or channel_id
        raise RuntimeError(f"No YouTube channel found for {target!r}")
    channel = items[0]
    uploads_playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    channel_title = channel["snippet"]["title"]
    return uploads_playlist_id, channel_title


def list_all_video_ids(api_key, uploads_playlist_id):
    """Paginates playlistItems.list against the channel's uploads playlist
    to collect EVERY video id in upload history — 1 unit per page of up to
    50 items, per the issue's explicit instruction to use this instead of
    the far costlier search.list for the same result."""
    ids = []
    page_token = None
    while True:
        params = {
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": PLAYLIST_ITEMS_PAGE_SIZE,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _api_get("playlistItems", params)
        for item in data.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                ids.append(video_id)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_video_metadata(api_key, video_ids):
    """videos.list in batches of up to 50 ids (1 unit per call regardless of
    batch size) for snippet + statistics. Returns {video_id: metadata dict}
    — a video id with no entry in the result (deleted/private since being
    enumerated) is simply absent, and callers skip it rather than erroring
    the whole run over one gone video."""
    metadata = {}
    for i in range(0, len(video_ids), VIDEOS_BATCH_SIZE):
        batch = video_ids[i:i + VIDEOS_BATCH_SIZE]
        data = _api_get("videos", {"part": "snippet,statistics", "id": ",".join(batch), "key": api_key})
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            metadata[item["id"]] = {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt"),
                "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
                "like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
                "comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
            }
    return metadata


def _parse_published(text):
    """RFC3339/ISO-8601 with a Z suffix ('2021-01-06T20:00:00Z') ->
    epoch seconds. Same stdlib approach as #22's script, adjusted for the
    literal 'Z' videos.list actually returns (fromisoformat needs
    +00:00, not Z, on the Python versions this project targets)."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def build_type_metadata(meta, owner_channel_title):
    """See module docstring's "type_metadata schema" / "On 'author'"
    sections. Only ever includes keys that actually have a value — an
    empty dict here means db.update_content_metadata's merge is a no-op
    for that call, never a wipe of the row's existing type_metadata."""
    md = {}
    if meta.get("view_count") is not None:
        md["view_count"] = meta["view_count"]
    if meta.get("like_count") is not None:
        md["like_count"] = meta["like_count"]
    if meta.get("comment_count") is not None:
        md["comment_count"] = meta["comment_count"]
    if meta.get("description"):
        md["description"] = meta["description"]
    channel_title = meta.get("channel_title")
    if channel_title and channel_title != owner_channel_title:
        md["author"] = channel_title
    return md


def existing_youtube_rows():
    """video_id -> slug for every existing media_type='youtube' row, read
    directly from the target's own database — no HTTP endpoint exposes
    external_url in its JSON shape, same constraint #22's script
    documents."""
    mapping = {}
    for row in db.search(limit=1000000):
        if row.get("media_type") != "youtube":
            continue
        video_id = object_types.extract_youtube_id(row.get("external_url"))
        if video_id:
            mapping[video_id] = row["slug"]
    return mapping


def _http_call(method, url, payload):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach {url} ({e.reason}) — is the target instance running and --base-url correct?") from e


def get_current_row(base_url, slug):
    """GET /api/image/{slug} — used before a correction so
    description/tags/ticket_id/client (fields api_update_image always
    applies, even when unrelated to this script's change — see that
    endpoint's signature) can be resubmitted unchanged instead of being
    blanked out."""
    url = f"{base_url.rstrip('/')}/api/image/{slug}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach {url} ({e.reason})") from e


def create_content_row(base_url, **fields):
    payload = {k: v for k, v in fields.items() if v is not None}
    return _http_call("POST", f"{base_url.rstrip('/')}/api/content", payload)


def correct_content_row(base_url, slug, current, content_description, type_metadata):
    """POST /api/image/{slug} — resubmits the row's CURRENT
    description/tags/ticket_id/client verbatim (see get_current_row) so
    this only actually changes content_description + type_metadata, per
    api_update_image's "always applies description/tags/ticket_id/client"
    contract."""
    payload = {
        "description": current.get("description") or "",
        "tags": json.dumps(current.get("tags") or []),
        "ticket_id": current.get("ticket_id") or "",
        "client": current.get("client") or "",
        "content_description": content_description,
        "type_metadata": json.dumps(type_metadata),
    }
    return _http_call("POST", f"{base_url.rstrip('/')}/api/image/{slug}", payload)


def verify_project_groupings():
    """Re-checks #51's groupings after a real run — see module docstring's
    step 5. Returns (ok, project_count, sorted_member_counts, total)."""
    projects = db.list_projects()
    counts = sorted(len(db.list_project_items(p["id"])) for p in projects)
    total = sum(counts)
    ok = (
        len(projects) == EXPECTED_PROJECT_COUNT
        and counts == EXPECTED_PROJECT_MEMBER_COUNTS
        and total == EXPECTED_PROJECT_TOTAL_MEMBERS
    )
    return ok, len(projects), counts, total


def main():
    parser = argparse.ArgumentParser(
        description="Full-channel YouTube Data API v3 sync (#54): enumerate every video the "
                     "channel has ever uploaded, correct existing rows' title/description/stats "
                     "with real data, and import everything not already present."
    )
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID, help="YouTube channel id (UC...)")
    parser.add_argument("--handle", default=None, help="YouTube channel handle (e.g. '@hooptiej'), instead of --channel-id")
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL of the running Constructicon instance to POST /api/content and "
             "/api/image/<slug> against (must share a database with this process)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; make no writes.")
    args = parser.parse_args()

    db.init_db()

    api_key = db.get_setting("youtube_data_api_key")
    if not api_key:
        print("No youtube_data_api_key is set (see the admin pane's API Keys section / POST /api/settings) — nothing to do.")
        sys.exit(1)

    uploads_playlist_id, owner_channel_title = get_channel_info(api_key, channel_id=args.channel_id, handle=args.handle)
    print("=" * 70)
    print(f"Channel: {owner_channel_title!r}  (uploads playlist {uploads_playlist_id})")

    all_video_ids = list_all_video_ids(api_key, uploads_playlist_id)
    print(f"Total videos found on the real channel: {len(all_video_ids)}")

    metadata = fetch_video_metadata(api_key, all_video_ids)
    missing_metadata = [v for v in all_video_ids if v not in metadata]
    if missing_metadata:
        print(f"  (note: {len(missing_metadata)} video id(s) had no videos.list metadata — deleted/private since enumeration; skipped)")

    existing = existing_youtube_rows()
    to_correct = [v for v in all_video_ids if v in existing and v in metadata]
    to_import = [v for v in all_video_ids if v not in existing and v in metadata]

    print(f"Already present in this instance (will correct): {len(to_correct)}")
    print(f"Not present in this instance (will import):       {len(to_import)}")
    print("=" * 70)

    if args.dry_run:
        print("--dry-run set: no writes will be made.\n")
        print("Sample corrections (existing rows -> real title):")
        for video_id in to_correct[:8]:
            m = metadata[video_id]
            print(f"  {video_id} (slug {existing[video_id]}): {m['title']!r}")
        if len(to_correct) > 8:
            print(f"  ... and {len(to_correct) - 8} more")
        print("\nSample new imports:")
        for video_id in to_import[:8]:
            m = metadata[video_id]
            print(f"  {video_id}: {m['title']!r}")
        if len(to_import) > 8:
            print(f"  ... and {len(to_import) - 8} more")
        return

    corrected = 0
    for video_id in to_correct:
        m = metadata[video_id]
        slug = existing[video_id]
        current = get_current_row(args.base_url, slug)
        if current is None:
            print(f"  WARNING: slug {slug} (video {video_id}) not found via the API — skipping correction")
            continue
        type_md = build_type_metadata(m, owner_channel_title)
        correct_content_row(args.base_url, slug, current, m["title"], type_md)
        corrected += 1
        print(f"Corrected {video_id} -> slug {slug}: {m['title']!r}")

    imported = []
    for video_id in to_import:
        m = metadata[video_id]
        type_md = build_type_metadata(m, owner_channel_title)
        row = create_content_row(
            args.base_url,
            media_type="youtube",
            external_url=f"https://www.youtube.com/watch?v={video_id}",
            content_description=m["title"],
            content_date=str(_parse_published(m["published_at"])) if _parse_published(m["published_at"]) else None,
            description=CAPTURE_NOTE,
            type_metadata=json.dumps(type_md),
        )
        imported.append(row["slug"])
        print(f"Imported {video_id} -> slug {row['slug']}: {m['title']!r}")

    print("=" * 70)
    print(f"Done. Corrected {corrected} existing row(s), imported {len(imported)} new row(s).")

    ok, project_count, counts, total = verify_project_groupings()
    print("=" * 70)
    print("#51 project grouping check:")
    print(f"  Projects: {project_count} (expected {EXPECTED_PROJECT_COUNT})")
    print(f"  Member counts (sorted): {counts} (expected {EXPECTED_PROJECT_MEMBER_COUNTS})")
    print(f"  Total members: {total} (expected {EXPECTED_PROJECT_TOTAL_MEMBERS})")
    print(f"  {'OK — groupings intact.' if ok else 'MISMATCH — investigate before considering this done.'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
