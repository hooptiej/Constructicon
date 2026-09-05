"""Source code file support (issue #132): raw file text extraction for
searchable indexing.

Source code files (PHP, Python, JavaScript, shell scripts, JSON, YAML, HTML,
CSS, SQL) are stored as-is with their raw UTF-8 text content extracted and
indexed for search. No visual thumbnail concept — code files are NONE-sourced,
same as written posts or archives.

Text extraction reads the file as UTF-8 with best-effort error handling
(corrupt/legacy encodings are replaced rather than erroring), returning the
full raw source code as searchable text.

Best-effort, same as every other type's text_extract_fn in this codebase: a
missing file or unreadable encoding returns "" rather than raising, so a bad
upload never breaks the upload response or the OCR background task.
"""

from pathlib import Path

from .. import storage


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def extract_text(path):
    """Raw UTF-8 text content of the code file at `path`, or "" on any
    failure (missing file, encoding issues)."""
    if not path:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        print(f"Code text extraction failed for {path}: {e!r}")
        return ""


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='code' — see
    core/ocr.py."""
    path = _stored_path(row)
    return extract_text(path) if path else ""


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="code",
    label="Source code",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset({".php", ".py", ".js", ".sh", ".json", ".yaml", ".yml", ".html", ".css", ".sql"}),
    text_extract_fn=extract_text_for_row,
    badge_icon="💻",
    badge_text="CODE",
))
