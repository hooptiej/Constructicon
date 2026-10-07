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
import logging
import re
from pathlib import Path

from .. import besteffort

log = logging.getLogger("constructicon.pe")

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
    except UnicodeDecodeError:  # silent-ok: documented fallback decode
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


# #587 item 3: an overlay (data appended after the PE image) that is most of the file, or that
# opens with an archive/installer signature, is the classic shape of a self-extracting installer.
OVERLAY_MAJORITY = 0.5
_ARCHIVE_SIGNATURES = (  # (marker, label), looked for at the start of the overlay
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"PK\x03\x04", "zip"),
    (b"MSCF", "cab"),
    (b"Rar!\x1a\x07", "rar"),
)
_INSTALLER_MARKERS = (  # looked for anywhere in the scanned overlay head
    (b"\xef\xbe\xad\xdeNullsoftInst", "NSIS"),
    (b"Inno Setup Setup Data", "Inno Setup"),
    (b"rDlPtS\xcd\xe6\xd7\x7b", "Inno Setup"),
)
_ARCHIVE_HEAD = 4096


def _cert_table(pe):
    """(file offset, size) of the Authenticode certificate table, or None. Unlike every other
    data directory its address is a FILE offset, not an RVA."""
    dirs = pe.OPTIONAL_HEADER.DATA_DIRECTORY
    if len(dirs) > 4 and dirs[4].VirtualAddress and dirs[4].Size:
        return dirs[4].VirtualAddress, dirs[4].Size
    return None


def _overlay(pe, path):
    """(overlay_start, overlay_bytes) for an open PE, or (None, 0). The Authenticode signature
    sits in the appended data too, so its size is not counted as payload."""
    start = pe.get_overlay_data_start_offset()
    if not start:
        return None, 0
    total = Path(path).stat().st_size
    size = total - start
    cert = _cert_table(pe)
    if cert and cert[0] >= start:
        size -= cert[1]
    return start, max(size, 0)


def overlay_info(path):
    """What the appended data (overlay) says: {"overlay_bytes": n, "overlay_ratio": 0-1,
    "overlay_signature": "7z"|"zip"|"cab"|"rar"|"NSIS"|"Inno Setup"|None}. Reads at most the first
    OVERLAY_SCAN bytes of the overlay; nothing is executed."""
    pe = _load(path)
    try:
        start, size = _overlay(pe, path)
    finally:
        pe.close()
    out = {"overlay_bytes": size, "overlay_ratio": 0.0, "overlay_signature": None}
    if not start or size <= 0:
        return out
    total = Path(path).stat().st_size
    out["overlay_ratio"] = size / total if total else 0.0
    with open(path, "rb") as f:
        f.seek(start)
        head = f.read(OVERLAY_SCAN)
    for marker, label in _ARCHIVE_SIGNATURES:
        if marker in head[:_ARCHIVE_HEAD]:
            out["overlay_signature"] = label
            return out
    for marker, label in _INSTALLER_MARKERS:
        if marker in head:
            out["overlay_signature"] = label
            return out
    return out


def looks_like_self_extractor(info):
    """True when overlay_info() says most of the file is an embedded payload or the payload opens
    with an archive/installer signature."""
    return bool(info.get("overlay_signature")) or info.get("overlay_ratio", 0) > OVERLAY_MAJORITY


# #587 item 4: the Authenticode signer as a publisher fallback. The certificate table is a
# WIN_CERTIFICATE (length, revision, type) wrapping a PKCS#7 blob that carries the signer's
# certificate chain. Read only: no signature or chain is verified and nothing goes online.
_CERT_SCAN_MAX = 2 * 1024 * 1024
_EKU_CODE_SIGNING = "1.3.6.1.5.5.7.3.3"
_EKU_TIMESTAMPING = "1.3.6.1.5.5.7.3.8"


def _der_certificates(blob):
    """Every X.509 certificate found in a PKCS#7/BER blob. The wrapper may be BER (indefinite
    lengths) which a strict loader refuses, but the certificates inside are DER, so they are
    found by their SEQUENCE header and parsed one by one."""
    from cryptography import x509
    certs = []
    i = 0
    while True:
        i = blob.find(b"\x30\x82", i)
        if i < 0 or i + 4 > len(blob):
            break
        length = 4 + int.from_bytes(blob[i + 2:i + 4], "big")
        if length > 64 and i + length <= len(blob):
            try:
                certs.append(x509.load_der_x509_certificate(blob[i:i + length]))
                i += length
                continue
            except ValueError:  # silent-ok: not a certificate at this offset, keep scanning
                pass
        i += 2
    return certs


def _name_attr(name, oid):
    attrs = name.get_attributes_for_oid(oid)
    return attrs[0].value.strip() if attrs and attrs[0].value else None


def signer_name(path):
    """The Authenticode signer's subject CN (else O) from the embedded certificate table, or None
    when the file isn't signed, the table is unreadable, or no leaf certificate is found. The
    signer is the leaf certificate that is not another certificate's issuer, preferring one with a
    code-signing usage over a timestamp authority's."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    pe = _load(path)
    try:
        cert = _cert_table(pe)
    finally:
        pe.close()
    if not cert:
        return None
    offset, size = cert
    with open(path, "rb") as f:
        f.seek(offset)
        blob = f.read(min(size, _CERT_SCAN_MAX))
    certs = _der_certificates(blob[8:])  # skip the WIN_CERTIFICATE header
    issuers = {c.issuer.rfc4514_string() for c in certs if c.issuer != c.subject}
    leaves = [c for c in certs if c.subject.rfc4514_string() not in issuers]

    def usage(c):
        try:
            eku = {o.dotted_string for o in c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value}
        except x509.ExtensionNotFound:  # silent-ok: no EKU extension just means no usage preference
            return 1
        if _EKU_TIMESTAMPING in eku:
            return 2
        return 0 if _EKU_CODE_SIGNING in eku else 1

    for c in sorted(leaves, key=usage):
        name = _name_attr(c.subject, NameOID.COMMON_NAME) or _name_attr(c.subject, NameOID.ORGANIZATION_NAME)
        if name:
            return name
    return None


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
        _start, overlay_bytes = _overlay(pe, path)
        if overlay_bytes > 0:
            info["overlay_bytes"] = overlay_bytes  # #587 item 3
        if info["signed"] and "publisher" not in info:
            # #587 item 4: no CompanyName, so name the signer instead (labelled "Signed by").
            try:
                signer = signer_name(path)
            except Exception as e:
                besteffort.warn(log, "pe: couldn't read the Authenticode signer", e, path=str(path))
                signer = None
            if signer:
                info["signer"] = signer
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
    except Exception as e:
        besteffort.warn(log, "pe: name_hint couldn't parse the executable (no hint)", e, path=str(path))
        return False


def is_exe_row(row):
    """TypeAction applies_fn: the Reclassify actions only make sense for .exe rows."""
    return Path(row.get("stored_filename") or "").suffix.lower() == ".exe"


def reclassify(row, new_type):
    """Shared by both Reclassify actions: retype the row, and close any open
    "installer or app?" question about it, since this answers it."""
    from .. import changes, decisions, ingest, items  # lazy: ingest/items import the registry
    batch_id = changes.new_batch_id()  # #541 phase D: the retype and the answered question = one batch
    items.retype(row["slug"], new_type, ingest.run_in_thread, batch_id=batch_id)
    decisions.close_retype_questions(row["slug"], new_type, batch_id=batch_id)
    return {"message": f"Reclassified as {new_type}"}
