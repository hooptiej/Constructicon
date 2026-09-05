"""Archive file support (issue #128): content listing extraction for ZIP
and 7z archive formats.

Archives (ZIP and 7z) are text-extractable via a listing of member filenames —
this is useful for searching/indexing what's inside an archive without
actually extracting or OCR'ing the full contents (which would be slow and
noisy for a 1GB zip full of binary files).

Both formats are handled via stdlib (zipfile) and a pure-Python 7z library
(py7zr), requiring no new system packages. No visual thumbnail concept —
archives are NONE-sourced, same as written posts.

Best-effort, same as every other type's text_extract_fn in this codebase: a
corrupt/unreadable archive returns "" rather than raising, so a bad upload
never breaks the upload response or the OCR background task.
"""

from pathlib import Path
import zipfile

try:
    import py7zr
except ImportError:
    py7zr = None

from .. import storage


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def extract_file_list(path):
    """Newline-joined list of member filenames in the archive at `path`, or
    "" on any failure (corrupt archive, wrong format, missing file, py7zr not
    installed)."""
    if not path:
        return ""

    ext = Path(path).suffix.lower()

    try:
        if ext == ".zip":
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
                return "\n".join(names) if names else ""
        elif ext == ".7z":
            if py7zr is None:
                return ""
            with py7zr.SevenZipFile(path, "r") as zf:
                names = zf.getnames()
                return "\n".join(names) if names else ""
        else:
            return ""
    except Exception as e:
        print(f"Archive listing failed for {path}: {e!r}")
        return ""


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='archive' — see
    core/ocr.py."""
    path = _stored_path(row)
    return extract_file_list(path) if path else ""


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="archive",
    label="Archive",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset({".zip", ".7z"}),
    text_extract_fn=extract_text_for_row,
    badge_icon="🗄️",
    badge_text="ZIP",
))
