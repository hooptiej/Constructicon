"""File storage for the image repo: random unguessable slugs, files on disk.

Thumbnails: gallery/card views should never ship the full-resolution
original just to shrink it in CSS — that's what made loading slow. Every
image gets a downscaled thumbnail generated at upload time and served
separately; the original stays untouched for the real hotlink use case
(embedding in Hudu/Slack) and the detail page's full preview.
"""

import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
MAX_BYTES = 25 * 1024 * 1024
THUMB_MAX_DIM = 400
THUMB_BG = (20, 23, 15)  # matches the app's dark page background, for flattened transparency


def make_slug():
    return secrets.token_urlsafe(9)


def thumb_path_for(slug):
    return STORAGE_DIR / f"{slug}_thumb.jpg"


def make_thumbnail(slug, content, ext):
    if ext not in IMAGE_EXTENSIONS:
        return
    try:
        img = Image.open(BytesIO(content))
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


def save_file(filename, content):
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")
    if len(content) > MAX_BYTES:
        raise ValueError(f"File exceeds {MAX_BYTES // (1024*1024)}MB limit")

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    slug = make_slug()
    dest = STORAGE_DIR / f"{slug}{ext}"
    dest.write_bytes(content)
    make_thumbnail(slug, content, ext)
    return slug, dest.name


def path_for(stored_filename):
    return STORAGE_DIR / stored_filename


def thumb_path_or_original(slug, stored_filename):
    thumb = thumb_path_for(slug)
    return thumb if thumb.exists() else STORAGE_DIR / stored_filename


AVATAR_SIZE = 256


def normalize_avatar(content):
    """Center-crop to square and resize to AVATAR_SIZE, regardless of what
    the client sent — the client-side cropper already exports a square
    AVATAR_SIZE PNG, but this makes that a server-enforced guarantee rather
    than a trusted assumption. A no-op on already-correct input."""
    img = Image.open(BytesIO(content))
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
    original = STORAGE_DIR / stored_filename
    if original.exists():
        original.unlink()
    thumb = thumb_path_for(slug)
    if thumb.exists():
        thumb.unlink()
