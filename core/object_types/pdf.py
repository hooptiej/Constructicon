"""PDF support (issue #13): first-page thumbnail render + text-layer
extraction, wired into the object-type registry as a CAPTURE-sourced type's
capture_fn/text_extract_fn.

PyMuPDF (import name `fitz`) does both jobs from one dependency with no
extra system packages (no poppler/ghostscript install needed in the
Dockerfile, unlike pdf2image) — rendering is just `page.get_pixmap()`, and
the same open document gives us `page.get_text()` for the embedded text
layer most PDFs already have.

Both entry points below are best-effort, same as every other type's
capture_fn/thumbnail_url_fn in this codebase: a corrupt/encrypted/zero-page
PDF returns None/""  rather than raising, so a bad upload never breaks the
upload response or the OCR background task that calls these.
"""

from pathlib import Path

import fitz  # PyMuPDF

from .. import storage

# 2x zoom on PyMuPDF's default 72 DPI render gives a ~144 DPI page image —
# plenty sharp once storage.save_thumbnail_from_bytes downscales it to the
# standard THUMB_MAX_DIM thumbnail, and sharp enough on its own to be a
# decent OCR source for image-only PDFs (see extract_text_for_row below).
RENDER_ZOOM = 2.0


def _open(path):
    doc = fitz.open(path)
    if doc.needs_pass:
        # We never have a password to hand it — treat exactly like any other
        # unreadable PDF rather than raising.
        doc.close()
        return None
    return doc


def render_first_page(path):
    """PNG bytes for page 1 of the PDF at `path`, or None on any failure
    (corrupt/encrypted/zero-page PDF, missing file)."""
    try:
        doc = _open(path)
        if doc is None:
            return None
        try:
            if doc.page_count == 0:
                return None
            pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM))
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception as e:
        print(f"PDF render failed for {path}: {e!r}")
        return None


def extract_text(path):
    """The PDF's embedded text layer, all pages concatenated, or "" if
    there isn't one (an image-only/scanned PDF), the PDF is
    corrupt/encrypted, or the file is missing. "" (not None) on the
    no-text-layer case specifically, so callers can tell "found nothing" and
    "wasn't even a PDF" apart if they ever need to — today core/ocr.py just
    treats falsy either way as "fall back to OCR"."""
    try:
        doc = _open(path)
        if doc is None:
            return ""
        try:
            text = "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
        return text.strip()
    except Exception as e:
        print(f"PDF text extraction failed for {path}: {e!r}")
        return ""


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='pdf' — see
    core/thumbnails.py. media_type='pdf' rows are still UPLOADED_FILE-shaped
    (the row's own stored_filename really is the PDF); CAPTURE is used here
    only because the *representative image* has to be rendered rather than
    being the file itself, the same reasoning as a stream/URL capture."""
    path = _stored_path(row)
    return render_first_page(path) if path else None


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='pdf' — see
    core/ocr.py, which tries this before ever running OCR."""
    path = _stored_path(row)
    return extract_text(path) if path else ""


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="pdf",
    label="PDF document",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    extensions=frozenset(storage.PDF_EXTENSIONS),
    capture_fn=capture_thumbnail,
    text_extract_fn=extract_text_for_row,
    badge_icon="\U0001F4C4",
    badge_text="PDF",
))
