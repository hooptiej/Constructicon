"""PSD support (issue #27): rendered composite-preview thumbnail, wired
into the object-type registry (core/object_types.py) as a CAPTURE-sourced
type's capture_fn — same shape as PDF's core/object_types/pdf.py and STL's core/object_types/stl.py.

psd-tools (pure Python, no system dependencies) reads the PSD's layer tree
and composites it down to a single flattened raster via
PSDImage.composite(). That's used here instead of extracting Photoshop's
own embedded low-res thumbnail/preview resource: not every PSD carries one
(one saved with "Maximize Compatibility" off, or one built programmatically
without ever touching Photoshop, has no such resource — psd-tools exposes
PSDImage.has_preview()/has_thumbnail() precisely because it's optional).
composite() instead works directly off the layer data, which every readable
PSD has, and produces a full-resolution flatten rather than a low-res
preview — strictly at least as good as extracting the embedded preview, and
it doesn't depend on a resource that may not be there. Confirmed installing
cleanly on this Dockerfile's python:3.14-slim base (manylinux wheel, no
extra system packages beyond what's already installed for Pillow/numpy).

No dedicated text_extract_fn (unlike PDF's core/object_types/pdf.py): a PSD has no
general embedded text layer the way a PDF's content stream does — any text
in a PSD is either baked into raster layers or lives in Photoshop's own
text-engine layer data, which isn't a reliable plain-text source to pull
from generically. So this type relies on ocr_capable=True routing OCR
against the composited preview the same way any other CAPTURE-sourced
type's thumbnail gets OCR'd (see core/ocr.py's _ocr_source_path) — the
right approach for reading rendered text within a design mockup, comp, or
screenshot saved as .psd.

Best-effort, same as every other type's capture_fn in this codebase: a
corrupt/unreadable PSD returns None rather than raising, so a bad upload
never breaks the upload response or the OCR background task that calls
this indirectly via thumbnails.ensure_thumbnail.
"""

from io import BytesIO

from psd_tools import PSDImage

from .. import storage


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def render_composite(path):
    """PNG bytes of the PSD's flattened composite (all visible layers
    merged), or None on any failure (corrupt/unreadable PSD, missing file,
    or a PSD whose layer data can't be composited)."""
    try:
        psd = PSDImage.open(path)
        image = psd.composite()
        if image is None:
            return None
        # PNG has no CMYK encoder in Pillow — a CMYK-mode PSD (common for
        # print-oriented Photoshop work) needs converting first. RGBA/LA/P
        # composites are left alone; storage.save_thumbnail_from_bytes
        # already flattens those onto the app's standard thumbnail
        # background, same as any other type's capture_fn output.
        if image.mode == "CMYK":
            image = image.convert("RGB")
        buf = BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"PSD composite failed for {path}: {e!r}")
        return None


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='psd' — see
    core/thumbnails.py. media_type='psd' rows are still UPLOADED_FILE-shaped
    (the row's own stored_filename really is the PSD); CAPTURE is used here
    only because the *representative image* has to be rendered/composited
    rather than being the file itself, the same reasoning as PDF/STL."""
    path = _stored_path(row)
    return render_composite(path) if path else None


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='psd' — returns canvas
    size, or {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}
    try:
        psd = PSDImage.open(path)
        width, height = psd.size
        return {"Canvas size": f"{width} × {height}"}
    except Exception as e:
        print(f"PSD properties extraction failed for {path}: {e!r}")
        return {}


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="psd",
    label="Photoshop document",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,  # OCR runs against the composited preview — see this module
    caption_capable=True,  # #239: same composited preview
    extensions=frozenset({".psd"}),
    capture_fn=capture_thumbnail,
    properties_fn=get_properties,
    badge_icon="\U0001F3A8",  # artist palette
    badge_text="PSD",
))
