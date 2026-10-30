"""Read and extract an archive safely, for "Unpack the Crate" (#605).

Pure file work: no DB, no app imports beyond core/parse_limits.py, so it runs inside a parse_limits
child process (core/unpack.py calls `read_archive` through `parse_limits.call`, which bounds its
time and memory and kills it when either runs out). It also keeps its own deadline, because on a
platform with no sandbox (Windows, CONSTRUCTICON_PARSE_SANDBOX=off) the child is just this process.

The safety rules (untrusted input), each with its own plain-words refusal:
- **Paths.** A member name that is absolute (`/etc/x`, `\\x`, `C:\\x`) or climbs out (`..`) makes the
  whole archive refused (`archive_hostile_path`, naming the member): nothing is extracted. Member
  names are never used as paths on disk anyway: each file is written as `<dest>/m<index>`, so a
  hostile name can't reach anywhere even if a check were missed (the .7z reader is the one
  exception, see below).
- **Links.** Symbolic and hard links (tar, zip with Unix modes, 7z) are never followed or extracted:
  that member is skipped and listed ("a link; links are never followed or extracted"). Device files
  and FIFOs likewise ("not a regular file").
- **Bombs.** `parse_limits.unpack_caps()`: the entry count, the total unpacked bytes (by default the
  install's max upload size), the compression ratio (per member, and for the whole archive, once
  past `UNPACK_RATIO_FLOOR_BYTES`) and the time. Declared sizes are checked before anything is
  written (zip, 7z); the bytes actually written are counted as they go (zip, tar), so a member that
  lies about its size is caught too. Hitting a cap refuses the whole archive, naming the cap and the
  numbers (`archive_too_many`, `archive_too_big`, `archive_ratio`, `archive_too_slow`).
- **Encryption.** Every file encrypted (or a .7z that needs a password): `archive_encrypted`, "This
  archive is password-protected; nothing was extracted." Only some members encrypted: those are
  skipped and listed ("encrypted, not extracted").
- **Corrupt.** An archive that can't be opened or breaks partway: `archive_corrupt`, with the reason.

The answer is plain data (it crosses a process boundary): {"ok": True, "format", "entries": [...],
"totals"} or {"ok": False, "code", "message", "details"}; never an exception for a refusal.

.7z: py7zr 1.1.3 (the bomb and symlink-chain CVEs on #605 are fixed there) can only extract by
name, so a .7z is extracted with `extractall` into an empty folder of its own, AFTER every name has
passed the path check; each file is then found by its (checked) name and confirmed to be a regular
file inside that folder, never a link. Its declared sizes are checked first; the bytes it writes
are counted afterwards (it can't be stopped mid-member), and the child's memory and time limits
bound the rest.
"""

import hashlib
import os
import re
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

try:
    import py7zr
except ImportError:  # silent-ok: optional dependency; .7z is refused with a reason without it
    py7zr = None

from . import parse_limits

CHUNK = 1024 * 1024
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz")
_DRIVE = re.compile(r"^[A-Za-z]:")
ZIP_NO_DATE = (1980, 1, 1, 0, 0, 0)
S_IFMT, S_IFLNK = 0o170000, 0o120000

LINK_REASON = "a link; links are never followed or extracted"
SPECIAL_REASON = "not a regular file (a device or a pipe), so it isn't extracted"
ENCRYPTED_REASON = "encrypted, not extracted (it is password-protected)"


class Refused(Exception):
    """A refusal of the whole archive; read_archive turns it into {"ok": False, ...}."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def archive_format(name):
    low = str(name).lower()
    if low.endswith(TAR_SUFFIXES):
        return "tar"
    if low.endswith(".zip"):
        return "zip"
    if low.endswith(".7z"):
        return "7z"
    return None


def _mb(n):
    return f"{n / (1024 * 1024):,.1f} MB"


def clean_name(raw):
    """The member name with backslashes as slashes, a leading './' dropped, control characters
    shown as '?'. Not decoded (that is core/unpack.py's job, for display)."""
    name = str(raw).replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return "".join(ch if ch.isprintable() else "?" for ch in name)


def check_path(raw):
    """The member's path parts, or Refused for an absolute or climbing name."""
    name = str(raw).replace("\\", "/")
    if name.startswith("/") or _DRIVE.match(name) or "\x00" in name:
        raise Refused("archive_hostile_path",
                      f"This archive was refused: the member {clean_name(raw)!r} has an absolute path, which could "
                      "write outside the folder it unpacks into. Nothing was extracted.", member=clean_name(raw))
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise Refused("archive_hostile_path",
                      f"This archive was refused: the member {clean_name(raw)!r} climbs out of its folder (\"..\"), "
                      "the zip-slip trick. Nothing was extracted.", member=clean_name(raw))
    return parts


class _Budget:
    def __init__(self, caps, archive_size):
        self.caps, self.archive_size = caps, max(int(archive_size or 0), 1)
        self.deadline = time.monotonic() + caps.max_seconds
        self.entries = 0
        self.written = 0

    def tick(self):
        if time.monotonic() > self.deadline:
            raise Refused("archive_too_slow",
                          f"This archive was refused: reading it took longer than {self.caps.max_seconds:g} s "
                          "(the unpack time limit), which is what a decompression bomb does. Nothing was extracted.",
                          cap="max_seconds", limit=self.caps.max_seconds)

    def entry(self):
        self.entries += 1
        if self.entries > self.caps.max_members:
            raise Refused("archive_too_many",
                          f"This archive was refused: it holds more than {self.caps.max_members:,} entries (the unpack "
                          "limit on members). Nothing was extracted.",
                          cap="max_members", limit=self.caps.max_members, found_at_least=self.entries)
        self.tick()

    def total(self, nbytes, *, declared=False):
        if nbytes > self.caps.max_total_bytes:
            what = "declares" if declared else "unpacks to more than"
            raise Refused("archive_too_big",
                          f"This archive was refused: it {what} {_mb(nbytes)}, over the {_mb(self.caps.max_total_bytes)} "
                          "unpack limit (the install's max upload size). Nothing was extracted.",
                          cap="max_total_bytes", limit=self.caps.max_total_bytes, bytes=nbytes)
        floor = parse_limits.UNPACK_RATIO_FLOOR_BYTES
        if nbytes > floor and nbytes / self.archive_size > self.caps.max_ratio:
            raise Refused("archive_ratio",
                          f"This archive was refused: it unpacks to {_mb(nbytes)} from {_mb(self.archive_size)}, "
                          f"{nbytes / self.archive_size:,.0f} times its size (the limit is {self.caps.max_ratio:g}x), "
                          "which is what a decompression bomb looks like. Nothing was extracted.",
                          cap="max_ratio", limit=self.caps.max_ratio, ratio=round(nbytes / self.archive_size, 1))

    def member_ratio(self, name, size, csize):
        floor = parse_limits.UNPACK_RATIO_FLOOR_BYTES
        if size > floor and csize is not None and size / max(int(csize), 1) > self.caps.max_ratio:
            raise Refused("archive_ratio",
                          f"This archive was refused: the member {name!r} unpacks to {_mb(size)} from "
                          f"{max(int(csize), 1):,} bytes, {size / max(int(csize), 1):,.0f} times its size (the limit is "
                          f"{self.caps.max_ratio:g}x), which is what a decompression bomb looks like. Nothing was extracted.",
                          cap="max_ratio", limit=self.caps.max_ratio, member=name)


def _copy(src, dst_path, budget, name, declared=None):
    """Streams src to dst_path, hashing and counting. Refuses past the declared size or the total."""
    h = hashlib.sha256()
    n = 0
    with open(dst_path, "wb") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            n += len(chunk)
            budget.written += len(chunk)
            if declared is not None and n > declared:
                raise Refused("archive_too_big",
                              f"This archive was refused: the member {name!r} declares {declared:,} bytes but holds "
                              "more, a trick decompression bombs use. Nothing was extracted.",
                              cap="declared_size", member=name, declared=declared)
            budget.total(budget.written)
            budget.tick()
            h.update(chunk)
            out.write(chunk)
    return n, h.hexdigest()


def _entry(index, raw, kind, size=0, mtime=None, mtime_tuple=None, encrypted=False):
    return {"index": index, "name": clean_name(raw), "parts": check_path(raw),
            "kind": kind, "size": int(size or 0), "mtime": mtime, "mtime_tuple": mtime_tuple,
            "encrypted": bool(encrypted), "file": None, "sha256": None, "reason": None}


# --- zip ------------------------------------------------------------------------------------------

def _read_zip(path, dest, budget):
    entries = []
    with zipfile.ZipFile(path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > budget.caps.max_members:
            budget.entries = len(infos) - 1
            budget.entry()
        for i in infos:  # a hostile name refuses before anything else
            check_path(i.filename)
        budget.total(sum(max(i.file_size, 0) for i in infos), declared=True)
        files = [i for i in infos if not i.is_dir()]
        if files and all(i.flag_bits & 0x1 for i in files):
            raise Refused("archive_encrypted", "This archive is password-protected; nothing was extracted.",
                          encrypted=len(files))
        for index, info in enumerate(infos):
            budget.entry()
            mode = (info.external_attr >> 16) & S_IFMT
            if info.is_dir():
                entries.append(_entry(index, info.filename, "dir"))
                continue
            e = _entry(index, info.filename, "file", info.file_size,
                       mtime_tuple=None if tuple(info.date_time) == ZIP_NO_DATE else tuple(info.date_time),
                       encrypted=info.flag_bits & 0x1)
            entries.append(e)
            if mode == S_IFLNK:
                e.update(kind="link", reason=LINK_REASON)
                continue
            if e["encrypted"]:
                e["reason"] = ENCRYPTED_REASON
                continue
            budget.member_ratio(e["name"], info.file_size, info.compress_size)
            target = Path(dest) / f"m{index}"
            try:
                with zf.open(info) as src:
                    n, digest = _copy(src, target, budget, e["name"], declared=info.file_size)
            except NotImplementedError as exc:  # an unsupported compression method for this one member
                target.unlink(missing_ok=True)
                e["reason"] = f"can't be decompressed here ({exc})"
                continue
            e.update(file=str(target), size=n, sha256=digest)
    return entries


# --- tar ------------------------------------------------------------------------------------------

def _read_tar(path, dest, budget):
    entries = []
    with tarfile.open(path, "r:*") as tf:
        index = -1
        while True:
            member = tf.next()
            if member is None:
                break
            tf.members = []  # tarfile caches every member; don't let it grow
            index += 1
            budget.entry()
            check_path(member.name)
            if member.isdir():
                entries.append(_entry(index, member.name, "dir"))
                continue
            e = _entry(index, member.name, "file", member.size, mtime=float(member.mtime) if member.mtime else None)
            entries.append(e)
            if member.issym() or member.islnk():
                e.update(kind="link", reason=LINK_REASON, size=0)
                continue
            if not member.isreg():
                e.update(kind="special", reason=SPECIAL_REASON, size=0)
                continue
            budget.total(budget.written + max(int(member.size or 0), 0), declared=True)
            target = Path(dest) / f"m{index}"
            src = tf.extractfile(member)
            n, digest = _copy(src, target, budget, e["name"], declared=member.size)
            e.update(file=str(target), size=n, sha256=digest)
    return entries


# --- 7z -------------------------------------------------------------------------------------------

def _7z_time(when):
    """(epoch or None, naive tuple or None) from py7zr's member time, whatever shape it comes in."""
    if when is None:
        return None, None
    try:
        if getattr(when, "tzinfo", None) is not None:
            return when.timestamp(), None
        if hasattr(when, "timetuple"):
            return None, tuple(when.timetuple())[:6]
    except (OverflowError, OSError, ValueError):
        return None, None  # silent-ok: an impossible member date is "no date"; the member itself is still read
    return None, None


def _read_7z(path, dest, budget):
    if py7zr is None:
        raise Refused("archive_unsupported", "This install can't read .7z archives (the 7z reader, py7zr, isn't "
                      "installed). Nothing was extracted.")
    entries = []
    xdir = Path(dest) / "x"
    xdir.mkdir()
    with py7zr.SevenZipFile(path, "r") as zf:
        if zf.needs_password():
            raise Refused("archive_encrypted", "This archive is password-protected; nothing was extracted.")
        infos = zf.list()
        if len(infos) > budget.caps.max_members:
            budget.entries = len(infos) - 1
            budget.entry()
        for info in infos:
            check_path(info.filename)
        budget.total(sum(int(getattr(i, "uncompressed", 0) or 0) for i in infos), declared=True)
        for index, info in enumerate(infos):
            budget.entry()
            kind = "dir" if info.is_directory else "file"
            mtime, mtuple = _7z_time(getattr(info, "creationtime", None))
            e = _entry(index, info.filename, kind, getattr(info, "uncompressed", 0), mtime=mtime, mtime_tuple=mtuple)
            if getattr(info, "is_symlink", False):
                e.update(kind="link", reason=LINK_REASON, size=0)
            entries.append(e)
        zf.extractall(path=xdir)
    root = xdir.resolve()
    for e in entries:
        budget.tick()
        if e["kind"] != "file":
            continue
        p = xdir.joinpath(*e["parts"])
        if p.is_symlink():
            e.update(kind="link", reason=LINK_REASON, size=0)
            continue
        try:
            real = p.resolve()
            real.relative_to(root)
        except (OSError, ValueError):
            e.update(kind="special", reason="its extracted file isn't where its name says; not extracted", size=0)
            continue
        if not real.is_file():
            e.update(kind="special", reason=SPECIAL_REASON, size=0)
            continue
        with open(real, "rb") as src:
            target = Path(dest) / f"m{e['index']}"
            n, digest = _copy(src, target, budget, e["name"])
        real.unlink()
        e.update(file=str(target), size=n, sha256=digest)
    return entries


READERS = {"zip": _read_zip, "tar": _read_tar, "7z": _read_7z}


def _corrupt(fmt, exc):
    detail = (str(exc).strip() or type(exc).__name__).replace("\n", " ")[:160]
    return Refused("archive_corrupt", f"This archive is damaged or isn't a valid .{fmt} file ({detail}). Nothing was "
                   "extracted.", reason=detail)


def read_archive(path, name, dest, caps_dict=None):
    """Read the archive at `path` (its original file name `name` decides the format) and extract its
    regular files into the empty folder `dest`. Returns plain data (see the module docstring)."""
    caps = parse_limits.UnpackCaps(**caps_dict) if caps_dict else parse_limits.unpack_caps()
    fmt = archive_format(name)
    if fmt is None:
        return {"ok": False, "code": "archive_unsupported",
                "message": f"{name!r} isn't a zip, 7z or tar archive, so it can't be unpacked.", "details": {}}
    budget = _Budget(caps, os.path.getsize(path))
    try:
        try:
            entries = READERS[fmt](path, dest, budget)
        except Refused:
            raise
        except MemoryError:
            raise Refused("archive_too_big", "This archive was refused: unpacking it ran out of memory. Nothing was "
                          "extracted.", cap="memory")
        except Exception as exc:  # zipfile.BadZipFile, tarfile.ReadError, EOFError, zlib/lzma errors, py7zr's own
            if py7zr is not None and type(exc).__name__ in ("PasswordRequired",):
                raise Refused("archive_encrypted", "This archive is password-protected; nothing was extracted.")
            if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
                raise Refused("archive_encrypted", "This archive is password-protected; nothing was extracted.")
            raise _corrupt(fmt, exc)
    except Refused as r:
        return {"ok": False, "code": r.code, "message": r.message, "details": r.details}
    total = sum(e["size"] for e in entries if e.get("file"))
    return {"ok": True, "format": fmt, "entries": entries,
            "totals": {"entries": len(entries), "bytes": total, "archive_bytes": budget.archive_size}}
