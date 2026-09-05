"""Image type spec and registration.

Images are the simplest object type: the uploaded file itself IS the
thumbnail (UPLOADED_FILE strategy), they're text-extractable via OCR,
and they need no dedicated capture or text-extraction logic module.
"""

from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ExifTags import TAGS

from .. import storage


def _stored_path(row):
    """Helper to get the full path to a stored image file, or None if the
    file doesn't exist or wasn't provided."""
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def _exif_properties(img):
    """Best-effort EXIF read via Pillow (no new dependency). Most of what's
    in a personal gallery is screenshots/renders with no EXIF at all, so
    this routinely returns {} — that's expected, not a failure, and the
    template already hides properties that never show up. Only pulls the
    handful of fields a person would actually recognize (camera, exposure
    triangle, when it was taken) rather than dumping every raw EXIF tag."""
    try:
        exif = img.getexif()
        if not exif:
            return {}
        tags = {TAGS.get(tag_id, tag_id): value for tag_id, value in exif.items()}
        props = {}
        camera = f"{tags.get('Make', '')} {tags.get('Model', '')}".strip()
        if camera:
            props["Camera"] = camera
        if tags.get("DateTime"):
            # EXIF's own format is "YYYY:MM:DD HH:MM:SS" — reformat to
            # something readable rather than showing the raw colons-as-date
            # separators form. Falls back to the raw string if it's ever
            # not in the expected shape.
            try:
                props["Taken"] = datetime.strptime(str(tags["DateTime"]), "%Y:%m:%d %H:%M:%S").strftime("%b %-d, %Y, %-I:%M %p")
            except ValueError:
                props["Taken"] = str(tags["DateTime"])
        # Exposure time/f-number/ISO live in the "Exif SubIFD" (tag
        # 0x8769 in the main IFD), not the top-level tags dict above.
        try:
            exif_ifd = exif.get_ifd(0x8769)
        except Exception:
            exif_ifd = {}
        exposure = exif_ifd.get(33434)  # ExposureTime
        if exposure:
            try:
                exposure = float(exposure)
                props["Exposure"] = f"1/{round(1 / exposure)}s" if exposure < 1 else f"{exposure:g}s"
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        fnumber = exif_ifd.get(33437)  # FNumber
        if fnumber:
            try:
                props["Aperture"] = f"f/{float(fnumber):.1f}"
            except (TypeError, ValueError):
                pass
        iso = exif_ifd.get(34855)  # ISOSpeedRatings
        if iso:
            props["ISO"] = str(iso)
        return props
    except Exception:
        return {}


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='image' — dimensions
    plus whatever EXIF camera metadata is actually present, or {} on any
    failure."""
    path = _stored_path(row)
    if not path:
        return {}
    try:
        img = Image.open(path)
        width, height = img.size
        props = {"Dimensions": f"{width} × {height}"}
        props.update(_exif_properties(img))
        return props
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
