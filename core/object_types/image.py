"""Image type spec and registration.

Images are the simplest object type: the uploaded file itself IS the
thumbnail (UPLOADED_FILE strategy), they're text-extractable via OCR,
and they need no dedicated capture or text-extraction logic module.
"""

from pathlib import Path

from PIL import Image

from .. import storage


def _stored_path(row):
    """Helper to get the full path to a stored image file, or None if the
    file doesn't exist or wasn't provided."""
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='image' — returns image
    dimensions as a user-friendly string, or {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}
    try:
        img = Image.open(path)
        width, height = img.size
        return {"Dimensions": f"{width} × {height}"}
    except Exception as e:
        print(f"Image properties extraction failed for {path}: {e!r}")
        return {}


from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="image",
    label="Image",
    thumbnail_source=ThumbnailSource.UPLOADED_FILE,
    ocr_capable=True,
    extensions=frozenset({".png", ".jpg", ".jpeg", ".ico", ".bmp", ".tiff", ".tif", ".webp"}),
    properties_fn=get_properties,
    badge_icon="\U0001F5BC️",
    badge_text="IMAGE",
))
