"""#724 (c): an item's searchable text from more than one source.

Some types hold text of their own beside the picture OCR reads: a YouTube video's stored
description today, its transcript later (#724 part e). Search indexes one column,
`extracted_text`, so the sources are concatenated there under headed sections rather than one
clobbering another (the #724 findings chose this over a new FTS column):

    Description:
    <the video's description>

    Thumbnail text:
    <what OCR read off the thumbnail>

A type declares its sources with `ObjectTypeSpec.text_sources_fn(row) -> [TextSource, ...]`
(text the row already holds: no file read, no network). `combine()` takes any number of them plus
the picture's OCR text, so a new source is one more TextSource, not new plumbing.

Rules:
- Only sources with text get a section, in the order given; the picture's text always goes LAST.
- When no source has text, the result is the picture's OCR text alone, with no header and no
  text_source: exactly what OCR stored before part c, so those rows read the same.
- `type_metadata.text_source` records the sources that gave text, joined with "+" in section
  order ("description", later "description+transcript"). OCR'd picture text never adds to it
  (the #724 convention: OCR text carries no text_source).
- `picture_text_of()` recovers the picture's section from stored text, so the text can be rebuilt
  when a source changes (a description corrected later, the part c backfill) without OCR'ing the
  picture again. Its one blind spot: a source whose own text contains a line "Thumbnail text:"
  after a blank line, on a row with no picture text, would be split there. Nothing is lost (the
  rebuilt text still holds every word); only the section boundary moves.
"""

from collections import namedtuple

# key: the text_source word ("description", "transcript"); label: the section's header.
TextSource = namedtuple("TextSource", "key label text")

PICTURE_LABEL = "Thumbnail text"
_PICTURE_HEADER = f"{PICTURE_LABEL}:\n"
_SECTION_GAP = "\n\n"


def _clean(text):
    return text.strip() if isinstance(text, str) else ""


def combine(sources, picture_text=""):
    """(extracted_text, text_source or None) for these sources plus the picture's OCR text."""
    picture = _clean(picture_text)
    parts, keys = [], []
    for src in sources or ():
        text = _clean(src.text)
        if not text:
            continue
        parts.append(f"{src.label}:\n{text}")
        if src.key and src.key not in keys:
            keys.append(src.key)
    if not parts:
        return picture, None
    if picture:
        parts.append(_PICTURE_HEADER + picture)
    return _SECTION_GAP.join(parts), "+".join(keys) or None


def picture_text_of(extracted_text, text_source):
    """The picture's OCR text inside stored `extracted_text`. With no text_source the whole text is
    OCR's (a row stored before part c, or one whose sources were all empty); with one, it is the
    last "Thumbnail text:" section, or "" when there is none."""
    text = extracted_text or ""
    if not text_source:
        return text.strip()
    marker = _SECTION_GAP + _PICTURE_HEADER
    at = text.rfind(marker)
    return text[at + len(marker):].strip() if at >= 0 else ""


def type_sources(row, spec):
    """The TextSources `spec` declares for `row` ([] for a type without text_sources_fn)."""
    fn = getattr(spec, "text_sources_fn", None)
    return list(fn(row) or ()) if fn is not None else []


def labels_of(sources):
    """'description' / 'description and transcript': the sources with text, for a note."""
    names = [s.label.lower() for s in sources or () if _clean(s.text)]
    return " and ".join(names)


def resettle(row, spec, unsupported_note):
    """Rebuild a settled row's text from its type's sources and the picture text already stored.
    Returns None when nothing changes, else {"text", "source", "status", "note"} to write:
    - some text -> status 'done' (the row's note is kept: `note` is None unless the status moves);
    - no text at all (no source text, no picture text) -> 'unsupported' with `unsupported_note`.
    Only for rows whose OCR has settled ('done' or 'unsupported'): a pending row's run combines
    the sources itself, and a 'failed' row keeps its real error until Retry."""
    status = row.get("ocr_status")
    if status not in ("done", "unsupported"):
        return None
    meta = row.get("type_metadata") or {}
    if not isinstance(meta, dict):
        return None
    old_text = row.get("extracted_text") or ""
    old_source = meta.get("text_source") or None
    picture = picture_text_of(old_text, old_source)
    text, source = combine(type_sources(row, spec), picture)
    if text:
        new_status, note = "done", None
    else:
        new_status, note = "unsupported", unsupported_note
    if text == old_text and source == old_source and new_status == status:
        return None
    return {"text": text, "source": source, "status": new_status,
            "note": note if new_status != status else None, "status_changed": new_status != status}


def resettled_meta(meta, change):
    """`meta` (a type_metadata dict) with a resettle() change applied: text_source set or removed,
    and, when the status moves, ocr_note set (unsupported) or removed (done). A new dict."""
    out = dict(meta)
    if change["source"]:
        out["text_source"] = change["source"]
    else:
        out.pop("text_source", None)
    if change["status_changed"]:
        if change["note"]:
            out["ocr_note"] = change["note"]
        else:
            out.pop("ocr_note", None)
    return out
