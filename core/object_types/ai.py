"""Adobe Illustrator support (issue #130): first-page thumbnail render + text
extraction, with a fitz-then-ghostscript fallback strategy.

Modern Illustrator files (CS/v9+, with "PDF Compatible" checked — the
Illustrator default) are valid PDFs. We try PyMuPDF (fitz) first to render
the first page and extract text, exactly as core/object_types/pdf.py does.

For legacy pre-v9 .ai files (raw PostScript, not valid PDFs), fitz fails
and we fall back to Ghostscript rasterization (same as core/object_types/eps.py).
This gives us broad compatibility: modern Adobe files work instantly, and
older PostScript-era .ai files still get a usable raster preview rather than
nothing at all.

Text extraction tries only the PDF path (fitz) — no text extraction attempted
on the EPS fallback path, matching eps.py's own discipline (raw PostScript
has no generic text layer to pull structured text from).

The thumbnail render is best-effort, same as every other type's capture_fn
in this codebase: a corrupt/unreadable .ai, a fitz or gs failure, or a
missing file render None rather than raising, so a bad upload never breaks
the upload response. The text reader says so instead (#724 b2): a
PDF-compatible .ai that can't be read raises UnreadableFile, which
core/ocr.py reports ("corrupt or unreadable .ai: ...").
"""

from pathlib import Path

from .. import storage
from . import pdf, eps


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def render_first_page(path):
    """PNG bytes for page 1 of the .ai at `path`, or None on any failure.
    Tries fitz (PDF path) first, falls back to Ghostscript (EPS path) for
    legacy .ai files."""
    if not path:
        return None
    # Try PDF path first (modern .ai files)
    result = pdf.render_first_page(path)
    if result is not None:
        return result
    # Fall back to Ghostscript rasterization (legacy .ai files)
    return eps.render_raster(path)


def is_pdf_compatible(path):
    """True when the .ai is a PDF inside (CS/v9+ "PDF Compatible": it starts with %PDF), False for a
    legacy PostScript .ai (%!PS-Adobe), which has no text layer to read."""
    with open(path, "rb") as f:
        return f.read(1024).lstrip().startswith(b"%PDF")


def extract_text(path):
    """The .ai's embedded text layer (PDF-compatible path), or "" if there
    isn't one. "" for legacy PostScript .ai files (Ghostscript path has no
    text extraction: OCR of the rendered page is the text). #724 (b2): a
    PDF-compatible .ai that PyMuPDF can't read raises, like a corrupt PDF
    ("corrupt or unreadable .ai: ..."), instead of reading as "no text"."""
    if not path:
        return ""
    from . import unreadable
    try:
        if not is_pdf_compatible(path):
            return ""
    except OSError as e:
        raise unreadable(path, e) from e
    # Only try PDF extraction path — no text extraction on EPS fallback
    return pdf.text_layer(path)


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='ai' — see core/thumbnails.py."""
    path = _stored_path(row)
    return render_first_page(path) if path else None


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='ai' — see core/ocr.py."""
    path = _stored_path(row)
    return extract_text(path) if path else ""


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='ai' — delegates entirely
    to pdf.get_properties (page count, title, author, creation date) for
    modern PDF-compatible .ai files. pdf.get_properties already returns {}
    gracefully when fitz can't open the file at all, which is exactly the
    legacy-PostScript-.ai case — no separate handling needed here."""
    return pdf.get_properties(row)


from . import _preview


def preview(ctx):
    """#449 preview_fn: rendered first page + "View original" link. None (-> the page's generic fallback) when there's no thumbnail."""
    return _preview.thumb_with_original_link(ctx) if ctx.thumb_url else None


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    preview_fn=preview,  # #449
    key="ai",
    label="Illustrator file",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    extensions=frozenset({".ai"}),
    capture_fn=capture_thumbnail,
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    badge_icon="✒️",
    badge_text="AI",
))
