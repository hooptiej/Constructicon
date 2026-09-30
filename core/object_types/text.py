"""Plain text file support (issue #431; #450 moved Markdown out to
core/object_types/markdown.py): raw file text extraction for searchable
indexing, shown verbatim.

Plain text files are stored as-is with their raw UTF-8
text content extracted and indexed for search. No visual thumbnail concept —
text files are NONE-sourced, same as written posts or archives or data files.

Deliberately a separate type from the existing 'document' type (which
represents authored "Written post" entries — see core/object_types/document.py)
— this type is for literal .txt file uploads, not structured authored
content.

Text extraction reads the file as UTF-8 with best-effort error handling
(corrupt/legacy encodings are replaced rather than erroring), returning the
full raw content as searchable text.

Best-effort, same as every other type's text_extract_fn in this codebase: a
missing file or unreadable encoding returns "" rather than raising, so a bad
upload never breaks the upload response or the OCR background task.
"""

from markupsafe import Markup, escape

from .. import storage
from . import _preview, _textstats


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


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='text': Lines, Words,
    Encoding, Line endings. Returns {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}

    text = extract_text(path)
    props = {}

    # Text stats
    stats = _textstats.text_file_stats(path, storage.MAX_EXTRACTED_TEXT_CHARS)
    if stats:
        if "lines" in stats:
            line_count = stats["lines"]
            if stats.get("truncated"):
                props["Lines"] = f"{line_count:,} (first {storage.MAX_EXTRACTED_TEXT_CHARS // (1024*1024)}+ MB)"
            else:
                props["Lines"] = f"{line_count:,}"
        if "encoding" in stats:
            props["Encoding"] = stats["encoding"]
        if "line_endings" in stats and stats["line_endings"] != "none":
            props["Line endings"] = stats["line_endings"]

    # Word count
    if text:
        words = len(text.split())
        props["Words"] = f"{words:,}"

    return props


def preview(ctx):
    """#449 preview_fn: text content preview (first 50,000 chars max).
    The static export keeps its download link."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    text = ctx.item.get("extracted_text") or ""
    if not text:
        return None

    shown = text[:50_000]
    truncated = len(text) > 50_000

    html = f'<pre class="ocr-text mono" style="max-height:70vh;overflow:auto;white-space:pre-wrap">{escape(shown)}</pre>'
    if truncated:
        html += '<p class="muted">…truncated, showing first 50,000 characters</p>'

    return Markup(html)


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="text",
    label="Plain text file",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset({".txt"}),
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    preview_fn=preview,
    badge_icon="📄",
    badge_text="TEXT",
))
