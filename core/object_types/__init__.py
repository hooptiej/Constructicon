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

from .. import eps as _eps
from .. import pdf as _pdf
from .. import psd as _psd
from .. import stl as _stl
from .. import svg as _svg


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


# Registered after auto-discovery to ensure all modules have been imported.
# youtube is registered separately via core/object_types/youtube.py.
# This is just document, pdf, stl, psd, svg, eps, stream, url for now;
# image is registered separately via core/object_types/image.py.

_pdf_spec = register(ObjectTypeSpec(
    key="pdf",
    label="PDF document",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    capture_fn=_pdf.capture_thumbnail,
    text_extract_fn=_pdf.extract_text_for_row,
    badge_icon="\U0001F4C4",
    badge_text="PDF",
))

_stl_spec = register(ObjectTypeSpec(
    key="stl",
    label="3D printing file",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,  # binary mesh format, no meaningful text to extract
    capture_fn=_stl.capture_thumbnail,
    badge_icon="\U0001F9CA",  # ice cube — closest built-in glyph to a 3D-printed block
    badge_text="STL",
))

_psd_spec = register(ObjectTypeSpec(
    key="psd",
    label="Photoshop document",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,  # OCR runs against the composited preview — see core/psd.py
    capture_fn=_psd.capture_thumbnail,
    badge_icon="\U0001F3A8",  # artist palette
    badge_text="PSD",
))

_svg_spec = register(ObjectTypeSpec(
    key="svg",
    label="Vector graphic (SVG)",
    # A browser can display an SVG directly, but the gallery/detail
    # thumbnail pipeline still rasterizes it server-side (core/svg.py)
    # for tile consistency with every other type and so OCR has a
    # raster fallback — see that module's docstring for why cairosvg
    # (not "pure Python" as first hoped — needs system libcairo2, a
    # small apt dependency) was picked.
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    capture_fn=_svg.capture_thumbnail,
    text_extract_fn=_svg.extract_text_for_row,  # <text> elements read directly, no OCR needed when present
    badge_icon="\U0001F4D0",  # triangular ruler
    badge_text="SVG",
))

_eps_spec = register(ObjectTypeSpec(
    key="eps",
    label="Vector graphic (EPS)",
    # Ghostscript-rendered raster (core/eps.py) — a real system binary,
    # investigated and found to be a small, standard apt dependency
    # rather than the kind of GPU/display-dependent tooling STL (#14)
    # had to route around.
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,  # OCR runs against the rendered raster; no text layer to extract directly (see core/eps.py)
    capture_fn=_eps.capture_thumbnail,
    badge_icon="\U0001F5A8️",  # printer — PostScript's original target device
    badge_text="EPS",
))

_document_spec = register(ObjectTypeSpec(
    key="document",
    label="Written post",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    badge_icon="\U0001F4DD",
    badge_text="POST",
))

_audio_spec = register(ObjectTypeSpec(
    key="audio",
    label="Audio file",
    # Unlike PDF/STL/PSD there's no visual frame to grab — a waveform
    # render is a plausible future nice-to-have but not required for a
    # first pass (issue #28), so this is NONE rather than CAPTURE. The
    # object detail page renders a <audio controls> mini player instead
    # of a thumbnail image (see web/app.py's is_audio_file and
    # object_detail.html) and gallery tiles fall back to the generic
    # file icon with this type's badge overlaid, same as any other
    # NONE-thumbnail type.
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,  # no visual/text layer to run OCR against
    badge_icon="\U0001F3B5",  # musical note
    badge_text="AUDIO",
))

# Not reachable from the UI or any insert path yet — registered ahead of
# time as a concrete example of the CAPTURE strategy (issue #15 calls
# these out by name: "Stream -> an image grab", "URL -> a screen
# capture"). Whichever future issue implements one fills in capture_fn
# (and, if it has properties beyond the generic columns, metadata_fields)
# and nothing outside this file changes.
_stream_spec = register(ObjectTypeSpec(
    key="stream",
    label="Live stream",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    capture_fn=None,  # TODO(future issue): grab a frame of the stream's OSD/wait-card
    badge_icon="\U0001F4E1",
    badge_text="STREAM",
))

_url_spec = register(ObjectTypeSpec(
    key="url",
    label="Web page",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    capture_fn=None,  # TODO(future issue): screenshot the page
    badge_icon="\U0001F517",
    badge_text="WEB",
))


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
