"""YouTube oEmbed metadata fetch (issue #44).

#44 asked, in order: (1) figure out what's actually fetchable with no API
key before assuming anything, then (2) decide what to keep. Investigated by
hitting the real public oEmbed endpoint --

    https://www.youtube.com/oembed?url=<watch-url>&format=json

-- with two real videos while building this (a hooptiej upload and an
unrelated public one). The real response shape, confirmed empirically, is:

    title, author_name, author_url, type, height, width, version,
    provider_name, provider_url, thumbnail_height, thumbnail_width,
    thumbnail_url, html

Neither response included a description or any play/view count field, and
none of oEmbed's documented fields cover them either. Both were explicitly
asked for in #44 but are NOT available without the full YouTube Data API
(needs an API key we don't have) -- scraping the watch page for them was
explicitly ruled out in the issue as fragile, so this module does not
attempt either. That's a real, standing limitation, not an oversight.

What's actually kept, and why:
  - title: used to default content_description when the caller didn't
    supply one -- today's manual-add UI (#25) only takes a bare URL, so a
    hand-added video previously fell all the way back to displaying its
    slug (see web/app.py's _to_object_detail / _to_public display_name
    chain) with nothing readable at all.
  - author_name: the channel's display name. Skipped entirely (not stored,
    not shown) when it matches OWNER_CHANNEL_NAME -- confirmed via a real
    fetch that hooptiej's own uploads report author_name "hooptiej", so
    storing/showing it there would just repeat the site owner's own name on
    every single video page, exactly what #44 said to avoid. Stored in
    type_metadata (not a dedicated column) as this is a youtube-specific
    field with no use anywhere else.
  - thumbnail_url is deliberately NOT used from here -- core/object_types.py's
    youtube_thumbnail_url already derives a static thumbnail URL directly
    from the video ID with zero extra HTTP request, so re-fetching it via
    oEmbed would just be a slower way to get something we already have.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

OEMBED_ENDPOINT = "https://www.youtube.com/oembed"

# The site's own YouTube channel (see scripts/import_new_youtube_from_channel_rss.py's
# DEFAULT_CHANNEL_ID for the same channel by id) -- compared case-insensitively
# against oEmbed's author_name so a hooptiej upload never surfaces "By hooptiej"
# on its own detail page.
OWNER_CHANNEL_NAME = "hooptiej"

FETCH_TIMEOUT_SECONDS = 5


def fetch_oembed(video_url):
    """Raw oEmbed payload for `video_url`, or None on any failure (network
    error, non-200, malformed JSON, timeout, etc.). Best-effort enrichment
    only -- a failure here must never block creating the object itself."""
    if not video_url:
        return None
    query = urllib.parse.urlencode({"url": video_url, "format": "json"})
    req = urllib.request.Request(
        f"{OEMBED_ENDPOINT}?{query}",
        headers={"User-Agent": "Constructicon/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, UnicodeDecodeError, OSError):
        return None


def youtube_metadata_for_content(video_url):
    """Distills a fetch_oembed() payload into what #44 decided to keep:
    (title, type_metadata_dict). title is None if the fetch failed or
    oEmbed returned no title; type_metadata_dict is {} unless author_name
    was present and isn't the site's own channel. Callers merge the dict
    into the row's existing type_metadata rather than assuming it's the
    whole thing."""
    data = fetch_oembed(video_url)
    if not data:
        return None, {}
    title = (data.get("title") or "").strip() or None
    author_name = (data.get("author_name") or "").strip() or None
    metadata = {}
    if author_name and author_name.lower() != OWNER_CHANNEL_NAME.lower():
        metadata["youtube_author_name"] = author_name
    return title, metadata
