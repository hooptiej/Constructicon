"""Word documents (#478): .docx .docm .dotx .dotm, and old binary .doc.

Modern files: properties (title, author, pages, words, dates, macros) and
body text come straight from the document's XML (core/object_types/_office
.py, standard library only); the preview image Word embeds
(docProps/thumbnail.*) is the thumbnail when present. The object page shows
that thumbnail, or otherwise the opening paragraphs as text.

Old .doc: summary properties via olefile. Its text needs a dedicated Word
binary parser, so .doc is searchable by its properties only.

Password-protected files are recognised (an OLE "EncryptedPackage") and
reported as protected instead of refused.
"""

import zipfile
from pathlib import Path

from markupsafe import Markup, escape

from .. import storage
from . import _office, _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "word_stats"
PREVIEW_PARAGRAPHS = 12
MODERN = {".docx", ".docm", ".dotx", ".dotm"}


def _path(row):
    stored = row.get("stored_filename")
    p = storage.path_for(stored) if stored else None
    return p if p and p.exists() else None


def sniff(path, filename):
    ext = Path(filename).suffix.lower()
    if ext in MODERN:
        return _office.is_ooxml(path, "word/document.xml") or _office.is_encrypted_ooxml(path)
    if ext == ".doc":
        streams = _office.ole_streams(path)
        return bool(streams and "WordDocument" in streams)
    return False


def _body(z):
    return _office.paragraphs(_office.read_part(z, "word/document.xml"), "p", "t")


def get_embedded_metadata(path):
    path = Path(path)
    try:
        if path.suffix.lower() == ".doc":
            stats = {"format": "Word 97–2003 document", **_office.ole_props(path)}
        elif _office.is_encrypted_ooxml(path):
            stats = {"format": "Word document", "encrypted": True}
        else:
            with zipfile.ZipFile(path) as z:
                stats = {"format": "Word document", **_office.ooxml_props(z)}
                body = _body(z)
                stats["has_thumbnail"] = bool(_office.ooxml_thumbnail(path))
                stats["paragraphs"] = len(body)
                stats["opening"] = body[:PREVIEW_PARAGRAPHS]
                # Count from the body itself: docProps/app.xml's Words is
                # only kept current by Word (other tools leave template
                # zeros there). Pages can't be recomputed, so that stays.
                stats["words"] = sum(len(p.split()) for p in body)
    except Exception as e:
        print(f"Word scan failed for {path}: {e!r}")
        return {}
    out = {"type_metadata": {STATS_KEY: stats}}
    if stats.get("title"):
        out["content_description"] = stats["title"]
    if stats.get("created"):
        out["content_date"] = stats["created"]
    return out


def extract_text_for_row(row):
    """text_extract_fn: the body text, capped, for search (.doc: none)."""
    path = _path(row)
    if not path or path.suffix.lower() not in MODERN:
        return ""
    try:
        with zipfile.ZipFile(path) as z:
            return "\n".join(_body(z))[:storage.MAX_EXTRACTED_TEXT_CHARS]
    except Exception as e:
        print(f"Word text extraction failed for {path}: {e!r}")
        return ""


def has_thumbnail(row):
    """has_thumbnail_fn: only files that carry their own preview image."""
    return bool(((row.get("type_metadata") or {}).get(STATS_KEY) or {}).get("has_thumbnail"))


def capture_thumbnail(row):
    path = _path(row)
    return _office.ooxml_thumbnail(path) if path and path.suffix.lower() in MODERN else None


def get_properties(row):
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats and _path(row):
            stats = (get_embedded_metadata(_path(row)).get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            return {}
        props = {"Format": stats.get("format", "Word document")}
        props.update(_office.common_props(stats))
        if stats.get("pages"):
            props["Pages"] = f"{stats['pages']:,}"
        if stats.get("words"):
            props["Words"] = f"{stats['words']:,}"
        return props
    except Exception as e:
        print(f"Word properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """Embedded thumbnail if there is one, else the opening paragraphs."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    stats = (ctx.item.get("type_metadata") or {}).get(STATS_KEY) or {}
    if ctx.thumb_url and stats.get("has_thumbnail"):
        return _preview.thumb_with_original_link(ctx)
    opening = stats.get("opening") or []
    if not opening:
        return None
    body = "".join(f"<p>{escape(p)}</p>" for p in opening)
    more = '<p class="muted">…opening paragraphs; download for the full document</p>' if stats.get("paragraphs", 0) > len(opening) else ""
    return Markup(f'<div class="markdown-body markdown-file">{body}{more}</div>')


register(ObjectTypeSpec(
    key="word",
    label="Word document",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,  # runs the background task that calls text_extract_fn (no OCR)
    caption_capable=False,
    extensions=frozenset(MODERN | {".doc"}),
    sniff_fn=sniff,
    capture_fn=capture_thumbnail,
    has_thumbnail_fn=has_thumbnail,  # only when the file embeds a preview
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,
    preview_fn=preview,
    preview_assets=("prose",),
    badge_icon="\U0001F4C4",
    badge_text="DOC",
))
