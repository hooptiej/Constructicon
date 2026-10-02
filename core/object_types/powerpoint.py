"""PowerPoint decks (#478): .pptx .pptm .ppsx .ppsm .potx, and old binary .ppt.

Modern files: properties (title, author, slides, notes, hidden slides,
dates, macros), the text of every slide in order plus speaker notes for
search, and the slide outline (each slide's first line) for the object page.
All of it comes from the deck's XML with the standard library
(core/object_types/_office.py). PowerPoint's embedded preview image (usually
the first slide) is the thumbnail when present.

Old .ppt: summary properties via olefile only.
"""

import zipfile
from pathlib import Path

from markupsafe import Markup, escape

from .. import storage
from . import _office, _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "powerpoint_stats"
MAX_OUTLINE = 40
MODERN = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}
_SLIDE = r"^ppt/slides/slide(\d+)\.xml$"
_NOTES = r"^ppt/notesSlides/notesSlide(\d+)\.xml$"


def _path(row):
    stored = row.get("stored_filename")
    p = storage.path_for(stored) if stored else None
    return p if p and p.exists() else None


def sniff(path, filename):
    ext = Path(filename).suffix.lower()
    if ext in MODERN:
        return _office.is_ooxml(path, "ppt/presentation.xml") or _office.is_encrypted_ooxml(path)
    if ext == ".ppt":
        streams = _office.ole_streams(path)
        return bool(streams and "PowerPoint Document" in streams)
    return False


def _slides(z):
    """[[paragraph, ...] per slide], in slide order."""
    return [_office.paragraphs(_office.read_part(z, n), "p", "t")
            for n in _office.numbered(z.namelist(), _SLIDE)]


def get_embedded_metadata(path):
    path = Path(path)
    try:
        if path.suffix.lower() == ".ppt":
            stats = {"format": "PowerPoint 97–2003 presentation", **_office.ole_props(path)}
        elif _office.is_encrypted_ooxml(path):
            stats = {"format": "PowerPoint presentation", "encrypted": True}
        else:
            with zipfile.ZipFile(path) as z:
                stats = {"format": "PowerPoint presentation", **_office.ooxml_props(z)}
                stats["has_thumbnail"] = bool(_office.ooxml_thumbnail(path))
                slides = _slides(z)
                # Counted from the parts themselves: app.xml's Slides/Notes
                # are only kept current by PowerPoint (other tools leave
                # template values there).
                stats["slides"] = len(slides)
                stats["outline"] = [s[0][:120] if s else "" for s in slides[:MAX_OUTLINE]]
                stats["notes"] = len(_office.numbered(z.namelist(), _NOTES))
    except Exception as e:
        print(f"PowerPoint scan failed for {path}: {e!r}")
        return {}
    out = {"type_metadata": {STATS_KEY: stats}}
    title = stats.get("title") or next((t for t in stats.get("outline", []) if t), None)
    if title:
        out["content_description"] = title
    if stats.get("created"):
        out["content_date"] = stats["created"]
    return out


def extract_text_for_row(row):
    """text_extract_fn: every slide's text, then speaker notes, capped."""
    path = _path(row)
    if not path or path.suffix.lower() not in MODERN:
        return ""
    try:
        with zipfile.ZipFile(path) as z:
            parts = ["\n".join(s) for s in _slides(z)]
            notes = ["\n".join(_office.paragraphs(_office.read_part(z, n), "p", "t"))
                     for n in _office.numbered(z.namelist(), _NOTES)]
            return "\n\n".join(p for p in parts + notes if p)[:storage.MAX_EXTRACTED_TEXT_CHARS]
    except Exception as e:
        print(f"PowerPoint text extraction failed for {path}: {e!r}")
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
        props = {"Format": stats.get("format", "PowerPoint presentation")}
        props.update(_office.common_props(stats))
        if stats.get("slides"):
            hidden = f" ({stats['hiddenslides']} hidden)" if stats.get("hiddenslides") else ""
            props["Slides"] = f"{stats['slides']:,}{hidden}"
        if stats.get("notes"):
            props["Speaker notes"] = f"{stats['notes']:,} slides"
        return props
    except Exception as e:
        print(f"PowerPoint properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """Embedded thumbnail if present; the slide outline either way."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    stats = (ctx.item.get("type_metadata") or {}).get(STATS_KEY) or {}
    outline = stats.get("outline") or []
    html = str(_preview.thumb_with_original_link(ctx)) if ctx.thumb_url and stats.get("has_thumbnail") else ""
    if outline:
        empty = '<span class="muted">(no text)</span>'
        items = "".join(f"<li>{escape(t) if t else empty}</li>" for t in outline)
        more = f'<p class="muted">…first {len(outline)} of {stats.get("slides")} slides</p>' if stats.get("slides", 0) > len(outline) else ""
        html += f'<div class="markdown-body markdown-file"><h3>Slides</h3><ol>{items}</ol>{more}</div>'
    return Markup(html) if html else None


register(ObjectTypeSpec(
    key="powerpoint",
    label="PowerPoint presentation",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    caption_capable=False,
    extensions=frozenset(MODERN | {".ppt"}),
    sniff_fn=sniff,
    capture_fn=capture_thumbnail,
    has_thumbnail_fn=has_thumbnail,  # only when the file embeds a preview
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,
    preview_fn=preview,
    preview_assets=("prose",),
    badge_icon="\U0001F4FD",  # film projector
    badge_text="PPT",
))
