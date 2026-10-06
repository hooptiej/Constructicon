"""Disk image support (issue #445): a disk (or disc) in a file.

Formats: .iso (ISO 9660 / UDF), .dmg (Apple UDIF), .img and .cdr (raw
images; .cdr is macOS's "DVD/CD master"), .sparseimage (Apple sparse), and
VM disks .vhd, .vhdx, .vmdk, .qcow2. Deliberately separate from the
installer type (#444): a .dmg that ships an app is still a container.

Never mounts anything: every fact comes from reading a few fixed-offset
headers (a handful of small reads, however big the image), done once at
upload by embedded_metadata_fn and stored under type_metadata[STATS_KEY]
(the STL #449 pattern). An ISO's volume creation date also seeds the row's
content_date, the way a photo's EXIF date does.

sniff_fn checks the magic where a format has one (.iso, .sparseimage, the
VM disks); .dmg, .img and .cdr can legitimately be raw bytes with no
signature, so those are claimed by extension and described as best the
headers allow ("raw, no partition table" at worst).

Best-effort like every other type: never raises, {} at worst.
"""

import base64
import datetime
import plistlib
import struct
import uuid
from pathlib import Path

from .. import storage
from . import _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "disk_image_stats"
MAX_READ = 16 * 1024 * 1024  # cap on any embedded structure read (DMG plist)
SECTOR = 512

EXTENSIONS = frozenset({".iso", ".dmg", ".img", ".cdr", ".sparseimage",
                        ".vhd", ".vhdx", ".vmdk", ".qcow2"})


def _read(f, offset, size):
    f.seek(offset)
    return f.read(size)


def _size_label(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,} bytes" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024


# ---------------------------------------------------------------- sniff

def _is_iso(f):
    """ISO 9660 ("CD001") or UDF ("BEA01"/"NSR0x") volume recognition at sector 16."""
    return _read(f, 0x8001, 5) in (b"CD001", b"BEA01", b"NSR02", b"NSR03")


def sniff(path, filename):
    ext = Path(filename).suffix.lower()
    with open(path, "rb") as f:
        head = f.read(8)
        if ext == ".iso":
            return _is_iso(f)
        if ext == ".sparseimage":
            return head[:4] == b"sprs"
        if ext == ".vhdx":
            return head == b"vhdxfile"
        if ext == ".qcow2":
            return head[:4] == b"QFI\xfb"
        if ext == ".vmdk":
            return head[:4] == b"KDMV" or head.startswith(b"# Disk D") or head.startswith(b"# Disk\tD")
        if ext == ".vhd":
            return _vhd_footer(f) is not None
    return ext in (".dmg", ".img", ".cdr")  # raw images have no magic


# ---------------------------------------------------------------- ISO 9660 / UDF

def _iso_date(b):
    """17-byte ISO 9660 dec-datetime -> UTC epoch float, or None."""
    try:
        digits = b[:16].decode("ascii")
        if not digits.strip("0 "):
            return None
        dt = datetime.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        offset = struct.unpack("b", b[16:17])[0] * 15  # minutes east of UTC
        dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(minutes=offset)))
        return dt.timestamp()
    except (ValueError, UnicodeDecodeError, struct.error):  # silent-ok: a malformed ISO date field = no date
        return None


def _parse_iso(f, info):
    """Walk the volume descriptor set from sector 16."""
    fmt = []
    for n in range(16, 64):
        vd = _read(f, n * 2048, 2048)
        if len(vd) < 2048:
            break
        vtype, ident = vd[0], vd[1:6]
        if ident in (b"BEA01", b"NSR02", b"NSR03", b"TEA01"):
            if ident.startswith(b"NSR") and "UDF" not in fmt:
                fmt.append("UDF")
            continue
        if ident != b"CD001":
            break
        if vtype == 255:
            continue  # ISO set terminator; a UDF bridge's descriptors may follow
        if vtype == 0 and vd[7:30].startswith(b"EL TORITO SPECIFICATION"):
            info["bootable"] = True
        elif vtype == 1:
            fmt.insert(0, "ISO 9660")
            label = vd[40:72].decode("ascii", errors="replace").strip()
            if label:
                info["volume_label"] = label
            for key, lo, hi in (("publisher", 318, 446), ("application", 574, 702)):
                val = vd[lo:hi].decode("ascii", errors="replace").strip()
                if val and not val.startswith("_"):
                    info[key] = val
            blocks = struct.unpack("<I", vd[80:84])[0]
            block_size = struct.unpack("<H", vd[128:130])[0]
            info["declared_bytes"] = blocks * block_size
            created = _iso_date(vd[813:830])
            if created:
                info["created"] = created
        elif vtype == 2 and vd[88:91] in (b"%/@", b"%/C", b"%/E"):
            fmt.append("Joliet")
    info["format"] = " + ".join(fmt) if fmt else "ISO image"
    info.setdefault("bootable", False)
    return info


# ---------------------------------------------------------------- partition tables (raw images)

_GPT_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
}
_MBR_TYPES = {
    0x01: "FAT12", 0x04: "FAT16", 0x06: "FAT16", 0x0E: "FAT16", 0x0B: "FAT32", 0x0C: "FAT32",
    0x07: "NTFS/exFAT", 0x05: "Extended", 0x0F: "Extended", 0x82: "Linux swap", 0x83: "Linux",
    0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xAF: "Apple HFS", 0xEF: "EFI System", 0xEE: "GPT protective",
}


def _parse_partitions(f, info):
    mbr = _read(f, 0, SECTOR)
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
        return False
    # A FAT floppy/volume image: its boot code sits where MBR entries would.
    if mbr[54:59] == b"FAT12" or mbr[54:59] == b"FAT16" or mbr[82:87] == b"FAT32":
        info["partition_table"] = "none (FAT boot sector, unpartitioned)"
        return True
    entries = [mbr[446 + i * 16:462 + i * 16] for i in range(4)]
    parts = [(e[4], struct.unpack("<II", e[8:16])) for e in entries if e[4]]
    if any(ptype == 0xEE for ptype, _ in parts):
        gpt = _read(f, SECTOR, 92)
        if gpt[:8] == b"EFI PART":
            entries_lba, count, size = struct.unpack("<QII", gpt[72:88])
            count, size = min(count, 256), max(size, 128)
            table = _read(f, entries_lba * SECTOR, count * size)
            found = []
            for i in range(len(table) // size):
                e = table[i * size:(i + 1) * size]
                if e[:16] == b"\x00" * 16:
                    continue
                kind = str(uuid.UUID(bytes_le=e[:16]))
                first, last = struct.unpack("<QQ", e[32:48])
                name = e[56:128].decode("utf-16-le", errors="replace").rstrip("\x00").strip()
                label = name or _GPT_TYPES.get(kind, "unknown type")
                found.append(f"{label} ({_size_label((last - first + 1) * SECTOR)})")
            info["partition_table"] = "GPT"
            info["partitions"] = found
            return True
    if parts:
        info["partition_table"] = "MBR"
        info["partitions"] = [
            f"{_MBR_TYPES.get(ptype, f'type 0x{ptype:02X}')} ({_size_label(count * SECTOR)})"
            for ptype, (_start, count) in parts
        ]
        return True
    return False


def _parse_raw(f, info, kind):
    if _is_iso(f):
        _parse_iso(f, info)
        info["format"] = f"{kind} ({info['format']})"
        return info
    info["format"] = kind
    if not _parse_partitions(f, info):
        if _read(f, 1024, 2) in (b"H+", b"HX"):
            info["filesystem"] = "HFS+ (unpartitioned)"
        elif _read(f, 32, 4) == b"NXSB":
            info["filesystem"] = "APFS (unpartitioned)"
        else:
            info["partition_table"] = "none found"
    return info


# ---------------------------------------------------------------- DMG (UDIF)

_DMG_CHUNKS = {
    0x80000005: "zlib (UDZO)", 0x80000006: "bzip2 (UDBZ)", 0x80000007: "LZFSE (ULFO)",
    0x80000008: "LZMA (ULMO)", 0x80000004: "ADC",
}


def _parse_dmg(f, file_size, info):
    head = _read(f, 0, 8)
    if head == b"encrcdsa" or head[:4] == b"AEA1":
        info.update(format="Apple disk image (encrypted)", encrypted=True)
        return info
    koly = _read(f, file_size - 512, 512) if file_size >= 512 else b""
    if koly[:4] != b"koly":
        return _parse_raw(f, info, "Apple disk image (raw, no UDIF trailer)")
    info["format"] = "Apple disk image (UDIF)"
    info["encrypted"] = False
    xml_offset, xml_length = struct.unpack(">QQ", koly[216:232])
    info["declared_bytes"] = struct.unpack(">Q", koly[492:500])[0] * SECTOR
    if not xml_length or xml_length > MAX_READ:
        return info
    try:
        plist = plistlib.loads(_read(f, xml_offset, xml_length))
    except Exception as e:
        print(f"DMG plist unreadable: {e!r}")
        return info
    methods, names = set(), []
    for blk in (plist.get("resource-fork") or {}).get("blkx", []):
        name = blk.get("Name") or blk.get("CFName") or ""
        if name:
            names.append(name)
        data = blk.get("Data")
        if isinstance(data, str):
            data = base64.b64decode(data)
        if not data or data[:4] != b"mish" or len(data) < 204:
            continue
        count = struct.unpack(">I", data[200:204])[0]
        for i in range(count):
            chunk = data[204 + i * 40:208 + i * 40]
            if len(chunk) == 4:
                methods.add(struct.unpack(">I", chunk)[0])
    compressed = sorted(_DMG_CHUNKS[m] for m in methods if m in _DMG_CHUNKS)
    info["compression"] = ", ".join(compressed) if compressed else "none (read-only or read-write)"
    fs = [n for n in names if "Apple_HFS" in n or "Apple_APFS" in n or "Apple_UFS" in n or "Windows" in n]
    if fs:
        info["partitions"] = fs[:8]
    return info


# ---------------------------------------------------------------- VM disks

def _vhd_footer(f):
    """The VHD footer (last 512 bytes; very old images: 511; dynamic: copy at 0)."""
    f.seek(0, 2)
    size = f.tell()
    for off in (size - 512, size - 511, 0):
        if off >= 0:
            b = _read(f, off, 512)
            if b[:8] == b"conectix":
                return b
    return None


def _parse_vhd(f, info):
    footer = _vhd_footer(f)
    if footer is None:
        return {}
    info["format"] = "VHD (Virtual PC / Hyper-V)"
    stamp, creator = struct.unpack(">I4s", footer[24:32])
    info["declared_bytes"] = struct.unpack(">Q", footer[48:56])[0]
    info["allocation"] = {2: "fixed", 3: "dynamic", 4: "differencing"}.get(
        struct.unpack(">I", footer[60:64])[0], "unknown")
    info["created_by"] = creator.decode("ascii", errors="replace").strip()
    if stamp:
        info["created"] = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc).timestamp() + stamp
    return info


_VHDX_METADATA_REGION = uuid.UUID("8b7ca206-4790-4b9a-b8fe-575f050f886e")
_VHDX_FILE_PARAMS = uuid.UUID("caa16737-fa36-4d43-b3b6-33f0aa44e76b")
_VHDX_DISK_SIZE = uuid.UUID("2fa54224-cd1b-4876-b211-5dbed83bf4b8")


def _parse_vhdx(f, info):
    info["format"] = "VHDX (Hyper-V)"
    creator = _read(f, 8, 512).decode("utf-16-le", errors="replace").split("\x00", 1)[0].strip()
    if creator:
        info["created_by"] = creator
    region = _read(f, 0x30000, 64 * 1024)  # region table
    if region[:4] != b"regi":
        return info
    count = struct.unpack("<I", region[8:12])[0]
    meta_offset = None
    for i in range(min(count, 2047)):
        e = region[16 + i * 32:48 + i * 32]
        if uuid.UUID(bytes_le=e[:16]) == _VHDX_METADATA_REGION:
            meta_offset = struct.unpack("<Q", e[16:24])[0]
    if meta_offset is None:
        return info
    table = _read(f, meta_offset, 64 * 1024)
    if table[:8] != b"metadata":
        return info
    entries = struct.unpack("<H", table[10:12])[0]
    for i in range(min(entries, 2047)):
        e = table[32 + i * 32:64 + i * 32]
        item, offset = uuid.UUID(bytes_le=e[:16]), struct.unpack("<I", e[16:20])[0]
        if item == _VHDX_DISK_SIZE:
            info["declared_bytes"] = struct.unpack("<Q", _read(f, meta_offset + offset, 8))[0]
        elif item == _VHDX_FILE_PARAMS:
            flags = struct.unpack("<I", _read(f, meta_offset + offset + 4, 4))[0]
            info["allocation"] = "differencing" if flags & 2 else ("fixed" if flags & 1 else "dynamic")
    return info


def _vmdk_descriptor(text, info):
    capacity = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("createType="):
            info["allocation"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("ddb.adapterType"):
            info["adapter"] = line.split("=", 1)[1].strip().strip('"')
        elif line.split(" ", 1)[0] in ("RW", "RDONLY", "NOACCESS"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                capacity += int(parts[1]) * SECTOR
    if capacity:
        info.setdefault("declared_bytes", capacity)


def _parse_vmdk(f, info):
    info["format"] = "VMDK (VMware)"
    head = _read(f, 0, 64)
    if head[:4] == b"KDMV":
        _version, flags, capacity, _grain, desc_off, desc_size = struct.unpack("<IIQQQQ", head[4:44])
        info["declared_bytes"] = capacity * SECTOR
        if flags & (1 << 16):
            info["compression"] = "deflate (stream-optimized)"
        if desc_off and desc_size and desc_size * SECTOR <= MAX_READ:
            _vmdk_descriptor(_read(f, desc_off * SECTOR, desc_size * SECTOR)
                             .split(b"\x00", 1)[0].decode("ascii", errors="replace"), info)
    else:  # descriptor-only file: the extents live in separate files
        _vmdk_descriptor(_read(f, 0, 64 * 1024).split(b"\x00", 1)[0]
                         .decode("ascii", errors="replace"), info)
        info["format"] = "VMDK (VMware, descriptor only)"
    return info


def _parse_qcow2(f, info):
    head = _read(f, 0, 112)
    version, backing_off, backing_len, _cluster_bits, size, crypt = struct.unpack(">IQIIQI", head[4:36])
    info["format"] = f"qcow2 v{version} (QEMU)"
    info["declared_bytes"] = size
    info["allocation"] = "sparse (copy-on-write)"
    info["encrypted"] = crypt != 0
    if version >= 3 and len(head) >= 105:
        header_len = struct.unpack(">I", head[100:104])[0]
        if header_len > 104:
            info["compression"] = {0: "zlib", 1: "zstd"}.get(head[104], f"type {head[104]}")
    if backing_off and 0 < backing_len <= 1024:
        info["backing_file"] = _read(f, backing_off, backing_len).decode("utf-8", errors="replace")
    return info


# ---------------------------------------------------------------- hooks

def _parse(path):
    ext = path.suffix.lower()
    info = {}
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if ext == ".iso":
            return _parse_iso(f, info)
        if ext == ".dmg":
            return _parse_dmg(f, size, info)
        if ext == ".img":
            return _parse_raw(f, info, "Raw disk image")
        if ext == ".cdr":
            return _parse_raw(f, info, "Apple CD/DVD master (raw)")
        if ext == ".sparseimage":
            return {"format": "Apple sparse image"}
        if ext == ".vhd":
            return _parse_vhd(f, info)
        if ext == ".vhdx":
            return _parse_vhdx(f, info)
        if ext == ".vmdk":
            return _parse_vmdk(f, info)
        if ext == ".qcow2":
            return _parse_qcow2(f, info)
    return {}


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn: parse the headers once at upload.
    An ISO's volume creation date also becomes the row's content_date."""
    try:
        info = _parse(Path(path))
    except Exception as e:
        print(f"Disk image header parse failed for {path}: {e!r}")
        return {}
    if not info:
        return {}
    out = {"type_metadata": {STATS_KEY: info}}
    if info.get("created") and "ISO" in info.get("format", ""):
        out["content_date"] = info["created"]
    return out


def get_properties(row):
    """ObjectTypeSpec.properties_fn: format the stored header facts (re-parsing
    only for a row that predates the hook). {} on any failure."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats and row.get("stored_filename"):
            path = storage.path_for(row["stored_filename"])
            if path.exists():
                stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            return {}
        props = {"Format": stats.get("format", "Disk image")}
        if stats.get("volume_label"):
            props["Volume label"] = stats["volume_label"]
        if stats.get("declared_bytes"):
            props["Disk size"] = _size_label(stats["declared_bytes"])
        if stats.get("allocation"):
            props["Allocation"] = stats["allocation"]
        if "bootable" in stats:
            props["Bootable"] = "Yes (El Torito)" if stats["bootable"] else "No"
        if stats.get("partition_table"):
            props["Partition table"] = stats["partition_table"]
        if stats.get("partitions"):
            props["Partitions"] = "; ".join(stats["partitions"][:8]) + (
                f" (+{len(stats['partitions']) - 8} more)" if len(stats["partitions"]) > 8 else "")
        if stats.get("filesystem"):
            props["Filesystem"] = stats["filesystem"]
        if stats.get("compression"):
            props["Compression"] = stats["compression"]
        if "encrypted" in stats:
            props["Encrypted"] = "Yes" if stats["encrypted"] else "No"
        if stats.get("backing_file"):
            props["Backing file"] = stats["backing_file"]
        if stats.get("adapter"):
            props["Adapter"] = stats["adapter"]
        for key, label in (("created_by", "Created by"), ("publisher", "Publisher"),
                           ("application", "Application")):
            if stats.get(key):
                props[label] = stats[key]
        if stats.get("created"):
            props["Created"] = datetime.datetime.fromtimestamp(
                stats["created"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return props
    except Exception as e:
        print(f"Disk image properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """preview_fn: nothing to render without mounting; the generic file icon
    (live) or a download link (export). The facts are in the properties line."""
    return _preview.file_icon(ctx)


register(ObjectTypeSpec(
    key="disk_image",
    label="Disk image",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    caption_capable=False,
    extensions=EXTENSIONS,
    sniff_fn=sniff,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,  # headers read once, at upload
    preview_fn=preview,
    badge_icon="\U0001F4BF",  # optical disc
    badge_text="DISK",
))
