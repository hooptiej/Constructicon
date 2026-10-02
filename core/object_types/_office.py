"""Shared Microsoft Office reading for the spreadsheet, word and powerpoint
types (#478). Not a type itself (underscore module, like _pe/_preview).

Modern Office files (OOXML: .docx/.xlsx/.pptx and their macro/template
variants) are zips of XML, so properties, text and the embedded preview
image come straight out with the standard library (zipfile + ElementTree):
no python-docx/python-pptx. Old binary files (.doc/.xls/.ppt) are OLE
compound files; olefile (already a dependency, #444) reads their summary
properties. A password-protected OOXML file is also an OLE container (an
"EncryptedPackage" stream), so it's recognised and reported rather than
refused.

Every read is bounded (MAX_PART_BYTES per XML part, MAX_TEXT chars of text)
so a huge document can't blow up memory at upload.
"""

import datetime
import re
import xml.etree.ElementTree as ET
import zipfile

MAX_PART_BYTES = 32 * 1024 * 1024
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def local(tag):
    return tag.rsplit("}", 1)[-1]


def magic(path, n=8):
    with open(path, "rb") as f:
        return f.read(n)


def is_ooxml(path, part):
    """True for an OOXML zip containing `part` (e.g. 'word/document.xml')."""
    if magic(path, 4) != b"PK\x03\x04":
        return False
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in names and part in names


def ole_streams(path):
    """Top-level stream names of an OLE file, or None if it isn't one."""
    if magic(path) != OLE_MAGIC:
        return None
    import olefile
    with olefile.OleFileIO(str(path)) as ole:
        return {"/".join(e) for e in ole.listdir()}


def is_encrypted_ooxml(path):
    streams = ole_streams(path)
    return bool(streams and "EncryptedPackage" in streams)


def read_part(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_PART_BYTES:
        raise ValueError(f"{name} too large")
    return z.read(name)


def _w3c_date(text):
    """docProps W3CDTF ('2024-05-01T10:00:00Z') -> UTC epoch float, or None."""
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ooxml_props(z):
    """Core + app properties of an open OOXML zip as a flat dict."""
    names = set(z.namelist())
    out = {}
    if "docProps/core.xml" in names:
        root = ET.fromstring(read_part(z, "docProps/core.xml"))
        for el in root:
            tag, text = local(el.tag), (el.text or "").strip()
            if not text:
                continue
            if tag == "title":
                out["title"] = text
            elif tag == "creator":
                out["author"] = text
            elif tag == "lastModifiedBy":
                out["last_modified_by"] = text
            elif tag == "created":
                out["created"] = _w3c_date(text)
            elif tag == "modified":
                out["modified"] = _w3c_date(text)
    if "docProps/app.xml" in names:
        root = ET.fromstring(read_part(z, "docProps/app.xml"))
        for el in root:
            tag, text = local(el.tag), (el.text or "").strip()
            if not text:
                continue
            if tag in ("Pages", "Words", "Slides", "Notes", "HiddenSlides") and text.isdigit():
                out[tag.lower()] = int(text)
            elif tag == "Application":
                out["application"] = text
            elif tag == "Company":
                out["company"] = text
    out["macros"] = any(n.endswith("vbaProject.bin") for n in names)
    return {k: v for k, v in out.items() if v is not None}


def ooxml_thumbnail(path):
    """The preview image an OOXML file carries (docProps/thumbnail.*), as
    bytes, when it's JPEG or PNG (EMF/WMF thumbnails aren't renderable here)."""
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().startswith("docprops/thumbnail."):
                    data = read_part(z, name)
                    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                        return data
    except Exception as e:
        print(f"Office thumbnail read failed for {path}: {e!r}")
    return None


def ole_props(path):
    """Summary properties of an old binary Office file via olefile."""
    import olefile
    out = {}
    with olefile.OleFileIO(str(path)) as ole:
        meta = ole.get_metadata()
        for attr, key in (("title", "title"), ("author", "author"),
                          ("last_saved_by", "last_modified_by"), ("company", "company"),
                          ("creating_application", "application")):
            value = getattr(meta, attr, None)
            if isinstance(value, bytes):
                value = value.decode("cp1252", errors="replace")
            if isinstance(value, str) and value.strip().strip("\x00"):
                out[key] = value.strip().strip("\x00")
        for attr, key in (("create_time", "created"), ("last_saved_time", "modified")):
            value = getattr(meta, attr, None)
            if isinstance(value, datetime.datetime) and value.year > 1980:
                out[key] = value.replace(tzinfo=value.tzinfo or datetime.timezone.utc).timestamp()
        for attr, key in (("num_pages", "pages"), ("num_words", "words"), ("slides", "slides"),
                          ("notes", "notes")):
            value = getattr(meta, attr, None)
            if isinstance(value, int) and value > 0:
                out[key] = value
        out["macros"] = any("VBA" in "/".join(e) or "Macros" in "/".join(e) for e in ole.listdir())
    return out


def paragraphs(xml_bytes, para_tag, text_tag):
    """Text of each paragraph element (e.g. w:p / w:t, a:p / a:t)."""
    out = []
    for el in ET.fromstring(xml_bytes).iter():
        if local(el.tag) == para_tag:
            text = "".join(t.text or "" for t in el.iter() if local(t.tag) == text_tag)
            if text.strip():
                out.append(text.strip())
    return out


def numbered(names, pattern):
    """Zip members matching e.g. r'^ppt/slides/slide(\\d+)\\.xml$', in numeric order."""
    rx = re.compile(pattern)
    hits = [(int(m.group(1)), n) for n in names for m in [rx.match(n)] if m]
    return [n for _, n in sorted(hits)]


def date_label(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d") if ts else None


def common_props(stats):
    """The properties every Office type shows, in a consistent order."""
    props = {}
    if stats.get("encrypted"):
        props["Protection"] = "Password-protected: contents not readable without the password"
    for key, label in (("title", "Title"), ("author", "Author"), ("last_modified_by", "Last modified by"),
                       ("company", "Company"), ("application", "Made with")):
        if stats.get(key):
            props[label] = stats[key]
    if stats.get("created"):
        props["Created"] = date_label(stats["created"])
    if stats.get("modified"):
        props["Modified"] = date_label(stats["modified"])
    if stats.get("macros"):
        props["Macros"] = "Yes (contains VBA macros)"
    return props
