"""CAD source files (issue #836, owner decision 2026-11-07): the files you EDIT, kept beside the model.

A Thingiverse or Printables bundle ships the printable STL *and* the source it was made from. The
source is the part you can change, so a model's card is incomplete without it. This type stores
those files whole and reads what can be read without CAD software.

Formats (the owner's list): `.step` / `.stp` (STEP, ISO 10303-21), `.f3d` (Autodesk Fusion design),
`.3mf` (3D Manufacturing Format), `.obj` (Wavefront OBJ) and `.igs` / `.iges` (IGES; `.iges` is the
same format under its long name).

What is read (once, at upload, by embedded_metadata_fn; the per-view hooks only format it):
- **STEP** is plain text and its `HEADER` section names the file, the author, the organization, the
  program that wrote it and when (FILE_NAME), what it contains (FILE_DESCRIPTION) and the schema
  (FILE_SCHEMA). Only that header is read (the first 256 KB), never the geometry. The FILE_NAME
  time stamp seeds the item's content date, flagged as export-prone (it is when the exporter ran).
- **3MF** is a zip of XML. The model part's first 128 KB gives its Title, Designer, Application and
  CreationDate metadata and its unit; the package's own thumbnail (the `_rels/.rels` thumbnail
  relationship, else `Metadata/thumbnail.png` and the usual slicer names) becomes the item's picture.
- **OBJ** is text: vertex / face / normal / texture counts, the object / group count, the material
  library names, the model's size in its own units, and the first header comments (a guess at the
  program is labelled as one). Read as a stream, at most 64 MB.
- **F3D**: Fusion's archive is a zip container. What is *verified* is only that: its file count, a
  few member names and a preview image when a member is named like one. Nothing else in it is
  parsed (that needs Fusion), and the page says so. A `.f3d` that is not a zip is still stored.
- **IGES**: the free-text Start section (the author's own description), nothing more.

An item's title is NEVER taken from inside the file (a STEP "name" is often a path or an exporter
banner); only facts, the date and search text come from it.

Safe by construction: every read is bounded (header bytes, zip member sizes, an OBJ's byte cap),
zip members are listed and read in memory but never extracted to disk, all text shown is escaped,
and a malformed file is described as malformed (counts of bad lines, "header unreadable") rather
than refused or silently dropped. `.obj` is the one extension with a real clash (a Windows compiled
object file is also `.obj`), so it and `.step` / `.3mf` are sniffed; `.f3d` and `.igs` are accepted
by extension, because the point of the type is to keep the owner's source files.
"""

import datetime
import logging
import math
import re
import zipfile
from pathlib import Path

from markupsafe import Markup, escape

from .. import besteffort, storage
from . import _preview, register, MetadataField, ObjectTypeSpec, ThumbnailSource

log = logging.getLogger("constructicon.cad_source")

STATS_KEY = "cad_stats"

STEP_EXTS = frozenset({".step", ".stp"})
IGES_EXTS = frozenset({".igs", ".iges"})
EXTENSIONS = STEP_EXTS | IGES_EXTS | frozenset({".f3d", ".3mf", ".obj"})

STEP_HEAD_BYTES = 256 * 1024
MODEL_HEAD_BYTES = 128 * 1024         # a 3MF model part's metadata sits in its first bytes
MAX_THUMB_BYTES = 4 * 1024 * 1024
OBJ_MAX_READ = 64 * 1024 * 1024
MAX_MEMBERS_LISTED = 25
HEAD_LINES = 30                       # lines kept for the preview
LINE_CHARS = 200
FIELD_CHARS = 300

_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


def _stored_path(row):
    stored_filename = (row or {}).get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def _ext(name):
    return Path(name or "").suffix.lower()


def _clean(value, cap=FIELD_CHARS):
    """A header value as one tidy line: a string, or a list of strings joined with '; ' ($ and * are
    STEP's 'unset' marks)."""
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, cap) for v in value]
        return "; ".join(p for p in parts if p)[:cap]
    text = " ".join(str(value or "").split())
    return "" if text in ("$", "*") else text[:cap]


# ---------------------------------------------------------------- STEP (ISO 10303-21)

_STEP_MAGIC = re.compile(rb"^ISO-10303-21\s*;", re.I)
_STEP_X2 = re.compile(r"\\X([24])\\([0-9A-Fa-f]*)\\X0\\")
_STEP_X1 = re.compile(r"\\X\\([0-9A-Fa-f]{2})")
_ENTITY = re.compile(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*(?=\()")
_TOKEN = re.compile(r"[^\s,()']+")


def _step_unescape(text):
    """STEP's string escapes: \\X2\\00E9\\X0\\ (UTF-16), \\X4\\...\\X0\\ (UTF-32), \\X\\E9 (Latin-1), \\\\."""
    def wide(m):
        width = 4 if m.group(1) == "2" else 8
        hexes = m.group(2)
        try:
            return "".join(chr(int(hexes[i:i + width], 16)) for i in range(0, len(hexes), width))
        except (ValueError, OverflowError):  # silent-ok: an escape that isn't valid hex is left as written
            return m.group(0)
    text = _STEP_X2.sub(wide, text)
    text = _STEP_X1.sub(lambda m: chr(int(m.group(1), 16)), text)
    return text.replace("\\\\", "\\")


def _step_value(s, i):
    """One STEP parameter starting at s[i]: a string, a nested list, a typed value (NAME, [..]) or a
    bare token ($, *, .T., a number). Returns (value, next index). Raises ValueError on malformed text."""
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    if i >= n:
        raise ValueError("ran out of text inside a parameter")
    c = s[i]
    if c == "'":
        j, buf = i + 1, []
        while j < n:
            if s[j] == "'":
                if j + 1 < n and s[j + 1] == "'":
                    buf.append("'")
                    j += 2
                    continue
                return _step_unescape("".join(buf)), j + 1
            buf.append(s[j])
            j += 1
        raise ValueError("unterminated string")
    if c == "(":
        items, j = [], i + 1
        while True:
            while j < n and s[j].isspace():
                j += 1
            if j >= n:
                raise ValueError("unterminated list")
            if s[j] == ")":
                return items, j + 1
            if s[j] == ",":
                j += 1
                continue
            value, j = _step_value(s, j)
            items.append(value)
    m = _TOKEN.match(s, i)
    if not m:
        raise ValueError(f"unexpected {c!r}")
    token, j = m.group(0), m.end()
    if j < n and s[j] == "(":  # a typed parameter, NAME(...)
        inner, j = _step_value(s, j)
        return (token, inner), j
    return token, j


def parse_step_header(text):
    """({ENTITY NAME: parameter list}, problem) for the entities of a STEP HEADER section's body. It stops
    at the first thing that is not `NAME(...);` and keeps what it read; `problem` says where and why it
    stopped ("" when it read to the end), so a damaged header is reported, never silently shortened."""
    out, i, n, problem = {}, 0, len(text), ""
    while i < n:
        m = _ENTITY.match(text, i)
        if not m:
            rest = text[i:].strip()
            if rest:
                problem = f"unreadable text after the last entry read: {rest[:40]!r}"
            break
        name = m.group(1).upper()
        try:
            value, j = _step_value(text, m.end())
        except ValueError as e:
            problem = f"the {name} entry is malformed ({e})"
            break
        out.setdefault(name, value)
        k = text.find(";", j)
        if k < 0:
            problem = f"the {name} entry has no closing ';'"
            break
        i = k + 1
    return out, problem


def _schema_label(schema):
    """'AP203' / 'AP214' / 'AP242' for the usual schema names, else ''."""
    s = (schema or "").upper()
    for needle, label in (("AP242", "AP242"), ("AP214", "AP214"), ("AUTOMOTIVE_DESIGN", "AP214"),
                          ("AP203", "AP203"), ("CONFIG_CONTROL_DESIGN", "AP203"), ("AP210", "AP210"),
                          ("AP209", "AP209"), ("AP238", "AP238"), ("AP239", "AP239")):
        if needle in s:
            return label
    return ""


def _step_time(text):
    """A STEP FILE_NAME time stamp ('2019-05-12T10:11:12', sometimes with an offset or a space) as a
    datetime, or None. Naive means the exporter's local time; the caller applies the install's convention."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        return datetime.datetime.fromisoformat(t)
    except ValueError:  # silent-ok: not ISO 8601 in one piece; the looser pattern below tries next
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", t)
    if not m:
        return None
    try:
        return datetime.datetime(*(int(g) for g in m.groups()))
    except ValueError:  # silent-ok: digits that aren't a real date (month 13, day 0) = no date, by contract
        return None


def _scan_step(path):
    """The facts of a STEP file's HEADER section (stats dict, plus the datetime found)."""
    with open(path, "rb") as f:
        raw = f.read(STEP_HEAD_BYTES)
    text = raw.decode("utf-8-sig", errors="replace")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    info = {"format": "STEP (ISO 10303-21)"}
    m = re.search(r"HEADER\s*;(.*?)ENDSEC\s*;", text, re.S | re.I)
    if not m:
        info["header_problem"] = ("no readable HEADER section in the first "
                                  f"{STEP_HEAD_BYTES // 1024} KB of the file")
        info["head"] = _head_lines(text)
        return info, None
    body = m.group(1)
    info["head"] = _head_lines("HEADER;\n" + body.strip())
    ents, problem = parse_step_header(body)
    fname = ents.get("FILE_NAME")
    stamp = None
    if isinstance(fname, list):
        fields = fname + [""] * (7 - len(fname))
        info["name_in_file"] = _clean(fields[0])
        info["time_stamp"] = _clean(fields[1])
        info["author"] = _clean(fields[2])
        info["organization"] = _clean(fields[3])
        info["preprocessor"] = _clean(fields[4])
        info["program"] = _clean(fields[5])
        stamp = _step_time(info["time_stamp"])
    desc = ents.get("FILE_DESCRIPTION")
    if isinstance(desc, list) and desc:
        info["description"] = _clean(desc[0])
        if len(desc) > 1:
            info["implementation_level"] = _clean(desc[1], 40)
    schema = ents.get("FILE_SCHEMA")
    if isinstance(schema, list) and schema:
        first = _clean(schema[0] if isinstance(schema[0], list) else schema, 120)
        info["schema"] = first.split(" {")[0].strip()
        label = _schema_label(first)
        if label:
            info["application_protocol"] = label
    if not any(info.get(k) for k in ("name_in_file", "author", "program", "organization", "description", "schema")):
        info["header_problem"] = problem or "the HEADER section is there but holds none of the standard entries"
    elif problem:
        info["header_problem"] = f"part of the HEADER could not be read: {problem}"
    return {k: v for k, v in info.items() if v not in ("", None)}, stamp


def _head_lines(text, count=HEAD_LINES):
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    lines = [ln[:LINE_CHARS] for ln in lines if ln]
    return lines[:count]


# ---------------------------------------------------------------- 3MF and F3D (zip containers)

_MODEL_META = re.compile(r"<(?:\w+:)?metadata\b[^>]*?\bname\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</(?:\w+:)?metadata>",
                         re.S | re.I)
_MODEL_UNIT = re.compile(r"<model\b[^>]*?\bunit\s*=\s*[\"']([A-Za-z]+)[\"']", re.I)
_REL = re.compile(r"<Relationship\b[^>]*>", re.I)
_THUMB_NAMES = ("metadata/thumbnail.png", "metadata/thumbnail.jpg", "metadata/thumbnail.jpeg",
                "metadata/plate_1.png", "metadata/top_1.png")
_XML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}


def _xml_text(text):
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    for k, v in _XML_ENTITIES.items():
        text = text.replace(k, v)
    return " ".join(text.split())


def _is_image_name(name):
    return name.lower().endswith((".png", ".jpg", ".jpeg"))


def _read_member(zf, info, cap):
    """The bytes of a zip member, bounded: refuses a member declared bigger than `cap` (None)."""
    if info.file_size > cap:
        return None
    with zf.open(info) as fh:
        data = fh.read(cap + 1)
    return data if len(data) <= cap else None


def _find_thumbnail(zf, by_lower):
    """The zip member that is the package's own picture, or None. 3MF: the thumbnail relationship in
    _rels/.rels, then the usual names; any zip: a member named like a thumbnail or preview image."""
    rels = by_lower.get("_rels/.rels")
    if rels is not None:
        data = _read_member(zf, rels, 256 * 1024)
        for tag in _REL.findall((data or b"").decode("utf-8", errors="replace")):
            if "thumbnail" in tag.lower():
                m = re.search(r"Target\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
                if m:
                    hit = by_lower.get(m.group(1).lstrip("/").lower())
                    if hit is not None:
                        return hit.filename
    for name in _THUMB_NAMES:
        if name in by_lower:
            return by_lower[name].filename
    for lower, zi in by_lower.items():
        if _is_image_name(lower) and ("thumbnail" in lower or "preview" in lower):
            return zi.filename
    return None


def _thumbnail_ok(zf, zi):
    data = _read_member(zf, zi, MAX_THUMB_BYTES)
    return data if data and data.startswith(_IMAGE_MAGIC) else None


def _scan_zip(path, info, model_part):
    """Shared by 3MF and F3D: member count / sample, the picture, and (3MF) the model part's metadata."""
    with zipfile.ZipFile(path) as zf:
        members = [zi for zi in zf.infolist() if not zi.is_dir()]
        by_lower = {zi.filename.lstrip("/").lower(): zi for zi in members}
        info["members"] = len(members)
        info["head"] = ([f"{len(members):,} file{'s' if len(members) != 1 else ''} in the package:"]
                        + [zi.filename[:LINE_CHARS] for zi in members[:MAX_MEMBERS_LISTED]]
                        + ([f"... and {len(members) - MAX_MEMBERS_LISTED:,} more"] if len(members) > MAX_MEMBERS_LISTED else []))
        thumb = _find_thumbnail(zf, by_lower)
        if thumb and _thumbnail_ok(zf, zf.getinfo(thumb)) is not None:
            info["thumbnail"] = thumb
        stamp_text = ""
        if model_part:
            part = by_lower.get(model_part) or next((zi for low, zi in by_lower.items() if low.endswith(".model")), None)
            if part is not None:
                with zf.open(part) as fh:
                    head = fh.read(MODEL_HEAD_BYTES).decode("utf-8", errors="replace")
                unit = _MODEL_UNIT.search(head)
                if unit:
                    info["unit"] = unit.group(1).lower()
                meta = {}
                for key, value in _MODEL_META.findall(head):
                    value = _xml_text(value)
                    if value:
                        meta.setdefault(key.strip(), value[:FIELD_CHARS])
                for key, label in (("Title", "title"), ("Designer", "designer"), ("Application", "program"),
                                   ("Description", "description"), ("Copyright", "copyright"),
                                   ("LicenseTerms", "license"), ("CreationDate", "created")):
                    if meta.get(key):
                        info[label] = meta[key]
                stamp_text = meta.get("CreationDate", "")
    return info, _step_time(stamp_text) if stamp_text else None


# ---------------------------------------------------------------- OBJ (Wavefront)

_OBJ_KEYWORDS = frozenset({"v", "vt", "vn", "vp", "f", "l", "p", "o", "g", "s", "mtllib", "usemtl", "cstype",
                           "deg", "bevel", "c_interp", "d_interp", "lod", "shadow_obj", "trace_obj", "maplib",
                           "usemap", "curv", "curv2", "surf", "parm", "trim", "hole", "scrv", "sp", "end",
                           "con", "g", "mg", "bmat", "step", "ctech", "stech", "vertex"})
_OBJ_PROGRAMS = re.compile(r"(Blender|Wavefront|3ds Max|Maya|MeshLab|ZBrush|Cinema ?4D|SketchUp|Rhino|FreeCAD|"
                           r"Fusion|Tinkercad|Thingiverse|OpenSCAD|Meshmixer|Sculptris|Houdini|Substance|"
                           r"Assimp|trimesh|Open3D)[^\n]{0,60}", re.I)


def _looks_like_obj(head):
    """True for text whose first statement is a Wavefront OBJ keyword (comments skipped). A compiled
    Windows object file is also .obj; it has NUL bytes and no such statement."""
    if b"\x00" in head:
        return False
    for raw in head.splitlines():
        line = raw.strip()
        if not line or line.startswith(b"#"):
            continue
        word = line.split(None, 1)[0].decode("ascii", errors="replace").lower()
        return word in _OBJ_KEYWORDS
    return True  # nothing but comments / blanks: still a text file named .obj


def _scan_obj(path):
    info = {"format": "Wavefront OBJ"}
    counts = {"v": 0, "vt": 0, "vn": 0, "f": 0}
    names, libs, comments = set(), [], []
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    malformed = read = 0
    truncated = False
    with open(path, "rb") as f:
        for raw in f:
            read += len(raw)
            if read > OBJ_MAX_READ:
                truncated = True
                break
            if raw[:1] in (b"\r", b"\n", b" ", b"\t"):
                raw = raw.lstrip()
            if raw[:1] == b"#":
                if len(comments) < 3:
                    text = raw[1:].decode("utf-8", errors="replace").strip()
                    if text:
                        comments.append(text[:160])
                continue
            word = raw.split(None, 1)[0] if raw.strip() else b""
            if word == b"v":
                counts["v"] += 1
                parts = raw.split()
                try:
                    xyz = [float(p) for p in parts[1:4]]
                    if len(xyz) < 3 or not all(math.isfinite(c) for c in xyz):
                        raise ValueError("not three finite numbers")
                except ValueError:
                    malformed += 1
                    continue
                for k in range(3):
                    lo[k] = min(lo[k], xyz[k])
                    hi[k] = max(hi[k], xyz[k])
            elif word in (b"vt", b"vn", b"f"):
                counts[word.decode()] += 1
            elif word in (b"o", b"g"):
                if len(names) < 1000:
                    names.add(raw[1:].decode("utf-8", errors="replace").strip()[:80])
            elif word == b"mtllib" and len(libs) < 5:
                libs.append(raw[6:].decode("utf-8", errors="replace").strip()[:120])
    info.update(vertices=counts["v"], faces=counts["f"], texture_coords=counts["vt"], normals=counts["vn"])
    if names - {""}:
        info["objects"] = len(names - {""})
    if libs:
        info["material_libraries"] = [m for m in libs if m]
    if counts["v"] and all(math.isfinite(c) for c in lo + hi):
        info["size"] = [round(hi[k] - lo[k], 4) for k in range(3)]
    if malformed:
        info["malformed_lines"] = malformed
    if truncated:
        info["truncated_at_bytes"] = OBJ_MAX_READ
    if comments:
        info["header_comments"] = comments
        m = _OBJ_PROGRAMS.search(" ".join(comments))
        if m:
            info["program_guess"] = " ".join(m.group(0).split())
    info["head"] = _head_lines("\n".join(comments)) if comments else []
    return info


# ---------------------------------------------------------------- IGES

def _scan_iges(path):
    """The Start section (records with 'S' in column 73): the author's free-text description."""
    info = {"format": "IGES"}
    start = []
    with open(path, "rb") as f:
        head = f.read(64 * 1024).decode("ascii", errors="replace")
    for line in head.replace("\r", "\n").split("\n"):
        if len(line) >= 73 and line[72] == "S":
            text = line[:72].strip()
            if text:
                start.append(text[:72])
            if len(start) >= 8:
                break
        elif start:
            break
    if start:
        info["start_section"] = " ".join(start)[:FIELD_CHARS]
        info["head"] = start
    else:
        info["note"] = "no IGES Start section found in the first 64 KB"
    return info


# ---------------------------------------------------------------- hooks

def sniff(path, filename):
    """sniff_fn: the contents must be what the extension says, where a wrong kind of file is
    plausible (.step, .3mf, .obj). .f3d and .igs are accepted by extension."""
    ext = _ext(filename)
    if ext in STEP_EXTS:
        with open(path, "rb") as f:
            head = f.read(512).lstrip(b"\xef\xbb\xbf \t\r\n")
        return bool(_STEP_MAGIC.match(head))
    if ext == ".3mf":
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path) as zf:
            return any(n.lstrip("/").lower().endswith(".model") for n in zf.namelist())
    if ext == ".obj":
        with open(path, "rb") as f:
            return _looks_like_obj(f.read(64 * 1024))
    return ext in (".f3d", *IGES_EXTS)


def _scan(path):
    """(stats, datetime or None) for any of the supported files."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in STEP_EXTS:
        return _scan_step(path)
    if ext == ".3mf":
        info = {"format": "3MF (3D Manufacturing Format)"}
        return _scan_zip(path, info, "3d/3dmodel.model")
    if ext == ".f3d":
        info = {"format": "Autodesk Fusion design (.f3d)"}
        if not zipfile.is_zipfile(path):
            info["note"] = "not a zip container, so nothing inside could be read"
            return info, None
        info["container"] = "zip"
        return _scan_zip(path, info, None)[0], None
    if ext == ".obj":
        return _scan_obj(path), None
    if ext in IGES_EXTS:
        return _scan_iges(path), None
    return {}, None


def get_embedded_metadata(path):
    """embedded_metadata_fn: the facts, once, at upload. The STEP time stamp / 3MF CreationDate seeds
    the content date (export-prone: it is when the exporter or slicer ran). Never a title."""
    try:
        info, stamp = _scan(path)
    except Exception as e:  # zip errors, odd encodings, a file that vanished: described, not fatal
        besteffort.warn(log, "cad_source: couldn't read the file's facts (stored without them)", e, path=str(path))
        return {"type_metadata": {STATS_KEY: {"read_problem": f"{type(e).__name__}: {e}"[:200]}}}
    if not info:
        return {}
    out = {"type_metadata": {STATS_KEY: info}}
    if stamp is not None:
        from .. import timeline  # lazy, like image.py's use
        try:
            out["content_date"] = timeline.source_datetime_to_epoch(stamp)
        except Exception as e:
            besteffort.warn(log, "cad_source: couldn't turn the file's time stamp into a date", e, path=str(path))
        else:
            out["content_date_tag"] = "cad_file_time_stamp"
            out["content_date_raw"] = info.get("time_stamp") or info.get("created") or stamp.isoformat()
            out["content_date_export_prone"] = True
    return out


def _stats_for(row):
    return (row.get("type_metadata") or {}).get(STATS_KEY) or {}


def capture_thumbnail(row):
    """capture_fn: the picture the package already carries (a 3MF's, or an F3D member named like one)."""
    path = _stored_path(row)
    name = _stats_for(row).get("thumbnail")
    if not path or not name:
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            return _thumbnail_ok(zf, zf.getinfo(name))
    except Exception as e:
        besteffort.warn(log, "cad_source: couldn't read the package's picture", e, slug=row.get("slug"), member=name)
        return None


def has_thumbnail(row):
    """has_thumbnail_fn: only a package that carried a picture at upload has one."""
    return bool(_stats_for(row).get("thumbnail"))


def _size_label(size, unit):
    dims = " × ".join(f"{v:g}" for v in size)
    return f"{dims} {unit}" if unit else f"{dims} (model units)"


OPEN_WITH = {
    ".step": "FreeCAD, Fusion, SolidWorks, Onshape or any CAD program",
    ".stp": "FreeCAD, Fusion, SolidWorks, Onshape or any CAD program",
    ".igs": "FreeCAD or any CAD program",
    ".iges": "FreeCAD or any CAD program",
    ".f3d": "Autodesk Fusion",
    ".3mf": "any slicer (PrusaSlicer, Bambu Studio, Cura) or the Windows 3D Viewer",
    ".obj": "Blender, MeshLab or any 3D program",
}


def get_properties(row):
    """properties_fn: format the stored facts. {} on failure; never re-reads the file."""
    try:
        s = _stats_for(row)
        ext = _ext(row.get("filename") or row.get("stored_filename"))
        props = {"Format": s.get("format") or "CAD source file"}
        if s.get("application_protocol"):
            props["Format"] += f", {s['application_protocol']}"
        if s.get("read_problem"):
            props["Could not read"] = s["read_problem"]
        if s.get("header_problem"):
            props["Header"] = s["header_problem"]
        for label, key in (("Name in file", "name_in_file"), ("Title", "title"), ("Author", "author"),
                           ("Designer", "designer"), ("Organization", "organization"),
                           ("Program", "program"), ("Exporter", "preprocessor"),
                           ("Date written", "time_stamp"), ("Created", "created"),
                           ("Description", "description"), ("Schema", "schema"),
                           ("Unit", "unit"), ("Copyright", "copyright"), ("License", "license"),
                           ("Start section", "start_section"), ("Container", "container"),
                           ("Note", "note")):
            if s.get(key):
                props[label] = str(s[key])
        if s.get("program_guess") and "Program" not in props:
            props["Program (guess from header comment)"] = s["program_guess"]
        if s.get("members") is not None:
            props["Files in package"] = f"{s['members']:,}"
        if s.get("vertices") is not None:
            props["Vertices"] = f"{s['vertices']:,}"
            props["Faces"] = f"{s.get('faces', 0):,}"
            if s.get("normals"):
                props["Normals"] = f"{s['normals']:,}"
            if s.get("texture_coords"):
                props["Texture coordinates"] = f"{s['texture_coords']:,}"
            if s.get("objects"):
                props["Objects / groups"] = f"{s['objects']:,}"
            if s.get("material_libraries"):
                props["Material library"] = ", ".join(s["material_libraries"])
            if s.get("size"):
                props["Size"] = _size_label(s["size"], "")
            if s.get("malformed_lines"):
                props["Malformed vertex lines"] = f"{s['malformed_lines']:,}"
            if s.get("truncated_at_bytes"):
                props["Read"] = f"only the first {s['truncated_at_bytes'] // (1024 * 1024)} MB was counted"
        if ext == ".f3d":
            props["Contents"] = "only the zip container was read; the design itself opens in Fusion"
        if ext in OPEN_WITH:
            props["Open with"] = OPEN_WITH[ext]
        return props
    except Exception as e:
        besteffort.warn(log, "cad_source: properties failed (none shown)", e, slug=(row or {}).get("slug"))
        return {}


def extract_text_for_row(row):
    """text_extract_fn: make the file findable by the facts inside it (program, author, part name...),
    not the geometry. Always non-empty (the file name and kind), so OCR never reads the picture."""
    try:
        s = _stats_for(row)
        parts = [row.get("filename") or "", "CAD source", s.get("format") or ""]
        for key in ("name_in_file", "title", "author", "designer", "organization", "program", "preprocessor",
                    "program_guess", "description", "start_section", "schema"):
            if s.get(key):
                parts.append(str(s[key]))
        return " ".join(p for p in parts if p)
    except Exception as e:
        besteffort.warn(log, "cad_source: extract_text_for_row failed (no searchable text)", e, slug=(row or {}).get("slug"))
        return ""


def preview(ctx):
    """preview_fn: the package's own picture when it has one; otherwise the first lines of what was
    read (the STEP header, the OBJ's comments, the package's file list), escaped monospace text."""
    if ctx.thumb_url:
        return _preview.thumb_with_original_link(ctx)
    head = (ctx.item.get("type_metadata") or {}).get(STATS_KEY, {}).get("head") or []
    if not head:
        return _preview.file_icon(ctx)
    shown = [str(h)[:LINE_CHARS] for h in head[:HEAD_LINES]]
    return Markup('<pre class="ocr-text mono" style="max-height:70vh;overflow:auto">'
                  f'{escape(chr(10).join(shown))}</pre>')


register(ObjectTypeSpec(
    key="cad_source",
    label="CAD source",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,  # lets text_extract_fn run (the header facts); a non-empty result means no picture OCR
    caption_capable=False,
    caption_excuse="CAD source files aren't captioned; their header facts are searchable instead",
    metadata_fields=(MetadataField(STATS_KEY, "CAD file facts (read once, at upload)"),),
    extensions=EXTENSIONS,
    sniff_fn=sniff,
    content_noun={".step": "STEP (ISO 10303-21) file", ".stp": "STEP (ISO 10303-21) file",
                  ".3mf": "3MF package", ".obj": "Wavefront OBJ model",
                  ".f3d": "Fusion design file", ".igs": "IGES file", ".iges": "IGES file"},
    capture_fn=capture_thumbnail,
    has_thumbnail_fn=has_thumbnail,
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,
    preview_fn=preview,
    badge_icon="\U0001F529",  # nut and bolt
    badge_text="CAD",
))
