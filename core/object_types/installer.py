"""Installer support (issue #444): packages that install software.

Formats: .msi (Windows Installer), .pkg/.mpkg (macOS flat packages, xar),
.xip (Apple signed archive, xar), .deb, .rpm, .msix/.appx (+ bundles).
.exe (#446): only an .exe with a clear installer-framework fingerprint
(NSIS, Inno Setup, InstallShield, WiX Burn, embedded MSI; see _pe.py) is
claimed here, by a sniffer that outranks the application type's. Every
other .exe goes to core/object_types/application.py, which asks the owner.
A "Reclassify" action on .exe rows is the undo either way.

Everything is header/manifest parsing, done once at upload by
embedded_metadata_fn and stored under type_metadata[STATS_KEY] (the STL #449
pattern); properties_fn only formats what was stored, and re-parses the file
only for a row that predates the hook. Nothing is executed or extracted to
disk, and every read is bounded (MAX_MEMBER_BYTES), so a 2 GB upload costs a
few small reads. sniff_fn checks the magic bytes, so a file whose content
doesn't match its extension is refused as unsupported rather than stored
under the wrong type.

Best-effort like every other type: a truncated or odd file yields whatever
could be read ({} at worst) and never raises.
"""

import io
import struct
import tarfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

from .. import storage
from . import _pe, _preview, register, ObjectTypeSpec, ThumbnailSource, TypeAction

STATS_KEY = "installer_stats"
# Cap on any one embedded document/member read into memory (TOC, Distribution,
# control.tar, rpm header store, MSI string/property streams, Appx manifest).
MAX_MEMBER_BYTES = 16 * 1024 * 1024

EXTENSIONS = frozenset({
    ".msi", ".pkg", ".mpkg", ".xip", ".deb", ".rpm",
    ".msix", ".appx", ".msixbundle", ".appxbundle", ".exe",
})
_XAR = {".pkg", ".mpkg", ".xip"}
_APPX = {".msix", ".appx", ".msixbundle", ".appxbundle"}
_APPX_MANIFEST = "AppxManifest.xml"
_APPX_BUNDLE_MANIFEST = "AppxMetadata/AppxBundleManifest.xml"


def _local(tag):
    """XML tag without its {namespace}."""
    return tag.rsplit("}", 1)[-1]


def _children(elem, name):
    return [c for c in elem if _local(c.tag) == name]


def _first(elem, name):
    """First descendant (any depth) with this local name, or None."""
    return next((e for e in elem.iter() if _local(e.tag) == name), None)


def _text(s):
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    return (s or "").strip().strip("\x00").strip()


# ---------------------------------------------------------------- sniff

def sniff(path, filename):
    """sniff_fn: the magic bytes must match the extension."""
    ext = Path(filename).suffix.lower()
    with open(path, "rb") as f:
        magic = f.read(8)
    if ext == ".msi":
        return magic == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    if ext in _XAR:
        return magic[:4] == b"xar!"
    if ext == ".deb":
        return magic == b"!<arch>\n"
    if ext == ".rpm":
        return magic[:4] == b"\xed\xab\xee\xdb"
    if ext == ".exe":
        return _pe.is_pe(path) and _pe.installer_framework(path) is not None
    if ext in _APPX:
        if magic[:4] != b"PK\x03\x04":
            return False
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:  # silent-ok: not a zip = not an APPX; this is a sniff, False is the answer
            return False
        return _APPX_MANIFEST in names or _APPX_BUNDLE_MANIFEST in names
    return False


# ---------------------------------------------------------------- MSI

_MSI_ALPHA = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"


def _msi_stream_name(name):
    """Decode an MSI-compressed OLE stream name (see Wine msi/table.c)."""
    out = []
    for ch in name:
        c = ord(ch)
        if 0x3800 <= c < 0x4800:
            c -= 0x3800
            out.append(_MSI_ALPHA[c & 0x3F])
            out.append(_MSI_ALPHA[(c >> 6) & 0x3F])
        elif 0x4800 <= c < 0x4840:
            out.append(_MSI_ALPHA[c - 0x4800])
        elif c == 0x4840:
            continue  # table-stream marker
        else:
            out.append(ch)
    return "".join(out)


def _msi_strings(pool_data, str_data):
    """(strings by id, string-ref size in bytes) from _StringPool/_StringData
    (port of Wine msi/string.c's string pool loader)."""
    n = len(pool_data) // 2
    pool = struct.unpack(f"<{n}H", pool_data[: n * 2])
    if len(pool) < 2:
        return {}, 2
    codepage = pool[0] | ((pool[1] & ~0x8000) << 16)
    refsize = 3 if pool[1] & 0x8000 else 2
    encoding = "utf-8" if codepage == 65001 else ("cp1252" if codepage == 0 else f"cp{codepage}")
    try:
        "".encode(encoding)
    except LookupError:  # silent-ok: unknown codepage, documented fallback to cp1252
        encoding = "cp1252"

    strings, sid, off, i = {}, 1, 0, 1
    while i * 2 + 1 < len(pool):
        length, refs = pool[i * 2], pool[i * 2 + 1]
        if length == 0 and refs == 0:
            i += 1
            sid += 1
            continue
        if length == 0:  # >64K string: real length in the next entry
            if i * 2 + 3 >= len(pool):
                break
            length = (pool[i * 2 + 3] << 16) + pool[i * 2 + 2]
            i += 2
        else:
            i += 1
        strings[sid] = str_data[off:off + length].decode(encoding, errors="replace")
        off += length
        sid += 1
    return strings, refsize


def _parse_msi(path):
    import olefile

    info = {"format": "Windows Installer (MSI)"}
    ole = olefile.OleFileIO(str(path))
    try:
        info["signed"] = ole.exists("\x05DigitalSignature")

        # SummaryInformation: fallbacks for name/publisher, and the platform.
        try:
            meta = ole.get_metadata()
            if _text(meta.subject):
                info["name"] = _text(meta.subject)
            if _text(meta.author):
                info["publisher"] = _text(meta.author)
            platform = _text(meta.template).split(";", 1)[0].strip()
            if platform:
                info["architecture"] = {"intel": "x86", "intel64": "ia64"}.get(platform.lower(), platform)
        except Exception as e:
            print(f"MSI summary info unreadable for {path}: {e!r}")

        # Property table: the authoritative ProductName/Version/Manufacturer/Code.
        streams = {}
        for entry in ole.listdir(streams=True, storages=False):
            if len(entry) == 1:
                streams[_msi_stream_name(entry[0])] = entry
        needed = ("_StringPool", "_StringData", "Property")
        if all(k in streams for k in needed) and all(
            ole.get_size(streams[k]) <= MAX_MEMBER_BYTES for k in needed
        ):
            strings, refsize = _msi_strings(
                ole.openstream(streams["_StringPool"]).read(),
                ole.openstream(streams["_StringData"]).read(),
            )
            table = ole.openstream(streams["Property"]).read()
            rows = len(table) // (2 * refsize)

            def ref(idx):
                b = table[idx * refsize:(idx + 1) * refsize]
                return int.from_bytes(b, "little")

            props = {}
            for r in range(rows):
                props[strings.get(ref(r), "")] = strings.get(ref(rows + r), "")
            for key, field in (("ProductName", "name"), ("ProductVersion", "version"),
                               ("Manufacturer", "publisher"), ("ProductCode", "identifier"),
                               ("UpgradeCode", "upgrade_code")):
                if _text(props.get(key)):
                    info[field] = _text(props[key])
    finally:
        ole.close()
    return info


# ---------------------------------------------------------------- xar (.pkg/.mpkg/.xip)

def _xar_toc(f):
    """(toc root element, heap start offset) of an open xar file."""
    header = f.read(28)
    magic, header_size = struct.unpack(">4sH", header[:6])
    toc_len_c, _toc_len_u = struct.unpack(">QQ", header[8:24])
    if magic != b"xar!" or toc_len_c > MAX_MEMBER_BYTES:
        raise ValueError("not a xar archive, or TOC too large")
    f.seek(header_size)
    root = ET.fromstring(zlib.decompress(f.read(toc_len_c)))
    return root, header_size + toc_len_c


def _xar_read(f, heap, file_elem):
    """Bytes of one xar TOC <file>'s data, decoded; None when unreadable."""
    data = _first(file_elem, "data")
    if data is None:
        return None
    offset = int(_first(data, "offset").text)
    length = int(_first(data, "length").text)
    if length > MAX_MEMBER_BYTES:
        return None
    f.seek(heap + offset)
    raw = f.read(length)
    enc = _first(data, "encoding")
    style = (enc.get("style") if enc is not None else "") or ""
    if "gzip" in style:
        return zlib.decompress(raw)
    if "bzip2" in style:
        import bz2
        return bz2.decompress(raw)
    return raw


def _parse_distribution(xml_bytes, info):
    root = ET.fromstring(xml_bytes)
    title = _first(root, "title")
    if title is not None and _text(title.text):
        info["name"] = _text(title.text)
    product = _first(root, "product")
    if product is not None:
        if product.get("id"):
            info["identifier"] = product.get("id")
        if product.get("version"):
            info["version"] = product.get("version")
    options = _first(root, "options")
    if options is not None and options.get("hostArchitectures"):
        info["architecture"] = options.get("hostArchitectures")
    os_version = _first(root, "os-version")
    if os_version is not None and os_version.get("min"):
        info["min_os"] = f"macOS {os_version.get('min')}"
    refs = [e for e in root.iter() if _local(e.tag) == "pkg-ref" and e.get("version")]
    if refs:
        info["components"] = len(refs)
        info.setdefault("identifier", refs[0].get("id"))
        info.setdefault("version", refs[0].get("version"))


def _parse_xar(path, ext):
    info = {"format": {".mpkg": "macOS metapackage", ".xip": "Apple signed archive (XIP)"}.get(
        ext, "macOS installer package")}
    with open(path, "rb") as f:
        root, heap = _xar_toc(f)
        toc = _first(root, "toc")
        if toc is None:
            return info
        info["signed"] = bool(_children(toc, "signature") or _children(toc, "x-signature"))
        top = {}
        for fe in _children(toc, "file"):
            name = _first(fe, "name")
            if name is not None and name.text:
                top[name.text] = fe
        if ext == ".xip":
            info["components"] = len(top)
            return info
        if "Distribution" in top:
            body = _xar_read(f, heap, top["Distribution"])
            if body:
                _parse_distribution(body, info)
        elif "PackageInfo" in top:
            body = _xar_read(f, heap, top["PackageInfo"])
            if body:
                pi = ET.fromstring(body)
                if pi.get("identifier"):
                    info["identifier"] = pi.get("identifier")
                if pi.get("version"):
                    info["version"] = pi.get("version")
    return info


# ---------------------------------------------------------------- deb

def _ar_members(f):
    """Yield (name, size, data_offset) for each member of an open ar file."""
    f.seek(8)
    while True:
        header = f.read(60)
        if len(header) < 60:
            return
        name = header[:16].decode("latin1").strip().rstrip("/")
        size = int(header[48:58].decode("latin1").strip())
        start = f.tell()
        yield name, size, start
        f.seek(start + size + (size % 2))


def _parse_deb(path):
    info = {"format": "Debian package", "signed": False}
    control_tar = None
    with open(path, "rb") as f:
        for name, size, start in list(_ar_members(f)):
            if name.startswith("_gpg"):
                info["signed"] = True
            elif name.startswith("control.tar") and control_tar is None and size <= MAX_MEMBER_BYTES:
                f.seek(start)
                control_tar = f.read(size)
    if not control_tar:
        return info
    try:
        tar = tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:*")
    except tarfile.ReadError:
        tar = tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:zst")  # control.tar.zst (Python 3.14+)
    with tar:
        member = next((m for m in tar.getmembers() if m.name in ("control", "./control")), None)
        if member is None or member.size > MAX_MEMBER_BYTES:
            return info
        text = tar.extractfile(member).read().decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue  # continuation lines (long Description) are skipped
        key, value = (s.strip() for s in line.split(":", 1))
        if key == "Package":
            info["name"] = value
        elif key == "Version":
            info["version"] = value
        elif key == "Architecture":
            info["architecture"] = value
        elif key == "Maintainer":
            info["publisher"] = value
        elif key == "Description":
            info["description"] = value
        elif key == "Installed-Size" and value.isdigit():
            info["installed_size_kib"] = int(value)
    return info


# ---------------------------------------------------------------- rpm

def _rpm_header(f):
    """(entries {tag: (type, offset, count)}, store bytes) of the header at f."""
    if f.read(4) != b"\x8e\xad\xe8\x01":
        raise ValueError("bad rpm header magic")
    f.read(4)  # reserved
    count, size = struct.unpack(">II", f.read(8))
    if count > 100_000 or size > MAX_MEMBER_BYTES:
        raise ValueError("rpm header too large")
    entries = {}
    for _ in range(count):
        tag, dtype, offset, n = struct.unpack(">IIII", f.read(16))
        entries[tag] = (dtype, offset, n)
    return entries, f.read(size)


def _rpm_value(entries, store, tag):
    if tag not in entries:
        return None
    dtype, offset, _count = entries[tag]
    if dtype == 4:  # INT32
        return struct.unpack(">I", store[offset:offset + 4])[0]
    if dtype in (6, 8, 9):  # STRING, STRING_ARRAY, I18NSTRING: first string
        end = store.find(b"\x00", offset)
        return store[offset:end if end != -1 else len(store)].decode("utf-8", errors="replace")
    return None


def _parse_rpm(path):
    info = {"format": "RPM package"}
    with open(path, "rb") as f:
        if f.read(96)[:4] != b"\xed\xab\xee\xdb":
            return {}
        sig, _ = _rpm_header(f)
        info["signed"] = any(t in sig for t in (267, 268, 1002, 1005))
        pos = f.tell()
        f.seek(pos + (-pos % 8))  # signature header is padded to 8 bytes
        entries, store = _rpm_header(f)

    def val(tag):
        return _rpm_value(entries, store, tag)

    if val(1000):
        info["name"] = val(1000)
    if val(1001):
        info["version"] = f"{val(1001)}-{val(1002)}" if val(1002) else val(1001)
    if val(1004):
        info["description"] = val(1004)
    publisher = val(1011) or val(1015)
    if publisher:
        info["publisher"] = publisher
    if val(1022):
        info["architecture"] = val(1022)
    if val(1021):
        info["os"] = val(1021)
    if isinstance(val(1009), int):
        info["installed_size_kib"] = val(1009) // 1024
    return info


# ---------------------------------------------------------------- msix/appx

def _parse_appx(path):
    info = {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        info["signed"] = "AppxSignature.p7x" in names
        is_bundle = _APPX_BUNDLE_MANIFEST in names
        manifest = _APPX_BUNDLE_MANIFEST if is_bundle else _APPX_MANIFEST
        if z.getinfo(manifest).file_size > MAX_MEMBER_BYTES:
            return info
        root = ET.fromstring(z.read(manifest))

    identity = _first(root, "Identity")
    if identity is not None:
        for attr, field in (("Name", "identifier"), ("Version", "version"), ("Publisher", "publisher")):
            if identity.get(attr):
                info[field] = identity.get(attr)

    if is_bundle:
        info["format"] = "MSIX bundle"
        packages = [e for e in root.iter() if _local(e.tag) == "Package"]
        arches = sorted({p.get("Architecture") for p in packages if p.get("Architecture")})
        if arches:
            info["architecture"] = ", ".join(arches)
        if packages:
            info["components"] = len(packages)
        return info

    info["format"] = "MSIX package"
    if identity is not None and identity.get("ProcessorArchitecture"):
        info["architecture"] = identity.get("ProcessorArchitecture")
    props = _first(root, "Properties")
    if props is not None:
        for tag, field in (("DisplayName", "name"), ("PublisherDisplayName", "publisher")):
            el = next(iter(_children(props, tag)), None)
            if el is not None and _text(el.text) and not el.text.strip().startswith("ms-resource:"):
                info[field] = _text(el.text)
    family = _first(root, "TargetDeviceFamily")
    if family is not None and family.get("MinVersion"):
        info["min_os"] = f"{family.get('Name', '')} {family.get('MinVersion')}".strip()
    return info


# ---------------------------------------------------------------- hooks

def _parse_exe(path):
    """An installer .exe: the PE facts plus which framework built it. (An
    .exe the owner retyped to installer may have no fingerprint: plain
    "Windows installer".)"""
    info = _pe.facts(path)
    framework = _pe.installer_framework(path)
    info["format"] = f"Windows installer ({framework})" if framework else "Windows installer"
    for app_only in ("dotnet", "subsystem", "built", "original_filename", "copyright"):
        info.pop(app_only, None)
    return info


def _parse(path):
    ext = Path(path).suffix.lower()
    if ext == ".exe":
        return _parse_exe(path)
    if ext == ".msi":
        return _parse_msi(path)
    if ext in _XAR:
        return _parse_xar(path, ext)
    if ext == ".deb":
        return _parse_deb(path)
    if ext == ".rpm":
        return _parse_rpm(path)
    if ext in _APPX:
        return _parse_appx(path)
    return {}


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn: parse once at upload, store the
    result under type_metadata[STATS_KEY]. {} on any failure."""
    try:
        info = _parse(path)
    except Exception as e:
        print(f"Installer metadata extraction failed for {path}: {e!r}")
        return {}
    return {"type_metadata": {STATS_KEY: info}} if info else {}


def _humanize_kib(kib):
    if kib < 1024:
        return f"{kib:,} KB"
    if kib < 1024 * 1024:
        return f"{kib / 1024:.1f} MB"
    return f"{kib / (1024 * 1024):.1f} GB"


def get_properties(row):
    """ObjectTypeSpec.properties_fn: format the stored stats (re-parsing the
    file only for a row that predates the hook). {} on any failure."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats and row.get("stored_filename"):
            path = storage.path_for(row["stored_filename"])
            if path.exists():
                stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            return {}
        props = {}
        for key, label in (("format", "Format"), ("name", "Name"), ("version", "Version"),
                           ("publisher", "Publisher"), ("identifier", "Identifier"),
                           ("architecture", "Architecture"), ("min_os", "Min OS")):
            if stats.get(key):
                props[label] = str(stats[key])
        if stats.get("signer") and not stats.get("publisher"):
            props["Signed by"] = str(stats["signer"])  # #587 item 4: a signed .exe with no CompanyName
        if stats.get("components"):
            props["Components"] = f"{stats['components']:,}"
        if stats.get("installed_size_kib"):
            props["Installed size"] = _humanize_kib(stats["installed_size_kib"])
        if "signed" in stats:
            props["Signed"] = "Yes" if stats["signed"] else "No"
        if stats.get("description"):
            props["Description"] = str(stats["description"])
        return props
    except Exception as e:
        print(f"Installer properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """preview_fn: nothing visual to show; the generic file icon (live) or a
    download link (export). The specifics are in the properties line."""
    return _preview.file_icon(ctx)


register(ObjectTypeSpec(
    key="installer",
    label="Installer",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    caption_capable=False,
    extensions=EXTENSIONS,
    sniff_fn=sniff,
    sniff_priority=10,  # #446: for .exe, runs before the application type's plain-PE sniffer
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,  # parsed once, at upload
    preview_fn=preview,
    actions=(TypeAction(
        "reclassify-application", "Reclassify as standalone app",
        lambda row: _pe.reclassify(row, "application"),
        confirm="File this .exe as a standalone app instead of an installer?",
        applies_fn=_pe.is_exe_row,
    ),),
    badge_icon="\U0001F9F0",  # toolbox
    badge_text="SETUP",
))
