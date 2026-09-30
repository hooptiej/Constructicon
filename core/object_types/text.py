"""Text file support (issue #431): raw file text extraction for searchable
indexing.

Text files (Markdown and plain text) are stored as-is with their raw UTF-8
text content extracted and indexed for search. No visual thumbnail concept —
text files are NONE-sourced, same as written posts or archives or data files.

Deliberately a separate type from the existing 'document' type (which
represents authored "Written post" entries — see core/object_types/document.py)
— this type is for literal file uploads (.md, .txt), not structured authored
content.

Text extraction reads the file as UTF-8 with best-effort error handling
(corrupt/legacy encodings are replaced rather than erroring), returning the
full raw content as searchable text.

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
    """Raw UTF-8 text content of the text file at `path` (capped at
    storage.MAX_EXTRACTED_TEXT_CHARS, #433), or "" on any failure (missing
    file, encoding issues)."""
    if not path:
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(storage.MAX_EXTRACTED_TEXT_CHARS).strip()
    except Exception as e:
        print(f"Text extraction failed for {path}: {e!r}")
        return ""


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='text' — see
    core/ocr.py."""
    path = _stored_path(row)
    return extract_text(path) if path else ""


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="text",
    label="Text file",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset({".md", ".txt"}),
    text_extract_fn=extract_text_for_row,
    badge_icon="📄",
    badge_text="TEXT",
))
