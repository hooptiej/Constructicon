"""Word documents (#478): .docx .docm .dotx .dotm, and old binary .doc.

Modern files: properties (title, author, pages, words, dates, macros) and
body text come straight from the document's XML (core/object_types/_office
.py, standard library only); the preview image Word embeds
(docProps/thumbnail.*) is the thumbnail when present. The object page shows
that thumbnail, or otherwise the opening paragraphs as text.

Old .doc: summary properties via olefile, and (#724) the text of a Word
97-2003 file read from its piece table (doc_text below, olefile only, no
antiword/LibreOffice): every text story (body, headers, footnotes, text
boxes) in stream order, field codes dropped and their results kept. Word
6/95 and older, password-protected and unrecognised files have no text we
can read (None: OCR settles "not compatible"); a broken file raises (failed).
Not handled: tracked-change deletions still in the file are read as text.

Password-protected files are recognised (an OLE "EncryptedPackage") and
reported as protected instead of refused.
"""

import re
import struct
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


# ---------------------------------------------------------------- legacy .doc text (#724)

FIB_WORD97 = 0xA5EC   # FIB.wIdent of a Word document (Word 6 also used it; nFib tells them apart)
FIB_WORD6 = 0xA5DC    # wIdent of some Word 6/95 files
NFIB_WORD97 = 0x00C1  # the lowest nFib of the Word 97-2003 binary format
_FCLX = 0x01A2        # fibRgFcLcb97.fcClx: the piece table's place in the table stream
_MAX_PIECES = 1_000_000
# Characters that stand for something other than text: paragraph, line, page and cell marks
# become breaks; picture/footnote/annotation anchors, separators and optional hyphens go.
_DOC_CONTROL = str.maketrans({"\r": "\n", "\x0b": "\n", "\x0c": "\n", "\x07": "\t", "\x1e": "-",
                              **{chr(c): None for c in (0, 1, 2, 3, 4, 5, 6, 8, 0x1f)},
                              **{chr(c): " " for c in range(0x0e, 0x1e) if c not in (0x13, 0x14, 0x15)}})


def _drop_field_codes(text):
    """Word fields are \\x13 code \\x14 result \\x15 (nestable, the result optional): keep the
    results (a hyperlink's words, a page number), drop the codes (HYPERLINK "http://...")."""
    out, stack = [], []
    for ch in text:
        if ch == "\x13":
            stack.append(True)        # in a code
        elif ch == "\x14":
            if stack:
                stack[-1] = False     # in the result
        elif ch == "\x15":
            if stack:
                stack.pop()
        elif not any(stack):
            out.append(ch)
    return "".join(out)


def _tidy(text):
    text = _drop_field_codes(text).translate(_DOC_CONTROL)
    text = re.sub(r"\t{2,}", "\n", text)      # a cell mark then the row's end mark: the row ends
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def doc_text(path, cap=None):
    """#724: the text of a Word 97-2003 .doc, from its piece table (MS-DOC 2.4.1).

    Returns the text (capped at `cap` characters; "" for a document with no text), or None when
    this isn't a format we read: Word 6/95 or older, password-protected, or a WordDocument stream
    that isn't a Word file at all. Raises (ValueError, or olefile's own error) when the file is a
    Word 97+ document that is broken: truncated, a piece table pointing outside the file."""
    import olefile
    cap = cap or storage.MAX_EXTRACTED_TEXT_CHARS
    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists("WordDocument"):
            return None
        wd = _office.read_ole_stream(ole, "WordDocument")
        if len(wd) < 0x20:
            raise ValueError("WordDocument stream too short for a FIB")
        ident, nfib = struct.unpack_from("<HH", wd, 0)
        if ident not in (FIB_WORD97, FIB_WORD6):
            return None                       # not a Word FIB: unrecognised
        if nfib < NFIB_WORD97:
            return None                       # Word 6/95 or older: another layout
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        if flags & 0x0100:
            return None                       # fEncrypted: password-protected, nothing to read
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            raise ValueError(f"{table_name} stream missing")
        table = _office.read_ole_stream(ole, table_name)
    if len(wd) < _FCLX + 8:
        raise ValueError("FIB truncated before fcClx")
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, _FCLX)
    if not lcb_clx or fc_clx + lcb_clx > len(table):
        raise ValueError("piece table outside the table stream")
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:    # Prc (property modifiers): skip
        if i + 3 > len(clx):
            raise ValueError("Prc truncated")
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i + 5 > len(clx) or clx[i] != 0x02:
        raise ValueError("no Pcdt in the piece table")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    if len(plc) < lcb or lcb < 16 or (lcb - 4) % 12:
        raise ValueError("piece table has a bad size")
    n = (lcb - 4) // 12
    if n > _MAX_PIECES:
        raise ValueError(f"piece table too large ({n:,} pieces)")
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    out, size = [], 0
    for k in range(n):
        count = cps[k + 1] - cps[k]
        if count < 0:
            raise ValueError("piece table out of order")
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
        if fc & 0x40000000:                   # compressed: one byte per character, cp1252
            off = (fc & ~0x40000000) // 2
            raw = wd[off:off + count]
            if len(raw) < count:
                raise ValueError("a text piece runs past the end of the document")
            piece = raw.decode("cp1252", "replace")
        else:                                 # UTF-16LE
            raw = wd[fc:fc + 2 * count]
            if len(raw) < 2 * count:
                raise ValueError("a text piece runs past the end of the document")
            piece = raw.decode("utf-16-le", "replace")
        out.append(piece)
        size += len(piece)
        if size > cap * 2:                    # room for the codes and marks _tidy drops
            break
    return _tidy("".join(out))[:cap]


def extract_text_for_row(row):
    """text_extract_fn: the body text, capped, for search. A legacy .doc reads through doc_text;
    None (Word 6/95, protected, unrecognised) settles "not compatible" in core/ocr.py."""
    path = _path(row)
    if not path:
        return ""
    try:
        if path.suffix.lower() not in MODERN:
            return doc_text(path, storage.MAX_EXTRACTED_TEXT_CHARS)
        with zipfile.ZipFile(path) as z:
            return "\n".join(_body(z))[:storage.MAX_EXTRACTED_TEXT_CHARS]
    except Exception as e:
        print(f"Word text extraction failed for {path}: {e!r}")
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
    # #724: a .doc whose text doc_text can't read (Word 6/95 or older, password-protected, unrecognised)
    ocr_unsupported_note={".doc": "legacy .doc: older Word format or password-protected, no readable text"},
    text_source="office",  # #724: text_extract_fn's text is read from the document itself
))
