"""YouTube video type spec and registration.

YouTube videos are external content (stored only as an external_url, not as
a local file). Thumbnails are fetched from YouTube's static hqdefault URL
(requires video ID extraction from various YouTube URL formats), and the
content is OCR-capable (can extract text from the video's description and
other metadata).
"""

import re

from . import register, ObjectTypeSpec, ThumbnailSource, MetadataField


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


register(ObjectTypeSpec(
    key="youtube",
    label="YouTube video",
    thumbnail_source=ThumbnailSource.FETCH_URL,
    thumbnail_url_fn=youtube_thumbnail_url,
    ocr_capable=True,
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
))
