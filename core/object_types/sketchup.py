"""SketchUp model support (issue #473): .skp files.

A .skp carries everything worth showing without SketchUp itself:
- a UTF-16 header, "SketchUp Model {7.1.6859}", naming the version that
  saved it;
- the model's own preview image, an embedded PNG near the start of the file,
  which becomes the thumbnail (CAPTURE, but "capturing" is just copying those
  bytes out: no rendering, no SketchUp);
- the textures the model uses, as embedded PNG/JPEG images (e.g. the
  hooptieJ logo decal inside The Project's ornithopter model).

No 3D view: that would need a real .skp geometry parser. The page says how
to open the file instead (SketchUp Free, in a browser, opens every version).

Facts are read once at upload (embedded_metadata_fn, the STL #449 pattern).
The file is scanned through mmap, so counting a big model's textures never
loads it into memory. Best-effort like every type: {} / None on failure.
"""

import mmap
import re
from pathlib import Path

from .. import storage
from . import _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "sketchup_stats"
_MAGIC = b"\xff\xfe\xff\x0e"
_HEADER = re.compile(rb"S\x00k\x00e\x00t\x00c\x00h\x00U\x00p\x00 \x00M\x00o\x00d\x00e\x00l\x00.{0,4}?\{\x00((?:[0-9.]\x00){1,24})\}\x00", re.S)
_PNG = re.compile(rb"\x89PNG\r\n\x1a\n")
_PNG_END = b"IEND\xaeB`\x82"
_JPEG = re.compile(rb"\xff\xd8\xff[\xe0\xe1\xdb]")
PREVIEW_SEARCH = 64 * 1024   # the preview PNG sits right after the header
MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def _version_label(version):
    """'7.1.6859' -> 'SketchUp 7.1 (build 6859)'; '21.0.392' -> 'SketchUp 2021 (21.0.392)'."""
    parts = version.split(".")
    try:
        major = int(parts[0])
    except ValueError:
        return f"SketchUp {version}"
    if major >= 13:  # SketchUp 2013 onwards number their files 13, 14, ... 2x
        return f"SketchUp 20{major:02d} ({version})"
    build = f" (build {parts[2]})" if len(parts) > 2 else ""
    return f"SketchUp {'.'.join(parts[:2])}{build}"


def sniff(path, filename):
    """sniff_fn: the SketchUp header, not just the extension."""
    with open(path, "rb") as f:
        head = f.read(200)
    return head[:4] == _MAGIC and bool(_HEADER.search(head))


def _preview_png(path):
    """The model's embedded preview PNG (bytes), or None."""
    with open(path, "rb") as f:
        head = f.read(PREVIEW_SEARCH + MAX_PREVIEW_BYTES)
    m = _PNG.search(head, 0, PREVIEW_SEARCH)
    if not m:
        return None
    end = head.find(_PNG_END, m.start())
    return head[m.start():end + len(_PNG_END)] if end != -1 else None


def _scan(path):
    """Version, preview size and embedded-image counts, via mmap."""
    info = {"format": "SketchUp model"}
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        m = _HEADER.search(mm, 0, 400)
        if m:
            info["version"] = m.group(1).decode("utf-16-le")
        pngs, largest = 0, (0, 0)
        for pm in _PNG.finditer(mm):
            pngs += 1
            ihdr = mm[pm.start() + 16:pm.start() + 24]
            if len(ihdr) == 8:
                w, h = int.from_bytes(ihdr[:4], "big"), int.from_bytes(ihdr[4:], "big")
                if pngs == 1:
                    info["preview_size"] = [w, h]  # the first PNG is the model's own preview
                elif w * h > largest[0] * largest[1] and w < 20000 and h < 20000:
                    largest = (w, h)
        info["png_textures"] = max(pngs - 1, 0)
        info["jpeg_textures"] = sum(1 for _ in _JPEG.finditer(mm))
        if largest != (0, 0):
            info["largest_texture"] = list(largest)
    return info


def capture_thumbnail(row):
    """capture_fn: the preview image the model already carries."""
    path = _stored_path(row)
    try:
        return _preview_png(path) if path else None
    except Exception as e:
        print(f"SketchUp preview extraction failed for {path}: {e!r}")
        return None


def get_embedded_metadata(path):
    """embedded_metadata_fn: scan once at upload."""
    try:
        return {"type_metadata": {STATS_KEY: _scan(Path(path))}}
    except Exception as e:
        print(f"SketchUp scan failed for {path}: {e!r}")
        return {}


def get_properties(row):
    """properties_fn: format the stored scan (re-scanning only for a row that
    predates the hook). {} on failure."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            path = _stored_path(row)
            stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY) if path else None
        if not stats:
            return {}
        props = {"Format": stats.get("format", "SketchUp model")}
        if stats.get("version"):
            props["Saved with"] = _version_label(stats["version"])
        textures = []
        if stats.get("png_textures"):
            textures.append(f"{stats['png_textures']:,} PNG")
        if stats.get("jpeg_textures"):
            textures.append(f"{stats['jpeg_textures']:,} JPEG")
        if textures:
            props["Embedded images"] = ", ".join(textures)
        if stats.get("largest_texture"):
            w, h = stats["largest_texture"]
            props["Largest image"] = f"{w:,} × {h:,}"
        props["Open with"] = "SketchUp Free (app.sketchup.com) opens every version"
        return props
    except Exception as e:
        print(f"SketchUp properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """preview_fn: the model's own preview image + a link to the file."""
    return _preview.thumb_with_original_link(ctx) if ctx.thumb_url else None


register(ObjectTypeSpec(
    key="sketchup",
    label="SketchUp model",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,
    caption_capable=False,
    extensions=frozenset({".skp"}),
    sniff_fn=sniff,
    capture_fn=capture_thumbnail,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,
    preview_fn=preview,
    badge_icon="\U0001F4D0",  # triangular ruler
    badge_text="SKP",
))
