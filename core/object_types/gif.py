"""Animated GIF support (issue #93): .gif file uploads with frame 0
thumbnail and OCR support.

GIFs are stored as-is (UPLOADED_FILE, like images) so that animated GIFs
remain animated on the detail page's full preview. The thumbnail is
generated from frame 0 by the standard PIL-based thumbnail pipeline
(core/storage.py), which already handles GIF's palette mode correctly.

OCR is enabled for tutorial/reaction/text-overlay GIFs (see issue #93),
which can contain readable text that's worth extracting.
"""

from PIL import Image

from .. import storage


def _stored_path(row):
    """Helper to get the full path to a stored GIF file, or None if the
    file doesn't exist or wasn't provided."""
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='gif' — returns GIF
    dimensions and frame count, or {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}
    try:
        img = Image.open(path)
        width, height = img.size
        props = {"Dimensions": f"{width} × {height}"}
        # Attempt to get frame count; not all GIFs are animated
        try:
            n_frames = img.n_frames
            if n_frames > 1:
                props["Frames"] = str(n_frames)
        except (AttributeError, Exception):
            pass
        return props
    except Exception as e:
        print(f"GIF properties extraction failed for {path}: {e!r}")
        return {}


from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="gif",
    label="Animated GIF",
    thumbnail_source=ThumbnailSource.UPLOADED_FILE,
    ocr_capable=True,
    extensions=frozenset({".gif"}),
    properties_fn=get_properties,
    badge_icon="\U0001F4CF",
    badge_text="GIF",
))
