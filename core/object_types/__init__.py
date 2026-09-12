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
  - what descriptive metadata the uploaded file itself carries (an audio
    file's ID3 tags — #255, see embedded_metadata_fn) and should seed the
    row's content fields with at upload time

core/thumbnails.py and core/ocr.py both dispatch purely off the spec
returned by get_object_type() — neither one should ever grow a literal
`if media_type == "some_new_type"` branch. Adding a new object type (PDF:
issue #13, STL: issue #14, PSD: issue #27, audio: issue #28, SVG/EPS: issue
#30, and whatever comes after) means adding one ObjectTypeSpec below plus,
if it needs one, a thumbnail_url_fn or capture_fn. Nothing else in the
codebase should need to change.
"""

import importlib
import pkgutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


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
    extensions: frozenset = frozenset()  # frozenset[str] — file extensions this type can handle (e.g. {".pdf"}), empty for types with no file
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
    # Issue #135: (row: dict) -> dict[str, str] of type-specific properties
    # suitable for display on the object detail page. Returns an ordered dict
    # of label → display-value pairs (e.g. {"Dimensions": "1920 × 1080"} for
    # images, {"Duration": "3:42"} for video). Return {} on any failure
    # (corrupt/missing file, extraction error), same best-effort discipline
    # as text_extract_fn and capture_fn — never raise, print a warning and
    # return the empty dict. Leave None for types that have no computed
    # properties to display. web/app.py calls this defensively and routes the
    # result to the template for generic iteration (no per-type template
    # branches needed).
    properties_fn: object = None
    # Issue #255: (path: pathlib.Path) -> dict of metadata the uploaded file
    # itself carries — an audio file's ID3/Vorbis/RIFF tags today — read
    # once at upload time to seed the row's content-side fields. Return
    # shape: {"content_description": str, "type_metadata": {key: value}},
    # either key omitted when the file has nothing usable for it, {} when it
    # has nothing at all. Consumed only by core/embedded_metadata.py, which
    # owns the never-overwrite fill rule and dispatches off this hook the
    # way core/ocr.py dispatches off text_extract_fn — a new type with
    # embedded metadata registers a function here, nothing else grows a
    # media_type branch. Same best-effort contract as properties_fn: never
    # raise, print a warning and return {} on any failure. The keys a type
    # writes into type_metadata belong in its metadata_fields.
    embedded_metadata_fn: object = None
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
    # Issue #239: should this type's representative image (the same one OCR
    # runs against — the uploaded file itself, or the generated thumbnail /
    # video frame / rendered raster) be sent to the local vision model for
    # an auto-caption suggestion (core/captions.py)? Scoped by whether the
    # type has a *real rendered-image preview*, NOT by ocr_capable — the two
    # differ on purpose: video is captionable but not OCR'd, and STL is the
    # explicit hard exclusion (plain-background wireframe renders produced
    # degenerate repeated-punctuation garbage in testing, not just a weak
    # caption). Defaults False so a new type opts in deliberately.
    caption_capable: bool = False


OBJECT_TYPES = {}


def register(spec):
    """Register an ObjectTypeSpec in the global OBJECT_TYPES registry.
    Called by each type module at the end of its definition."""
    OBJECT_TYPES[spec.key] = spec
    return spec


# Auto-discover and import all type modules in this package.
for _, name, _ in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{name}")


def detect_media_type(filename):
    """Given a filename, return the media_type key if its extension
    matches a registered type, or None if unsupported. Used to classify
    uploads before calling storage.save_file()."""
    ext = Path(filename).suffix.lower()
    for spec in OBJECT_TYPES.values():
        if ext in spec.extensions:
            return spec.key
    return None


def classify_url(url):
    """Classify an external URL into a media_type key. Checks YouTube first
    (to avoid misclassifying youtu.be/youtube links as generic URLs), then
    defaults to 'url' as a fallback. Returns a media_type key suitable for
    passing to db.insert_content()."""
    from . import youtube
    if youtube.matches(url):
        return "youtube"
    return "url"


# Every concrete type is registered by its own module in this package (the
# pkgutil loop above imports all of them — `python -c "from core import
# object_types; print(sorted(object_types.OBJECT_TYPES))"` lists the current
# set). This section only defines the DEFAULT_SPEC fallback.


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


# Re-export extract_youtube_id for backward compatibility with existing callers
# (web/app.py, scripts/* that reference object_types.extract_youtube_id).
# The function has moved to core/object_types/youtube.py as part of the
# object-type plugin architecture refactor.
from . import youtube
extract_youtube_id = youtube.extract_youtube_id
