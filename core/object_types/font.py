"""Font file support (issue #127): glyph-sample preview thumbnail using PIL.

TTF and OTF fonts can be rendered as a preview by drawing a pangram string
using PIL's ImageFont.truetype() loader and ImageDraw. This gives a visual
sample of the font's appearance without needing any external system tools —
PIL's bundled freetype is the only dependency, which is already present for
all other image processing in the app.

No text extraction is performed — the rendered image contains a synthetic
(always-the-same) pangram, not meaningful searchable content, so extracting
it back via OCR would be pointless.

Best-effort, same as every other type's capture_fn in this codebase: a
corrupt font file, a file not actually a valid TTF/OTF, or a missing file
all return None rather than raising, so a bad upload never breaks the
upload response or the OCR background task.
"""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import storage

# Standard pangram — demonstrates all letters + numbers
PANGRAM = "The quick brown fox jumps over the lazy dog 0123456789"

# Canvas dimensions for the glyph sample
CANVAS_WIDTH = 800
CANVAS_HEIGHT = 200
FONT_SIZE = 48

# Match the app's dark page background (from storage.py)
BG_COLOR = (20, 23, 15)
TEXT_COLOR = (255, 255, 255)


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def render_glyph_sample(path):
    """PNG bytes of a glyph sample (pangram rendered with the font at `path`),
    or None on any failure (corrupt font file, not a valid TTF/OTF, missing
    file)."""
    try:
        # Load the font
        font = ImageFont.truetype(str(path), size=FONT_SIZE)

        # Create canvas
        img = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # Draw the pangram
        draw.text((20, 20), PANGRAM, font=font, fill=TEXT_COLOR)

        # Encode as PNG bytes
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"Font glyph sample rendering failed for {path}: {e!r}")
        return None


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='font' — see
    core/thumbnails.py."""
    path = _stored_path(row)
    return render_glyph_sample(path) if path else None


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="font",
    label="Font",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,  # The rendered text is synthetic (always the same pangram), not meaningful to search
    extensions=frozenset({".ttf", ".otf"}),
    capture_fn=capture_thumbnail,
    badge_icon="🔤",
    badge_text="FONT",
))
