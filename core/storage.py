"""File storage for the image repo: random unguessable slugs, files on disk.

Thumbnails: gallery/card views should never ship the full-resolution
original just to shrink it in CSS — that's what made loading slow. Every
image gets a downscaled thumbnail generated at upload time and served
separately; the original stays untouched for the real hotlink use case
(embedding in Hudu/Slack) and the detail page's full preview.
"""

import os
import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from . import parse_limits, paths  # parse_limits: importing it sets Pillow's pixel cap (#644)

# #578: the storage directory is resolved when used (core/paths.py), not frozen at import.

# #433: make file size limit configurable via environment variable
# so large PDFs/videos can be allowed without a code change
def _get_max_bytes():
    try:
        max_mb = int(os.getenv("CONSTRUCTICON_MAX_UPLOAD_MB", "25"))
        return max_mb * 1024 * 1024
    except ValueError:
        # Fall back to 25 MB on garbage env value
        return 25 * 1024 * 1024

MAX_BYTES = _get_max_bytes()
MAX_MB = MAX_BYTES // (1024 * 1024)
# #433: cap on text pulled out of any one file (PDF text layer, text/code/data
# files). Bounds the DB row, the object page's inline text and search; ~300+
# pages of dense text. Anything past it is not searchable.
MAX_EXTRACTED_TEXT_CHARS = 1_000_000
THUMB_MAX_DIM = 400
THUMB_BG = (20, 23, 15)  # matches the app's dark page background, for flattened transparency
EXIF_ORIENTATION_TAG = 0x0112


def make_slug():
    return secrets.token_urlsafe(9)


def exif_upright(img):
    """#288: apply the file's EXIF Orientation tag to the pixels before any
    resize/crop derives a new image from them. A phone/camera JPEG is usually
    stored sensor-side-up with a "rotate 90° to display" tag that browsers
    and OS viewers honor silently -- Pillow doesn't, so .thumbnail()/.crop()
    on the raw layout produce a sideways result (and the derived JPEG drops
    the tag that would have let a browser fix it on display). Returns the
    image unchanged for the untagged majority (screenshots, renders, fetched
    thumbnails): guarded rather than calling exif_transpose unconditionally
    because that always returns a fully decoded copy, which would throw away
    the DCT-scaled draft decode .thumbnail() otherwise gets for large JPEGs."""
    try:
        if img.getexif().get(EXIF_ORIENTATION_TAG, 1) == 1:
            return img
    except Exception:
        return img
    return ImageOps.exif_transpose(img)


def thumb_path_for(slug):
    return paths.storage_dir() / f"{slug}_thumb.jpg"


class ThumbnailError(Exception):
    """#852: a thumbnail could not be made from bytes that were supposed to be an image. The message says
    what was attempted and why it failed (the original exception rides in ``__cause__``). Never swallowed:
    core/thumbnails.py's ensure_thumbnail logs it and records it on the item (``type_metadata.thumb_error``)
    so the object page can say so instead of a thumbnail just being missing."""


# Pillow modes that carry more than 8 bits per pixel of greyscale (#852). The reduce step behind
# Image.thumbnail() cannot handle the 16-bit ones (ValueError "image has wrong mode"), so they are
# brought down to 8-bit "L" first.
_HIGH_DEPTH_INT_MODES = ("I;16", "I;16L", "I;16B", "I;16N", "I")
_HIGH_DEPTH_MODES = _HIGH_DEPTH_INT_MODES + ("F",)


def to_8bit_for_thumbnail(img):
    """#852: a 16-bit (``I;16*``), 32-bit integer (``I``) or float (``F``) greyscale image as an 8-bit ``L``
    image, any other mode returned unchanged. The tone mapping is a straight stretch of the image's own
    darkest..lightest value onto 0..255, so a dim scientific / astronomy / scanner frame that only uses a
    slice of the 16-bit range still shows its detail (a plain ``v / 256`` would leave it near-black). A
    flat image (one value everywhere) falls back to the plain ``v / 256`` (float: ``v * 255``), clamped, so
    a black frame stays black. Pure Pillow: no numpy."""
    if img.mode not in _HIGH_DEPTH_MODES:
        return img
    if img.mode in _HIGH_DEPTH_INT_MODES and img.mode != "I":
        img = img.convert("I")  # 16-bit (either byte order) to 32-bit signed, which Pillow's point() can scale
    lo, hi = img.getextrema()
    if img.mode == "F":
        scale = 255.0 / (hi - lo) if hi > lo else 255.0
        offset = -lo * scale if hi > lo else 0.0
    else:
        scale = 255.0 / (hi - lo) if hi > lo else 1 / 256
        offset = -lo * scale if hi > lo else 0.0
    return img.point(lambda v: v * scale + offset).convert("L")  # convert() clamps to 0..255


def save_thumbnail_from_bytes(slug, image_bytes):
    """Given raw bytes that decode as an image, generate and save the
    standard downscaled JPEG thumbnail for `slug`. This is the shared
    primitive behind every object type's thumbnail: the ingest pipeline calls
    core/thumbnails.py's ensure_thumbnail which dispatches to this function
    for UPLOADED_FILE types, and fetched/captured images for non-file types
    (see core/thumbnails.py) both funnel through here so there's exactly one
    place that resizes/flattens/encodes a thumbnail.

    #852: 16-bit greyscale (and ``I`` / ``F``) images are converted to 8-bit before the resize.
    A failure raises ``ThumbnailError`` saying what was attempted and why (it used to be a bare print and
    a missing thumbnail); callers that treat "no thumbnail" as recoverable (ensure_thumbnail) catch it,
    log it and leave a marker on the item.
    """
    try:
        img = exif_upright(Image.open(BytesIO(image_bytes)))
        img = to_8bit_for_thumbnail(img)
        img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
        if img.mode in ("RGBA", "LA", "P"):
            flattened = Image.new("RGB", img.size, THUMB_BG)
            flattened.paste(img, mask=img.convert("RGBA").split()[-1])
            img = flattened
        else:
            img = img.convert("RGB")
        img.save(thumb_path_for(slug), "JPEG", quality=82)
    except Exception as e:
        # Not fatal to the item (thumb_path_or_original() falls back to the full image), but never silent
        # (#852): say what was attempted and why it failed. A parser limit (an image over the pixel cap) keeps
        # its own classification through __cause__ (core/parse_limits.py walks it).
        raise ThumbnailError(f"couldn't make a thumbnail from this image ({type(e).__name__}: {e})") from e


def save_stream(filename, fileobj, chunk_size=1024*1024):
    """Stream a file-like object to disk in chunks, checking size limits as we go.
    Returns (slug, stored_filename, bytes_written). If the file exceeds MAX_BYTES,
    closes and deletes the partial destination file and raises ValueError.
    Supports seeking to the start if the fileobj has a seek method.
    Thumbnail generation (if needed) is handled by the ingest pipeline via
    core/thumbnails.py's ensure_thumbnail(), not here."""
    if hasattr(fileobj, "seek"):
        fileobj.seek(0)

    # #485: keep a multi-suffix extension (".tar.gz") whole so the stored file
    # still says what it is. Lazy import: object_types imports this module.
    from . import object_types
    ext = object_types.file_extension(filename)
    store = paths.storage_dir()
    store.mkdir(parents=True, exist_ok=True)
    slug = make_slug()
    dest = store / f"{slug}{ext}"

    bytes_written = 0
    try:
        with open(dest, "wb") as f:
            while chunk := fileobj.read(chunk_size):
                bytes_written += len(chunk)
                if bytes_written > MAX_BYTES:
                    raise ValueError(f"File exceeds {MAX_MB}MB limit")
                f.write(chunk)
    except BaseException:
        # Never leave a partial file behind (over-limit, disk full, client gone).
        dest.unlink(missing_ok=True)
        raise

    return slug, dest.name, bytes_written


def save_file(filename, content):
    """Save a file to the storage directory. Callers are responsible for validating
    the file extension (via object_types.detect_media_type()) before calling this —
    this function no longer validates extensions itself (that responsibility moved to
    the call site in the plugin architecture redesign, issue #82). Thumbnail generation
    (if needed) is handled by the ingest pipeline via core/thumbnails.py's
    ensure_thumbnail(), not here."""
    if len(content) > MAX_BYTES:
        raise ValueError(f"File exceeds {MAX_MB}MB limit")

    slug, stored_filename, _ = save_stream(filename, BytesIO(content))
    return slug, stored_filename


def path_for(stored_filename):
    return paths.storage_dir() / stored_filename


def thumb_path_or_original(slug, stored_filename):
    """Falls back to the original file only when there is one — content-only
    rows (media_type='youtube' and friends, see core/db.py's insert_content)
    have stored_filename=None and only ever have a thumbnail, never an
    original stored locally."""
    thumb = thumb_path_for(slug)
    if thumb.exists():
        return thumb
    return paths.storage_dir() / stored_filename if stored_filename else thumb


def delete_files(slug, stored_filename):
    """Remove the original and its thumbnail (if any) from disk. Used by both
    a full delete and a redact-file-keep-metadata action."""
    original = paths.storage_dir() / stored_filename
    if original.exists():
        original.unlink()
    thumb = thumb_path_for(slug)
    if thumb.exists():
        thumb.unlink()
