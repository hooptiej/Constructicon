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

from . import paths

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


def save_thumbnail_from_bytes(slug, image_bytes):
    """Given raw bytes that decode as an image, generate and save the
    standard downscaled JPEG thumbnail for `slug`. This is the shared
    primitive behind every object type's thumbnail: the ingest pipeline calls
    core/thumbnails.py's ensure_thumbnail which dispatches to this function
    for UPLOADED_FILE types, and fetched/captured images for non-file types
    (see core/thumbnails.py) both funnel through here so there's exactly one
    place that resizes/flattens/encodes a thumbnail.
    """
    try:
        img = exif_upright(Image.open(BytesIO(image_bytes)))
        img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
        if img.mode in ("RGBA", "LA", "P"):
            flattened = Image.new("RGB", img.size, THUMB_BG)
            flattened.paste(img, mask=img.convert("RGBA").split()[-1])
            img = flattened
        else:
            img = img.convert("RGB")
        img.save(thumb_path_for(slug), "JPEG", quality=82)
    except Exception as e:
        # Not fatal — thumb_path_or_original() falls back to the full image —
        # but print so a systematic failure (e.g. a corrupt-image edge case) is traceable.
        print(f"thumbnail generation failed for {slug}: {e!r}")


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


AVATAR_SIZE = 256


def normalize_avatar(content):
    """Center-crop to square and resize to AVATAR_SIZE, regardless of what
    the client sent — the client-side cropper already exports a square
    AVATAR_SIZE PNG, but this makes that a server-enforced guarantee rather
    than a trusted assumption. A no-op on already-correct input."""
    img = exif_upright(Image.open(BytesIO(content)))
    img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def delete_files(slug, stored_filename):
    """Remove the original and its thumbnail (if any) from disk. Used by both
    a full delete and a redact-file-keep-metadata action."""
    original = paths.storage_dir() / stored_filename
    if original.exists():
        original.unlink()
    thumb = thumb_path_for(slug)
    if thumb.exists():
        thumb.unlink()
