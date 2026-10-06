"""macOS application support (issue #475): a zipped .app bundle.

A .app is a folder, so it can only arrive zipped: either packed by the
browser on drop (web/static/js/ingest.js zips bundle folders into
"Foo.app.zip") or compressed in Finder ("Foo.app.zip", same layout). This
type claims a .zip only when its contents ARE an app ("X.app/Contents/
Info.plist" at the top, or one folder down); every other zip stays an
ordinary archive (core/object_types/archive.py, the .zip fallback).

What it shows, read once at upload (embedded_metadata_fn, the STL #449
pattern) without unpacking anything to disk:
- Info.plist: name, bundle ID, version and build, minimum macOS, App Store
  category, copyright;
- the main executable's Mach-O header: Apple Silicon, Intel, or Universal
  (and the odd PowerPC relic);
- whether the bundle carries a code signature (Contents/_CodeSignature);
- the app's own icon (.icns) as the thumbnail, via Pillow.

The archived zip is a record of the app, not necessarily a runnable copy:
a browser-packed zip can't preserve Unix permissions or symlinks.
"""

import io
import plistlib
import re
import struct
import zipfile
from datetime import datetime
from pathlib import Path

from .. import storage
from . import _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "macos_app_stats"
MAX_MEMBER_BYTES = 20 * 1024 * 1024  # Info.plist / .icns read into memory
_INFO = re.compile(r"^((?:[^/]+/)?[^/]+\.app)/Contents/Info\.plist$")
_CPU = {
    0x01000007: "x86_64", 0x00000007: "i386",
    0x0100000C: "arm64", 0x0000000C: "arm",
    0x00000012: "ppc", 0x01000012: "ppc64",
}
_CATEGORY = re.compile(r"^public\.app-category\.")


def _app_root(names):
    """'Foo.app' (or 'Folder/Foo.app') if the zip holds an app, else None.
    Prefers the shallowest match and ignores Finder's __MACOSX shadow tree."""
    roots = [m.group(1) for n in names if not n.startswith("__MACOSX/") for m in [_INFO.match(n)] if m]
    return min(roots, key=lambda r: r.count("/")) if roots else None


def sniff(path, filename):
    """sniff_fn (.zip): only zips whose contents are a macOS app."""
    with open(path, "rb") as f:
        if f.read(4) != b"PK\x03\x04":
            return False
    try:
        with zipfile.ZipFile(path) as z:
            return _app_root(z.namelist()) is not None
    except zipfile.BadZipFile:  # silent-ok: not a zip = not a macOS app; this is a sniff, False is the answer
        return False


def _read(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"{name} too large")
    return z.read(name)


def _architectures(z, exe):
    """Mach-O header of the main executable -> ['arm64', 'x86_64', ...]."""
    with z.open(exe) as f:
        head = f.read(4096)
    magic = head[:4]
    if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):  # fat (universal), big-endian
        count = struct.unpack(">I", head[4:8])[0]
        size = 20 if magic == b"\xca\xfe\xba\xbe" else 32
        return [_CPU.get(struct.unpack(">I", head[8 + i * size:12 + i * size])[0], "unknown")
                for i in range(min(count, 16))]
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):  # thin, little-endian 64/32-bit
        return [_CPU.get(struct.unpack("<I", head[4:8])[0], "unknown")]
    return []


def _arch_label(arches):
    s = set(arches)
    if {"arm64", "x86_64"} <= s:
        return "Universal (Apple Silicon + Intel)"
    if s == {"arm64"}:
        return "Apple Silicon"
    if s <= {"x86_64", "i386"} and s:
        return "Intel" + (" (32-bit)" if s == {"i386"} else "")
    if s & {"ppc", "ppc64"}:
        return "PowerPC" + (" + Intel" if s & {"i386", "x86_64"} else "")
    return ", ".join(sorted(s)) if s else ""


def _icon_name(root, plist, names):
    """Zip member name of the app's .icns icon, or None."""
    res = f"{root}/Contents/Resources/"
    candidates = []
    if plist.get("CFBundleIconFile"):
        icon = plist["CFBundleIconFile"]
        candidates += [res + icon, res + icon + ".icns"]
    candidates += [res + "AppIcon.icns", res + "icon.icns"]
    for c in candidates:
        if c in names:
            return c
    return next((n for n in names if n.startswith(res) and n.count("/") == res.count("/")
                 and n.lower().endswith(".icns")), None)


def _scan(path):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        root = _app_root(names)
        if not root:
            return {}
        info_name = f"{root}/Contents/Info.plist"
        plist = plistlib.loads(_read(z, info_name))
        stats = {"format": "macOS application", "bundle": Path(root).name}
        for key, field in (("CFBundleDisplayName", "name"), ("CFBundleName", "name"),
                           ("CFBundleIdentifier", "bundle_id"),
                           ("CFBundleShortVersionString", "version"), ("CFBundleVersion", "build"),
                           ("LSMinimumSystemVersion", "min_macos"),
                           ("LSApplicationCategoryType", "category"),
                           ("NSHumanReadableCopyright", "copyright")):
            value = plist.get(key)
            if isinstance(value, str) and value.strip() and field not in stats:
                stats[field] = value.strip()
        exe = plist.get("CFBundleExecutable")
        exe_name = f"{root}/Contents/MacOS/{exe}" if exe else None
        if exe_name and exe_name in names:
            stats["architectures"] = _architectures(z, exe_name)
        stats["signed"] = any(n.startswith(f"{root}/Contents/_CodeSignature/") for n in names)
        stats["icon"] = _icon_name(root, plist, names)
        members = [i for i in z.infolist() if i.filename.startswith(root + "/") and not i.is_dir()]
        stats["files"] = len(members)
        stats["unpacked_bytes"] = sum(i.file_size for i in members)
        y, mo, d, h, mi, s = z.getinfo(info_name).date_time
        stats["info_plist_date"] = [y, mo, d, h, mi, s]
    return stats


def get_embedded_metadata(path):
    """embedded_metadata_fn: Info.plist + Mach-O facts, once at upload. The
    Info.plist's timestamp in the zip (its modified time on the Mac it came
    from) seeds content_date when plausible."""
    try:
        stats = _scan(Path(path))
    except Exception as e:
        print(f"macOS app scan failed for {path}: {e!r}")
        return {}
    if not stats:
        return {}
    out = {"type_metadata": {STATS_KEY: stats}}
    if stats.get("name"):
        out["content_description"] = f"{stats['name']} (macOS app)"
    try:
        y = stats["info_plist_date"][0]
        if 1995 <= y <= datetime.now().year + 1:
            from ..timeline import source_datetime_to_epoch
            out["content_date"] = source_datetime_to_epoch(datetime(*stats["info_plist_date"]))
    except Exception as e:
        print(f"macOS app date skipped for {path}: {e!r}")
    return out


def _stored_path(row):
    stored = row.get("stored_filename")
    if not stored:
        return None
    p = storage.path_for(stored)
    return p if p.exists() else None


def has_thumbnail(row):
    """has_thumbnail_fn: only apps whose bundle carries an .icns icon."""
    return bool(((row.get("type_metadata") or {}).get(STATS_KEY) or {}).get("icon"))


def capture_thumbnail(row):
    """capture_fn: the app's own icon, largest size Pillow can read, as PNG."""
    path = _stored_path(row)
    if not path:
        return None
    try:
        from PIL import Image
        stats = ((row.get("type_metadata") or {}).get(STATS_KEY)) or _scan(path)
        if not stats.get("icon"):
            return None
        with zipfile.ZipFile(path) as z:
            data = _read(z, stats["icon"])
        im = Image.open(io.BytesIO(data))
        sizes = im.info.get("sizes")  # IcnsImagePlugin: {(w, h, scale), ...}
        if sizes:
            w, h, scale = max(sizes, key=lambda t: t[0] * t[2])  # most pixels (Retina @2x counts)
            im.size = (w, h)
            im.load(scale=scale)
        else:
            im.load()
        out = io.BytesIO()
        im.convert("RGBA").save(out, "PNG")
        return out.getvalue()
    except Exception as e:
        print(f"macOS app icon extraction failed for {row.get('slug')}: {e!r}")
        return None


def _size_label(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,} bytes" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024


def get_properties(row):
    """properties_fn: format the stored facts (re-scanning only for a row
    that predates the hook). {} on failure."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            path = _stored_path(row)
            stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY) if path else None
        if not stats:
            return {}
        props = {"Format": "macOS application"}
        if stats.get("name"):
            props["Name"] = stats["name"]
        if stats.get("version"):
            build = stats.get("build")
            props["Version"] = stats["version"] + (f" ({build})" if build and build != stats["version"] else "")
        if stats.get("bundle_id"):
            props["Bundle ID"] = stats["bundle_id"]
        arch = _arch_label(stats.get("architectures") or [])
        if arch:
            props["Runs on"] = arch
        if stats.get("min_macos"):
            props["Minimum macOS"] = stats["min_macos"]
        if stats.get("category"):
            props["Category"] = _CATEGORY.sub("", stats["category"]).replace("-", " ").title()
        props["Signed"] = "Yes" if stats.get("signed") else "No"
        if stats.get("files"):
            props["Contents"] = f"{stats['files']:,} files, {_size_label(stats.get('unpacked_bytes', 0))} unpacked"
        if stats.get("copyright"):
            props["Copyright"] = stats["copyright"]
        return props
    except Exception as e:
        print(f"macOS app properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """preview_fn: the app icon (thumbnail) + a link to the zip."""
    return _preview.thumb_with_original_link(ctx) if ctx.thumb_url else None


register(ObjectTypeSpec(
    key="macos_app",
    label="macOS application",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,
    caption_capable=False,
    extensions=frozenset({".zip"}),
    sniff_fn=sniff,  # claims only app zips; other .zip files fall through to archive
    sniff_priority=10,
    capture_fn=capture_thumbnail,
    has_thumbnail_fn=has_thumbnail,  # #478: no icon -> show the type badge
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,
    preview_fn=preview,
    badge_icon="\U0001F34E",  # apple
    badge_text="APP",
))
