"""Imgur import type spec and registration (issue #200).

Imgur content is external (no local file) — pulled in from the owner's
public gallery submissions by core/imgur_import.py, triggered from the
upload drawer's "Import from Imgur" button (see web/app.py's
/api/imgur/import). external_url always stores a direct i.imgur.com image
link — for an album submission, that's the album's first/cover image,
with the album's own page link and image count recorded in type_metadata
instead (see imgur_import.py's _normalize) rather than needing a second
thumbnail strategy for albums.
"""

import re

from . import register, ObjectTypeSpec, ThumbnailSource, MetadataField

# Matches a direct Imgur image link, e.g. https://i.imgur.com/AbC123d.jpg —
# group(1) is the link up to (not including) the extension, group(2) is the
# bare id, group(3) is the extension (with leading dot).
IMGUR_DIRECT_LINK_RE = re.compile(r"^(https://i\.imgur\.com/([A-Za-z0-9]+))(\.\w+)$")


def extract_imgur_id(url):
    """Imgur image id out of a direct i.imgur.com link — the only shape
    external_url ever takes for this type (see module docstring). Returns
    None if `url` doesn't look like one at all."""
    if not url:
        return None
    m = IMGUR_DIRECT_LINK_RE.match(url)
    return m.group(2) if m else None


def imgur_thumbnail_url(external_url):
    """Imgur serves resized variants of any direct image link by inserting
    a single size-letter before the extension (here: 'l', the ~640px
    "large thumbnail" size) — no API call needed, same no-extra-request
    shape as YouTube's static thumbnail URLs."""
    m = IMGUR_DIRECT_LINK_RE.match(external_url or "")
    return f"{m.group(1)}l{m.group(3)}" if m else external_url


register(ObjectTypeSpec(
    key="imgur",
    label="Imgur upload",
    thumbnail_source=ThumbnailSource.FETCH_URL,
    thumbnail_url_fn=imgur_thumbnail_url,
    ocr_capable=True,
    # Populated by core/imgur_import.py from account/{username}/submissions.
    # is_album/image_count/permalink only mean anything for an album
    # submission — for a plain single-image one they're just False/1/the
    # same link as external_url.
    metadata_fields=(
        MetadataField("is_album", "Album submission (vs. a single image)"),
        MetadataField("image_count", "Number of images in the album"),
        MetadataField("permalink", "The submission's own Imgur page (album or image)"),
    ),
    badge_icon="\U0001F5BC️",  # framed picture emoji
    badge_text="IMGUR",
))
