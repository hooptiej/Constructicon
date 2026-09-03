"""SVG support (issue #30): rendered raster thumbnail + direct <text>
extraction, wired into the object-type registry (core/object_types.py) as a
CAPTURE-sourced type's capture_fn/text_extract_fn — same shape as PDF's
core/pdf.py.

A browser can display an SVG directly (it's already an image format to
them), but the gallery/detail thumbnail pipeline wants a raster image both
for tile consistency with every other type's thumbnail and so OCR has
something raster to fall back to — so this still rasterizes server-side
rather than special-casing SVG to skip the thumbnail step.

cairosvg does the rasterization. It is NOT pure-Python despite being a pure
Python *package* — investigated for issue #30 and confirmed it dynamically
loads a real system libcairo.so.2 at runtime via cairocffi/ctypes, and fails
outright without it (no bundled/static cairo in the wheel). That system
dependency is `libcairo2` via apt (see Dockerfile) — confirmed installing
cleanly on this Dockerfile's python:3.14-slim base and adding only ~1.4MB to
the image, the same "small, well-packaged system dependency" category as
tesseract-ocr (already installed for OCR), not the kind of heavyweight/fragile
dependency STL's core/stl.py had to route around for issue #14.

Text extraction: an SVG's <text> (and nested <tspan>) elements are ordinary
XML content, not a rendered glyph layer — stdlib xml.etree.ElementTree reads
them directly with no new dependency, the same "skip OCR when the format
already has real text" reasoning as PDF's text-layer extraction. Falling
back to OCR against the rendered raster (core/ocr.py) still catches text
that's actually vector paths/outlines rather than <text> elements, same as
any other type's OCR fallback.

Both entry points below are best-effort, same as every other type's
capture_fn/text_extract_fn in this codebase: a corrupt/malformed SVG or
missing file returns None/"" rather than raising.
"""

import xml.etree.ElementTree as ET

import cairosvg

from .. import storage

# SVGs are usually small/simple compared to a scanned page or a dense mesh,
# so a single fixed render size (rather than PDF-style DPI zoom) is enough
# to look sharp once storage.save_thumbnail_from_bytes downscales it to the
# standard THUMB_MAX_DIM thumbnail. cairosvg happily upscales a
# viewBox-only SVG with no intrinsic width/height to this size.
RENDER_SIZE_PX = 500

# The SVG namespace every <text>/<tspan> element found via ElementTree.iter()
# is qualified with (a bare SVG document's default xmlns), so tag-matching
# has to account for the "{namespace}localname" form ElementTree produces —
# comparing plain "text" would never match a real SVG file.
_SVG_NS = "{http://www.w3.org/2000/svg}"


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def render_raster(path):
    """PNG bytes of `path` rasterized at a fixed size, or None on any
    failure (malformed SVG, missing file, an SVG referencing something
    cairosvg can't resolve)."""
    try:
        return cairosvg.svg2png(
            url=str(path),
            output_width=RENDER_SIZE_PX,
            output_height=RENDER_SIZE_PX,
        )
    except Exception as e:
        print(f"SVG render failed for {path}: {e!r}")
        return None


def extract_text(path):
    """Concatenated content of every top-level <text> element in the SVG at
    `path`, in document order, or "" if there are none, the file is
    malformed, or the file is missing. Only <text> is matched (not <tspan>
    directly) — itertext() already recurses into a <text> element's nested
    <tspan> children, so matching both would double-count their content.
    "" (not None) on the no-text case specifically, matching PDF's
    extract_text — core/ocr.py treats falsy either way as "fall back to OCR
    against the rendered raster"."""
    try:
        tree = ET.parse(path)
    except Exception as e:
        print(f"SVG text extraction failed for {path}: {e!r}")
        return ""
    texts = []
    for elem in tree.getroot().iter(f"{_SVG_NS}text"):
        text = "".join(elem.itertext()).strip()
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='svg' — see
    core/thumbnails.py. media_type='svg' rows are still UPLOADED_FILE-shaped
    (the row's own stored_filename really is the SVG); CAPTURE is used here
    only because the representative image has to be rasterized rather than
    being the file itself, the same reasoning as PDF/STL/PSD."""
    path = _stored_path(row)
    return render_raster(path) if path else None


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='svg' — see
    core/ocr.py, which tries this before ever running OCR."""
    path = _stored_path(row)
    return extract_text(path) if path else ""


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
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
    extensions=frozenset({".svg"}),
    capture_fn=capture_thumbnail,
    text_extract_fn=extract_text_for_row,  # <text> elements read directly, no OCR needed when present
    badge_icon="\U0001F4D0",  # triangular ruler
    badge_text="SVG",
))
