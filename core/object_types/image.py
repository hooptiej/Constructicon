"""Image type spec and registration.

Images are the simplest object type: the uploaded file itself IS the
thumbnail (UPLOADED_FILE strategy), they're text-extractable via OCR,
and they need no dedicated capture or text-extraction logic module.

Capture date (#265): a camera photo's EXIF DateTimeOriginal is the one
piece of embedded metadata worth promoting to content_date — read once at
upload time by get_embedded_metadata below and written by
core/embedded_metadata.py under its fill-only-missing rule, and over
existing rows by scripts/backfill_content_dates.py. Most of what's in this
gallery (screenshots, renders, web-resized copies) has no EXIF at all;
that's the expected {} result, not a failure. Checked against production
while scoping this: 66 of 361 undated image rows carried a real capture
timestamp.
"""

from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ExifTags import TAGS

from .. import storage, timeline

# EXIF tag ids. The capture-time tags live in the Exif SubIFD (0x8769 in
# the main IFD, same place _exif_properties reads exposure/aperture/ISO
# from), not the top-level dict Pillow's getexif() iterates. Pillow does
# expose these as PIL.ExifTags.Base/IFD enums; raw ids are used here to
# match how the rest of this module already addresses SubIFD tags.
_EXIF_IFD = 0x8769
# DateTimeOriginal is the shutter moment; DateTimeDigitized is when the
# image became a file — identical for a camera, and the only one of the
# two a scanner writes, so it's the fallback. Each has a matching
# OffsetTime* tag (EXIF 2.31, 2016 — written by phones since iOS 13 /
# recent Android) carrying the camera's UTC offset as "+HH:MM".
_DATE_TAGS = (
    (0x9003, 0x9011),  # DateTimeOriginal, OffsetTimeOriginal
    (0x9004, 0x9012),  # DateTimeDigitized, OffsetTimeDigitized
)
_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
# Deliberately NOT in the list: the main IFD's DateTime (0x0132). Despite
# the name it's the file's last-modified stamp per the EXIF spec (it
# diverges from DateTimeOriginal the moment an editor re-saves the file),
# so it's the same kind of signal as source_modified_at — which already
# has its own rung in core/timeline.py's resolve_item_date chain, one
# below content_date. Promoting it would put a modification time in the
# slot reserved for when the content actually happened.


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


def read_capture_datetime(img):
    """The EXIF capture moment of an open PIL image as a datetime —
    timezone-aware when the file carries an OffsetTime* tag for it, naive
    (the camera's wall clock, zone unknown) otherwise — or None when the
    image has no usable DateTimeOriginal/DateTimeDigitized. Raises on a
    Pillow failure; get_embedded_metadata is the best-effort wrapper.

    A value that isn't in EXIF's "YYYY:MM:DD HH:MM:SS" shape — the
    "0000:00:00 00:00:00" an unset camera clock writes, or free text some
    editors leave there — is treated as absent rather than guessed at."""
    exif = img.getexif()
    if not exif:
        return None
    try:
        ifd = exif.get_ifd(_EXIF_IFD)
    except Exception:
        return None
    for date_tag, offset_tag in _DATE_TAGS:
        raw = ifd.get(date_tag)
        if not raw:
            continue
        raw = str(raw).strip()
        offset = ifd.get(offset_tag)
        if offset:
            # "%z" accepts the "+HH:MM" form OffsetTime* uses. A malformed
            # offset falls through to the naive parse below rather than
            # costing the date entirely.
            try:
                return datetime.strptime(f"{raw} {str(offset).strip()}", f"{_EXIF_DATETIME_FORMAT} %z")
            except ValueError:
                pass
        try:
            return datetime.strptime(raw, _EXIF_DATETIME_FORMAT)
        except ValueError:
            continue
    return None


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn for media_type='image' (#265) —
    {"content_date": <UTC unix seconds>} from the file's EXIF capture
    timestamp (see read_capture_datetime), or {} for an image with none
    or on any failure. A naive EXIF time is interpreted as Mountain Time
    per core/timeline.py's convention; an explicit OffsetTime* wins over
    it. Only content_date: an image's EXIF has no title-shaped field
    worth seeding content_description from the way an audio tag does."""
    try:
        with Image.open(path) as img:
            captured = read_capture_datetime(img)
        if captured is None:
            return {}
        return {"content_date": timeline.source_datetime_to_epoch(captured)}
    except Exception as e:
        print(f"Image capture-date extraction failed for {path}: {e!r}")
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
    caption_capable=True,  # #239: the uploaded image itself goes to the vision model
    extensions=frozenset({".png", ".jpg", ".jpeg", ".ico", ".bmp", ".tiff", ".tif", ".webp"}),
    properties_fn=get_properties,
    # #265: EXIF DateTimeOriginal -> content_date at upload time (and via
    # scripts/backfill_content_dates.py for rows that predate this). See
    # get_embedded_metadata; writes nothing into type_metadata, so no
    # metadata_fields entry goes with it.
    embedded_metadata_fn=get_embedded_metadata,
    badge_icon="\U0001F5BC️",
    badge_text="IMAGE",
))
