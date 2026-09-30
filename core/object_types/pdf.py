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

import re
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

from .. import storage

# 2x zoom on PyMuPDF's default 72 DPI render gives a ~144 DPI page image —
# plenty sharp once storage.save_thumbnail_from_bytes downscales it to the
# standard THUMB_MAX_DIM thumbnail, and sharp enough on its own to be a
# decent OCR source for image-only PDFs (see extract_text_for_row below).
RENDER_ZOOM = 2.0

# #433: letter/A4 pages at RENDER_ZOOM stay unchanged (1224x1584), oversized
# drawing sheets are scaled down instead of building a 100+ MB pixmap for a
# 400 px thumbnail.
MAX_RENDER_DIM = 2000


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
            page = doc.load_page(0)
            longest = max(page.rect.width, page.rect.height)
            zoom = min(RENDER_ZOOM, MAX_RENDER_DIM / longest) if longest > 0 else RENDER_ZOOM
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception as e:
        print(f"PDF render failed for {path}: {e!r}")
        return None


def extract_text(path):
    """The PDF's embedded text layer, all pages concatenated (capped at
    storage.MAX_EXTRACTED_TEXT_CHARS, #433), or "" if there isn't one
    (an image-only/scanned PDF), the PDF is corrupt/encrypted, or the file
    is missing. "" (not None) on the no-text-layer case specifically, so
    callers can tell "found nothing" and "wasn't even a PDF" apart if they
    ever need to — today core/ocr.py just treats falsy either way as "fall
    back to OCR"."""
    try:
        doc = _open(path)
        if doc is None:
            return ""
        try:
            cap = storage.MAX_EXTRACTED_TEXT_CHARS
            text_parts = []
            running_length = 0
            pages_read = 0
            for page_num, page in enumerate(doc):
                page_text = page.get_text()
                page_length = len(page_text)
                if running_length + page_length > cap:
                    # Adding this page would exceed the cap; truncate here
                    remaining = cap - running_length
                    if remaining > 0:
                        text_parts.append(page_text[:remaining])
                    print(f"PDF text capped at {cap} chars after {pages_read} of {doc.page_count} pages: {path}")
                    break
                text_parts.append(page_text)
                running_length += page_length
                pages_read += 1
            # Slice after joining: the "\n" separators count toward the cap too.
            text = "\n".join(text_parts)[:cap]
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


def _parse_pdf_date(raw):
    """PDF date strings look like 'D:20230615120000+00'00'' (ISO 8601-ish,
    PDF's own format, not ISO). Returns a friendly 'Jun 15, 2023', or None
    if `raw` is empty/unparseable."""
    if not raw:
        return None
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%b %-d, %Y")
    except ValueError:
        return None


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='pdf' — page count plus
    whatever of title/author/creation-date the PDF's own metadata actually
    has set (most PDFs have some but not all of these), or {} on any
    failure."""
    path = _stored_path(row)
    if not path:
        return {}
    try:
        doc = _open(path)
        if doc is None:
            return {}
        try:
            props = {"Pages": str(doc.page_count)}
            meta = doc.metadata or {}
            if meta.get("title"):
                props["Title"] = meta["title"]
            if meta.get("author"):
                props["Author"] = meta["author"]
            created = _parse_pdf_date(meta.get("creationDate"))
            if created:
                props["Created"] = created
            return props
        finally:
            doc.close()
    except Exception as e:
        print(f"PDF properties extraction failed for {path}: {e!r}")
        return {}


from . import _preview


def preview(ctx):
    """#449 preview_fn: rendered first page + "View original" link. None (-> the page's generic fallback) when there's no thumbnail."""
    return _preview.thumb_with_original_link(ctx) if ctx.thumb_url else None


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    preview_fn=preview,  # #449
    key="pdf",
    label="PDF document",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    extensions=frozenset({".pdf"}),
    capture_fn=capture_thumbnail,
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    badge_icon="\U0001F4C4",
    badge_text="PDF",
))
