"""PowerPoint decks (#478): .pptx .pptm .ppsx .ppsm .potx, and old binary .ppt.

Modern files: properties (title, author, slides, notes, hidden slides,
dates, macros), the text of every slide in order plus speaker notes for
search, and the slide outline (each slide's first line) for the object page.
All of it comes from the deck's XML with the standard library
(core/object_types/_office.py). PowerPoint's embedded preview image (usually
the first slide) is the thumbnail when present.

Old .ppt: summary properties via olefile, and (#724) the text of a
PowerPoint 97-2003 deck (ppt_text below, olefile only): the current edit's
persist directory leads to the document, its slide list gives the slides in
order, and each slide's text (its outline text and its own text boxes) is
read, then the speaker notes. Masters (title/notes/handout) are never
visited, so their "Click to edit Master title style" placeholders stay out.
PowerPoint 95 and older, password-protected and unrecognised files have no
text we can read (None: OCR settles "not compatible"); a broken file raises
(failed).
"""

import re
import struct
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


# ---------------------------------------------------------------- legacy .ppt text (#724)
# Record types (MS-PPT 2.13.24).
RT_DOCUMENT = 0x03E8
RT_SLIDE = 0x03EE
RT_NOTES = 0x03F0
RT_SLIDE_PERSIST = 0x03F3
RT_USER_EDIT = 0x0FF5
RT_CURRENT_USER = 0x0FF6
RT_SLIDE_LIST = 0x0FF0
RT_TEXT_CHARS = 0x0FA0
RT_TEXT_BYTES = 0x0FA8
RT_PERSIST_DIR = 0x1772
HEADER_TOKEN = 0xE391C05F            # CurrentUserAtom: not encrypted
HEADER_TOKEN_ENCRYPTED = 0xF3D1C4DF
DOC_FILE_VERSION = 0x03F4            # PowerPoint 97-2003
_MAX_RECORDS = 2_000_000             # records walked per deck, a backstop on hostile nesting
_MAX_DEPTH = 64
_MAX_EDITS = 10_000                  # UserEditAtoms followed (one per incremental save)


def _rec(data, off, end):
    """(version, instance, type, body start, body end) of the record at `off`, inside [.., end)."""
    if off + 8 > end:
        raise ValueError(f"record header at {off} runs past its container")
    vi, rtype, rlen = struct.unpack_from("<HHI", data, off)
    body = off + 8
    if body + rlen > end:
        raise ValueError(f"record 0x{rtype:04X} at {off} runs past its container")
    return vi & 0x0F, vi >> 4, rtype, body, body + rlen


def _children(data, start, end, budget):
    off = start
    while off < end:
        rec = _rec(data, off, end)
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("too many records")
        yield rec
        off = rec[4]


def _atom_text(data, rtype, body, end):
    if rtype == RT_TEXT_CHARS:
        text = data[body:end].decode("utf-16-le", "replace")
    else:
        text = data[body:end].decode("latin-1")
    text = text.replace("\r", "\n").replace("\x0b", "\n")
    return re.sub(r"[\x00-\x08\x0c\x0e-\x1f]", "", text).strip()


def _texts_in(data, start, end, budget, depth=0):
    """Every text atom inside a container, in file order."""
    if depth > _MAX_DEPTH:
        raise ValueError("records nested too deep")
    out = []
    for ver, _inst, rtype, body, rend in _children(data, start, end, budget):
        if ver == 0x0F:
            out += _texts_in(data, body, rend, budget, depth + 1)
        elif rtype in (RT_TEXT_CHARS, RT_TEXT_BYTES):
            out.append(_atom_text(data, rtype, body, rend))
    return out


def _persist_map(data, first_edit, budget):
    """{persist id: stream offset} from the newest edit back (newer wins), and the document's id."""
    persist, doc_ref, off, seen = {}, None, first_edit, set()
    while off not in seen:  # the current edit first (offset 0 included), then each older one until 0
        if len(seen) >= _MAX_EDITS:
            raise ValueError("too many edits")
        seen.add(off)
        _v, _i, rtype, body, rend = _rec(data, off, len(data))
        if rtype != RT_USER_EDIT or rend - body < 20:
            raise ValueError(f"no UserEditAtom at {off}")
        last_edit, dir_off, doc = struct.unpack_from("<III", data, body + 8)
        if doc_ref is None:
            doc_ref = doc
        _v, _i, rtype, dbody, dend = _rec(data, dir_off, len(data))
        if rtype != RT_PERSIST_DIR:
            raise ValueError(f"no PersistDirectoryAtom at {dir_off}")
        p = dbody
        while p + 4 <= dend:
            head = struct.unpack_from("<I", data, p)[0]
            first, count = head & 0xFFFFF, head >> 20
            p += 4
            if p + 4 * count > dend:
                raise ValueError("persist directory entry truncated")
            for k in range(count):
                persist.setdefault(first + k, struct.unpack_from("<I", data, p + 4 * k)[0])
            budget[0] -= count
            if budget[0] < 0:
                raise ValueError("too many persist entries")
            p += 4 * count
        if not last_edit:
            break
        off = last_edit
    return persist, doc_ref


def _container_at(data, persist, ref, want, budget):
    if ref not in persist:
        raise ValueError(f"persist id {ref} has no offset")
    ver, _i, rtype, body, rend = _rec(data, persist[ref], len(data))
    if ver != 0x0F or rtype != want:
        raise ValueError(f"persist id {ref} is record 0x{rtype:04X}, not 0x{want:04X}")
    return body, rend


def ppt_text(path, cap=None):
    """#724: the text of a PowerPoint 97-2003 .ppt (MS-PPT): each slide in order (its outline
    text from the slide list, then its own text boxes), then the speaker notes, slides separated by
    a blank line. Master slides are never read, so their placeholder text never appears.

    Returns the text (capped at `cap` characters; "" when the deck holds none), or None when this
    isn't a format we read: no Current User stream, PowerPoint 95 or older, password-protected.
    Raises when it is a PowerPoint 97+ deck that is broken (truncated records, bad offsets)."""
    import olefile
    cap = cap or storage.MAX_EXTRACTED_TEXT_CHARS
    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists("PowerPoint Document") or not ole.exists("Current User"):
            return None
        cu = _office.read_ole_stream(ole, "Current User", cap=64 * 1024)
        data = _office.read_ole_stream(ole, "PowerPoint Document")
    if len(cu) < 28:
        return None
    rtype = struct.unpack_from("<H", cu, 2)[0]
    token, first_edit, _name_len, file_version, major = struct.unpack_from("<IIHHB", cu, 12)
    if rtype != RT_CURRENT_USER or token not in (HEADER_TOKEN, HEADER_TOKEN_ENCRYPTED):
        return None                           # not a CurrentUserAtom we know
    if token == HEADER_TOKEN_ENCRYPTED:
        return None                           # password-protected
    if file_version != DOC_FILE_VERSION or major != 3:
        return None                           # PowerPoint 95 or older
    budget = [_MAX_RECORDS]
    persist, doc_ref = _persist_map(data, first_edit, budget)
    dbody, dend = _container_at(data, persist, doc_ref, RT_DOCUMENT, budget)

    slides, notes = [], []   # [(persist ref, [outline text])]
    for ver, inst, rtype, body, rend in _children(data, dbody, dend, budget):
        if rtype != RT_SLIDE_LIST or ver != 0x0F or inst not in (0, 2):
            continue                          # instance 1 is the masters' list
        target = slides if inst == 0 else notes
        for _cv, _ci, ctype, cbody, cend in _children(data, body, rend, budget):
            if ctype == RT_SLIDE_PERSIST:
                if cend - cbody < 4:
                    raise ValueError("SlidePersistAtom truncated")
                target.append((struct.unpack_from("<I", data, cbody)[0], []))
            elif ctype in (RT_TEXT_CHARS, RT_TEXT_BYTES) and target:
                target[-1][1].append(_atom_text(data, ctype, cbody, cend))

    parts, size = [], 0
    for entries, want in ((slides, RT_SLIDE), (notes, RT_NOTES)):
        for ref, outline in entries:
            sbody, send = _container_at(data, persist, ref, want, budget)
            seen, lines = set(), []
            for t in outline + _texts_in(data, sbody, send, budget):
                if t and t != "*" and t not in seen:  # "*" is a slide-number field's placeholder
                    seen.add(t)
                    lines.append(t)
            if lines:
                parts.append("\n".join(lines))
                size += len(parts[-1])
                if size > cap:
                    return "\n\n".join(parts)[:cap]
    return "\n\n".join(parts)[:cap]


def extract_text_for_row(row):
    """text_extract_fn: every slide's text, then speaker notes, capped. A legacy .ppt reads
    through ppt_text; None (PowerPoint 95, protected, unrecognised) settles "not compatible"."""
    path = _path(row)
    if not path:
        return ""
    try:
        if path.suffix.lower() not in MODERN:
            return ppt_text(path, storage.MAX_EXTRACTED_TEXT_CHARS)
        with zipfile.ZipFile(path) as z:
            parts = ["\n".join(s) for s in _slides(z)]
            notes = ["\n".join(_office.paragraphs(_office.read_part(z, n), "p", "t"))
                     for n in _office.numbered(z.namelist(), _NOTES)]
            return "\n\n".join(p for p in parts + notes if p)[:storage.MAX_EXTRACTED_TEXT_CHARS]
    except Exception as e:
        print(f"PowerPoint text extraction failed for {path}: {e!r}")
        raise  # #724: a real read error; core/ocr.py records it (failed), never "no text"


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
    # #724: a .ppt whose text ppt_text can't read (PowerPoint 95 or older, password-protected, unrecognised)
    ocr_unsupported_note={".ppt": "legacy .ppt: older PowerPoint format or password-protected, no readable text"},
    text_source="office",  # #724: text_extract_fn's text is read from the deck itself
))
