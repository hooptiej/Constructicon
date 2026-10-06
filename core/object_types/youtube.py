"""YouTube video type spec and registration.

YouTube videos are external content (stored only as an external_url, not as
a local file). Thumbnails are fetched from YouTube's static hqdefault URL
(requires video ID extraction from various YouTube URL formats), and the
content is OCR-capable (can extract text from the video's description and
other metadata).
"""

import json
import logging
import re
from datetime import datetime
from markupsafe import Markup, escape

from .. import besteffort
from . import register, ObjectTypeSpec, ThumbnailSource, MetadataField, TypeAction

log = logging.getLogger("constructicon.youtube")
from . import _preview


YOUTUBE_ID_RE = re.compile(r"(?:v=|/embed/|youtu\.be/)([A-Za-z0-9_-]{6,})")


def extract_youtube_id(url):
    """Video ID out of any of the URL shapes we might have stored in
    external_url (watch?v=, youtu.be/, or an already-embed URL). Returns
    None if `url` doesn't look like a YouTube link at all. Centralized here
    so both the embed-player URL (web/app.py) and the static-thumbnail URL
    below are built from the same extraction, instead of two regexes that
    could drift apart."""
    if not url:
        return None
    m = YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


def youtube_thumbnail_url(external_url):
    """YouTube serves a static thumbnail for any video at a predictable,
    unauthenticated URL — no API key, no extra request to look one up.
    hqdefault is available for effectively every video (maxresdefault isn't,
    for older/lower-res uploads)."""
    video_id = extract_youtube_id(external_url)
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else None


def matches(url):
    """Return True if the URL looks like a YouTube link (has an extractable
    video ID), False otherwise. Used by classify_url() to detect YouTube
    content before falling back to the generic 'url' type."""
    return bool(extract_youtube_id(url))


def embed_url(external_url):
    """Builds a canonical embed URL from whatever YouTube URL shape is
    stored in external_url. Returns None if it doesn't look like a YouTube
    link at all — the template falls back to a plain external-link CTA in
    that case rather than rendering a broken iframe. This is the single
    parser for both live and export; replaces web/app.py's and site_export's
    copies."""
    video_id = extract_youtube_id(external_url)
    return f"https://www.youtube.com/embed/{video_id}" if video_id else None


def fetch_published_date(video_id, api_key):
    """Single-video counterpart to scripts/full_youtube_channel_sync.py's
    batch fetch_video_metadata (#275) -- looks up just one video's real
    publishedAt via the YouTube Data API, for the per-item "fetch real
    date" button below, rather than needing that whole-channel batch
    script for a one-off correction. Returns epoch seconds, or None if the
    video has no publishedAt / isn't found (deleted or private).
    Raises RuntimeError on an actual API failure (bad key, network, etc.)
    so the caller can report a real error instead of silently no-op'ing.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
        {"part": "snippet", "id": video_id, "key": api_key}
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"YouTube Data API request failed ({e.code})") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach the YouTube Data API ({e.reason})") from e
    items = data.get("items") or []
    if not items:
        return None
    published_at = items[0].get("snippet", {}).get("publishedAt")
    if not published_at:
        return None
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError) as e:
        besteffort.warn(log, "youtube: the API's publishedAt isn't a date (no publish date)", e, value=published_at)
        return None


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='youtube' — view/like/comment
    counts, channel author, and video ID, or {} on any failure."""
    type_metadata = row.get("type_metadata") or {}
    props = {}

    try:
        if type_metadata.get("view_count") is not None:
            view_count = type_metadata["view_count"]
            if isinstance(view_count, str):
                view_count = int(view_count)
            props["Views"] = f"{view_count:,}"
    except (TypeError, ValueError):
        print(f"YouTube properties: couldn't format view_count for {row.get('slug')}")

    try:
        if type_metadata.get("like_count") is not None:
            like_count = type_metadata["like_count"]
            if isinstance(like_count, str):
                like_count = int(like_count)
            props["Likes"] = f"{like_count:,}"
    except (TypeError, ValueError):
        print(f"YouTube properties: couldn't format like_count for {row.get('slug')}")

    try:
        if type_metadata.get("comment_count") is not None:
            comment_count = type_metadata["comment_count"]
            if isinstance(comment_count, str):
                comment_count = int(comment_count)
            props["Comments"] = f"{comment_count:,}"
    except (TypeError, ValueError):
        print(f"YouTube properties: couldn't format comment_count for {row.get('slug')}")

    if type_metadata.get("author"):
        props["Channel"] = type_metadata["author"]

    video_id = extract_youtube_id(row.get("external_url"))
    if video_id:
        props["Video ID"] = video_id

    return props


def preview(ctx):
    """#449 preview_fn: YouTube embed player (live) or export iframe.
    Includes Full description block in live mode if type_metadata.description exists."""
    url = embed_url(ctx.item.get("external_url"))
    if not url:
        return None

    html = str(_preview.youtube_embed(ctx, url))

    # In live mode, append Full description <details> if present
    if ctx.mode == "live" and (ctx.item.get("type_metadata") or {}).get("description"):
        description = escape((ctx.item.get("type_metadata") or {}).get("description", ""))
        details_html = (
            '<details class="ocr-disclosure" style="display:block;margin-top:6px">'
            '<summary>'
            '<svg class="disclosure-caret" width="10" height="10" viewBox="0 0 24 24" fill="none">'
            '<path d="M9 6L15 12L9 18" stroke="#A8A48F" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg>'
            'Full description'
            '</summary>'
            f'<pre class="ocr-text mono">{description}</pre>'
            '</details>'
        )
        html += details_html

    return Markup(html)


def _fetch_real_date(row):
    """Action handler for 'fetch-real-date': re-fetch the real publishedAt
    from the YouTube Data API and update content_date."""
    from .. import db  # Lazy import to avoid circular dependency

    video_id = extract_youtube_id(row.get("external_url"))
    if not video_id:
        raise RuntimeError("Couldn't determine this item's YouTube video id")

    api_key = db.get_setting("youtube_data_api_key")
    if not api_key:
        raise RuntimeError("No YouTube Data API key is set (see the admin page's API Keys section)")

    published = fetch_published_date(video_id, api_key)
    if published is None:
        raise RuntimeError("YouTube has no published date for this video (it may be deleted or private)")

    db._set_content_date(row["slug"], published)
    return {"content_date": published}


register(ObjectTypeSpec(
    key="youtube",
    label="YouTube video",
    thumbnail_source=ThumbnailSource.FETCH_URL,
    thumbnail_url_fn=youtube_thumbnail_url,
    ocr_capable=True,
    url_match_fn=matches,  # #448: content-based URL classification
    # #54: populated by scripts/full_youtube_channel_sync.py from the
    # real YouTube Data API v3 (videos.list's snippet.description and
    # statistics.*) — see that script's module docstring for why these
    # four keys specifically, and web/app.py's update_content_metadata
    # usage for how a row's content_description (the video's title) and
    # this type_metadata get corrected/populated together. `author` is
    # only ever set when the uploading channel ISN'T hooptiej's own —
    # the sync script deliberately omits it otherwise so every single
    # video doesn't carry a redundant "author: hooptiej".
    metadata_fields=(
        MetadataField("view_count", "View count"),
        MetadataField("like_count", "Like count"),
        MetadataField("comment_count", "Comment count"),
        MetadataField("description", "Full description (from the YouTube Data API)"),
        MetadataField("author", "Uploading channel — only set when it isn't the owner's own channel"),
    ),
    badge_icon="▶️",
    badge_text="YOUTUBE",
    preview_fn=preview,
    properties_fn=get_properties,
    external_link_label="Watch on YouTube ↗",
    actions=(TypeAction("fetch-real-date", "Fetch real date", _fetch_real_date),),
))
