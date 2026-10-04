"""Shared Windows .exe (PE) reading for the installer and application types (#446).

Not a type itself (underscore module, like _preview/_textstats): the facts
both core/object_types/installer.py and core/object_types/application.py show
for an .exe, plus the installer-framework fingerprinting that decides which of
the two claims an upload.

pefile is used with fast_load (headers only) and then parses just the
resource directory, so a large .exe costs a few small reads; nothing is
executed. Fingerprint scans are bounded: the first HEAD_SCAN bytes of the file
and the first OVERLAY_SCAN bytes of the overlay (data appended after the PE
image, which is where installer frameworks keep their payloads).
"""

import datetime
import re
from pathlib import Path

HEAD_SCAN = 8 * 1024 * 1024
OVERLAY_SCAN = 1024 * 1024

_MACHINES = {0x14C: "x86", 0x8664: "x64", 0xAA64: "ARM64", 0x1C4: "ARM", 0x200: "Itanium"}
_SUBSYSTEMS = {2: "GUI", 3: "console", 1: "native", 9: "Windows CE", 10: "EFI application"}
_NAME_HINT = re.compile(r"setup|install", re.IGNORECASE)
_VERSION_KEYS = ("CompanyName", "ProductName", "FileDescription", "ProductVersion",
                 "FileVersion", "OriginalFilename", "InternalName", "LegalCopyright", "Comments")


def is_pe(path):
    """True for an MZ file whose e_lfanew points at a "PE\\0\\0" header."""
    with open(path, "rb") as f:
        mz = f.read(64)
        if len(mz) < 64 or mz[:2] != b"MZ":
            return False
        f.seek(int.from_bytes(mz[60:64], "little"))
        return f.read(4) == b"PE\x00\x00"


def _load(path):
    import pefile
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    return pe


def _decode(b):
    """pefile hands version strings back as UTF-8 bytes; fall back to cp1252
    rather than U+FFFD soup for the odd resource that isn't valid UTF-8."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")


def _version_strings(pe):
    out = {}
    for fileinfo in getattr(pe, "FileInfo", None) or []:
        for entry in fileinfo if isinstance(fileinfo, list) else [fileinfo]:
            if getattr(entry, "Key", b"") != b"StringFileInfo":
                continue
            for table in entry.StringTable:
                for k, v in table.entries.items():
                    key, val = _decode(k), _decode(v).strip().strip("\x00").strip()
                    if key in _VERSION_KEYS and val and key not in out:
                        out[key] = val
    return out


def _fixed_version(pe):
    fixed = getattr(pe, "VS_FIXEDFILEINFO", None)
    if not fixed:
        return None
    fi = fixed[0]
    return (f"{fi.FileVersionMS >> 16}.{fi.FileVersionMS & 0xFFFF}."
            f"{fi.FileVersionLS >> 16}.{fi.FileVersionLS & 0xFFFF}")


def facts(path):
    """Everything worth showing about an .exe, as a flat JSON-safe dict."""
    pe = _load(path)
    try:
        strings = _version_strings(pe)
        dirs = pe.OPTIONAL_HEADER.DATA_DIRECTORY
        info = {
            "architecture": _MACHINES.get(pe.FILE_HEADER.Machine, f"0x{pe.FILE_HEADER.Machine:04X}"),
            "subsystem": _SUBSYSTEMS.get(pe.OPTIONAL_HEADER.Subsystem, f"type {pe.OPTIONAL_HEADER.Subsystem}"),
            # Data directory 4 = Authenticode certificate table, 14 = CLR (.NET) header.
            "signed": len(dirs) > 4 and dirs[4].VirtualAddress != 0 and dirs[4].Size != 0,
            "dotnet": len(dirs) > 14 and dirs[14].VirtualAddress != 0,
        }
        for key, field in (("ProductName", "name"), ("CompanyName", "publisher"),
                           ("FileDescription", "description"), ("OriginalFilename", "original_filename"),
                           ("LegalCopyright", "copyright")):
            if strings.get(key):
                info[field] = strings[key]
        version = strings.get("ProductVersion") or strings.get("FileVersion") or _fixed_version(pe)
        if version:
            info["version"] = version
        stamp = pe.FILE_HEADER.TimeDateStamp
        # Reproducible builds put a hash here, not a time: keep only plausible dates.
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        if 631152000 <= stamp <= now + 86400:  # 1990-01-01 .. tomorrow
            info["built"] = float(stamp)
        return info
    finally:
        pe.close()


def installer_framework(path):
    """The installer framework an .exe was built with, or None when there's
    no fingerprint. Only strong, structural signs count; a name that merely
    says "Setup" is name_hint()'s job, not this one's."""
    pe = _load(path)
    try:
        if any(s.Name.rstrip(b"\x00") == b".wixburn" for s in pe.sections):
            return "WiX Burn"
        overlay_at = pe.get_overlay_data_start_offset()
        strings = " ".join(_version_strings(pe).values())
    finally:
        pe.close()
    with open(path, "rb") as f:
        head = f.read(HEAD_SCAN)
        overlay = b""
        if overlay_at:
            f.seek(overlay_at)
            overlay = f.read(OVERLAY_SCAN)
    if b"\xef\xbe\xad\xdeNullsoftInst" in overlay or b"\xef\xbe\xad\xdeNullsoftInst" in head:
        return "NSIS"
    if b"Inno Setup Setup Data" in overlay or b"rDlPtS\xcd\xe6\xd7\x7b" in head or "Inno Setup" in strings:
        return "Inno Setup"
    if "InstallShield" in strings or b"InstallShield" in head:
        return "InstallShield"
    if b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" in overlay:
        return "embedded MSI"
    return None


def name_hint(path, filename):
    """True when only the NAME suggests an installer: the filename or the
    version strings say setup/install(er). Weak evidence, so it preselects
    the answer to the owner's question instead of deciding it."""
    if filename and _NAME_HINT.search(filename):
        return True
    try:
        pe = _load(path)
        try:
            return bool(_NAME_HINT.search(" ".join(_version_strings(pe).values())))
        finally:
            pe.close()
    except Exception:
        return False


def is_exe_row(row):
    """TypeAction applies_fn: the Reclassify actions only make sense for .exe rows."""
    return Path(row.get("stored_filename") or "").suffix.lower() == ".exe"


def reclassify(row, new_type):
    """Shared by both Reclassify actions: retype the row, and close any open
    "installer or app?" question about it, since this answers it."""
    from .. import db, ingest  # lazy: ingest imports the registry
    ingest.retype(row["slug"], new_type, ingest.run_in_thread)
    for decision in db.list_pending_decisions("retype"):
        if decision["post_slug"] == row["slug"]:
            db.resolve_pending_decision(decision["id"], {"choice": new_type, "via": "reclassify action"})
    return {"message": f"Reclassified as {new_type}"}
