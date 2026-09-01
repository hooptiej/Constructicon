"""Registry of object/file types Constructicon can store.

Every capture_events row has a `media_type` (see core/db.py's SCHEMA comment
for the full loose vocabulary — 'image', 'youtube', 'document', and
whatever a caller invents next). This module is the single place that
describes, per type:

  - what per-type metadata fields exist beyond the generic capture_events
    columns (stored in the row's `type_metadata` JSON column — see
    db.set_type_metadata — rather than one-off ALTER TABLEs per type)
  - how to obtain a representative thumbnail image for the type (an
    uploaded file IS the image, a URL is fetched, or an image is captured
    on demand — see ThumbnailSource)
  - whether that representative image should be run through OCR the same
    way an uploaded screenshot already is

core/thumbnails.py and core/ocr.py both dispatch purely off the spec
returned by get_object_type() — neither one should ever grow a literal
`if media_type == "some_new_type"` branch. Adding a new object type (PDF:
issue #13, STL: issue #14, and whatever comes after) means adding one
ObjectTypeSpec below plus, if it needs one, a thumbnail_url_fn or
capture_fn. Nothing else in the codebase should need to change.
"""

import re
from dataclasses import dataclass
from enum import Enum

from . import pdf as _pdf


class ThumbnailSource(Enum):
    """How a type's representative thumbnail image is obtained."""

    UPLOADED_FILE = "uploaded_file"  # the row's own stored_filename IS the image (today: 'image' uploads)
    FETCH_URL = "fetch_url"          # download a representative image from a URL derived from external_url (e.g. YouTube's static thumbnail)
    CAPTURE = "capture"              # run a capture routine to produce one on demand (a stream's OSD/wait-card frame, a URL's screenshot, a rendered PDF/STL preview)
    NONE = "none"                    # no thumbnail concept for this type (e.g. a plain text/document post)


@dataclass(frozen=True)
class MetadataField:
    """Documents one per-type property stored in a row's `type_metadata`
    JSON blob (see db.set_type_metadata) rather than as a dedicated column.
    Purely descriptive today — the standard a type's fields are recorded
    against — not yet wired into form generation or validation; that's for
    whichever follow-on issue first needs it."""

    key: str
    label: str
    required: bool = False
    help_text: str = ""


@dataclass(frozen=True)
class ObjectTypeSpec:
    key: str
    label: str
    thumbnail_source: ThumbnailSource
    ocr_capable: bool  # should this type's representative image be run through OCR, same as an uploaded screenshot
    metadata_fields: tuple = ()  # tuple[MetadataField, ...] — per-type properties, stored in type_metadata
    # For thumbnail_source == FETCH_URL: (external_url) -> image URL str, or None if not derivable.
    thumbnail_url_fn: object = None
    # For thumbnail_source == CAPTURE: (row: dict) -> raw image bytes, or None on failure.
    # None here just means "not implemented yet" — core/thumbnails.py treats
    # that as "no thumbnail available" rather than an error, so registering
    # a CAPTURE-sourced type ahead of its capture routine existing is safe.
    capture_fn: object = None
    # (row: dict) -> extracted text str, or None/"" if this row has no usable
    # embedded text layer. core/ocr.py tries this FIRST, before ever running
    # OCR — a text-layer PDF (issue #13) is the first type to use this, but
    # it's generic: any future type with its own text layer (e.g. a .docx)
    # registers one here instead of ocr.py growing a per-type branch. Only
    # meaningful when ocr_capable=True; leave None for types that have no
    # text layer to try (an uploaded screenshot goes straight to OCR, same
    # as always).
    text_extract_fn: object = None
    # Issue #12: what the "file kind" badge shown on gallery tiles and the
    # object detail page looks like for this type. badge_icon is a single
    # glyph/emoji for the compact tile-corner badge; badge_text is a short
    # (~3-6 char) uppercase label used on the detail page (and as the tile
    # badge's title/tooltip). Both are plain strings so web/app.py can pass
    # them straight through to templates — no per-media_type branching
    # needed there or in the templates. A future type (PDF: #13, STL: #14)
    # just fills these in alongside the rest of its ObjectTypeSpec and gets
    # a badge for free.
    badge_icon: str = "\U0001F4E6"  # package emoji — generic fallback
    badge_text: str = "FILE"


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


OBJECT_TYPES = {
    "image": ObjectTypeSpec(
        key="image",
        label="Image",
        thumbnail_source=ThumbnailSource.UPLOADED_FILE,
        ocr_capable=True,
        badge_icon="\U0001F5BC️",
        badge_text="IMAGE",
    ),
    "youtube": ObjectTypeSpec(
        key="youtube",
        label="YouTube video",
        thumbnail_source=ThumbnailSource.FETCH_URL,
        thumbnail_url_fn=youtube_thumbnail_url,
        ocr_capable=True,
        badge_icon="▶️",
        badge_text="YOUTUBE",
    ),
    "pdf": ObjectTypeSpec(
        key="pdf",
        label="PDF document",
        thumbnail_source=ThumbnailSource.CAPTURE,
        ocr_capable=True,
        capture_fn=_pdf.capture_thumbnail,
        text_extract_fn=_pdf.extract_text_for_row,
        badge_icon="\U0001F4C4",
        badge_text="PDF",
    ),
    "document": ObjectTypeSpec(
        key="document",
        label="Written post",
        thumbnail_source=ThumbnailSource.NONE,
        ocr_capable=False,
        badge_icon="\U0001F4DD",
        badge_text="POST",
    ),
    # Not reachable from the UI or any insert path yet — registered ahead of
    # time as a concrete example of the CAPTURE strategy (issue #15 calls
    # these out by name: "Stream -> an image grab", "URL -> a screen
    # capture"). Whichever future issue implements one fills in capture_fn
    # (and, if it has properties beyond the generic columns, metadata_fields)
    # and nothing outside this file changes.
    "stream": ObjectTypeSpec(
        key="stream",
        label="Live stream",
        thumbnail_source=ThumbnailSource.CAPTURE,
        ocr_capable=True,
        capture_fn=None,  # TODO(future issue): grab a frame of the stream's OSD/wait-card
        badge_icon="\U0001F4E1",
        badge_text="STREAM",
    ),
    "url": ObjectTypeSpec(
        key="url",
        label="Web page",
        thumbnail_source=ThumbnailSource.CAPTURE,
        ocr_capable=True,
        capture_fn=None,  # TODO(future issue): screenshot the page
        badge_icon="\U0001F517",
        badge_text="WEB",
    ),
}

# Anything not registered above (or media_type left unset) — never breaks
# thumbnail/OCR dispatch, it just means "no thumbnail, no OCR" until a real
# spec is registered for it.
DEFAULT_SPEC = ObjectTypeSpec(
    key="unknown",
    label="Unknown",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    badge_icon="❓",
    badge_text="FILE",
)


def get_object_type(media_type):
    return OBJECT_TYPES.get(media_type, DEFAULT_SPEC)
