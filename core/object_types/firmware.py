"""Firmware support (issue #485): microcontroller images.

Formats: .hex (Intel HEX flash image), .eep (Intel HEX EEPROM data, the
AVR toolchain's convention), and .bin/.elf *only when the file really is
firmware*: an ELF whose header names an embedded machine type (AVR, ARM,
Xtensa, RISC-V, MSP430...). A raw .bin has no magic to tell it from any
other blob, so it is NOT claimed unless it's one of those ELFs, and
anything else keeps whatever handling it had before (unsupported).

Intel HEX is parsed ONCE at upload by embedded_metadata_fn (record count,
data bytes, address ranges, entry point, checksum validity, the first
records for the preview) and stored under type_metadata[STATS_KEY]; the
per-view properties/preview functions only format those stored facts. The
parse streams line by line and never holds the file in memory.

A .hex/.eep is accepted by extension even when it isn't clean HEX: a
damaged recovery is still the owner's artifact, so the properties say what
is wrong (malformed lines, bad checksums) instead of refusing the upload.

The target MCU is only a GUESS from the filename (atmega168, attiny85,
pic16f877, stm32f103, avr, ...), and is labelled as one.

Best-effort like every other type: never raises, {} at worst.
"""

import re
import struct
from pathlib import Path

from markupsafe import Markup, escape

from .. import storage
from . import _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "firmware_stats"
HEAD_RECORDS = 40          # records kept for the preview
MAX_RANGES = 8             # address ranges kept (total count is still recorded)
MAX_LINE = 4096            # a line longer than this is malformed, not buffered
PREVIEW_LINE_CHARS = 140

HEX_EXTENSIONS = (".hex", ".eep")
ELF_EXTENSIONS = (".bin", ".elf")
EXTENSIONS = frozenset(HEX_EXTENSIONS + ELF_EXTENSIONS)

_HEX_CHARS = re.compile(rb"^[0-9A-Fa-f]+$")

# ELF e_machine values that mean "microcontroller/embedded target".
_ELF_MACHINES = {
    8: "MIPS", 20: "PowerPC", 40: "ARM", 42: "SuperH", 83: "AVR",
    93: "ARC", 94: "Xtensa", 105: "MSP430", 183: "AArch64", 243: "RISC-V",
    0x18AD: "AVR32",
}
_ELF_TYPES = {1: "relocatable", 2: "executable", 3: "shared object", 4: "core"}

# (regex on the lowercased filename stem, label). First match wins; specific
# part numbers before the generic family words.
_MCU_GUESSES = [
    (re.compile(r"(atmega\d+[a-z]*)"), lambda m: m.group(1)),
    (re.compile(r"(attiny\d+[a-z]*)"), lambda m: m.group(1)),
    (re.compile(r"(at90[a-z]+\d+[a-z]*)"), lambda m: m.group(1)),
    (re.compile(r"(pic\d{2}[a-z]*\d+[a-z]*)"), lambda m: m.group(1)),
    (re.compile(r"(stm32[a-z]*\d*[a-z]*)"), lambda m: m.group(1)),
    (re.compile(r"(stm8[a-z]*\d*)"), lambda m: m.group(1)),
    (re.compile(r"(esp32|esp8266)"), lambda m: m.group(1)),
    (re.compile(r"(msp430[a-z]*\d*)"), lambda m: m.group(1)),
    (re.compile(r"(nrf5\d+)"), lambda m: m.group(1)),
    (re.compile(r"(rp2040)"), lambda m: m.group(1)),
    (re.compile(r"(samd\d+)"), lambda m: m.group(1)),
    (re.compile(r"(?<![a-z])(avr)(?![a-z])"), lambda m: "avr"),
    (re.compile(r"(?<![a-z])(pic)(?![a-z])"), lambda m: "pic"),
    (re.compile(r"(arduino)"), lambda m: "avr (arduino)"),
    (re.compile(r"(?<![a-z])(arm)(?![a-z])"), lambda m: "arm"),
]


def guess_mcu(filename):
    """Best-effort MCU/family from a filename, or "". A guess, never a fact."""
    stem = Path(filename or "").stem.lower()
    for pattern, label in _MCU_GUESSES:
        m = pattern.search(stem)
        if m:
            return label(m)
    return ""


# ---------------------------------------------------------------- ELF

def _elf_header(path):
    """Parse the ELF header of `path` -> dict, or None if it isn't an ELF."""
    with open(path, "rb") as f:
        h = f.read(64)
    if len(h) < 20 or h[:4] != b"\x7fELF" or h[4] not in (1, 2) or h[5] not in (1, 2):
        return None
    endian = "<" if h[5] == 1 else ">"
    e_type, machine = struct.unpack(endian + "HH", h[16:20])
    info = {
        "kind": "elf",
        "bits": 32 if h[4] == 1 else 64,
        "endian": "little" if h[5] == 1 else "big",
        "elf_type": _ELF_TYPES.get(e_type, f"type {e_type}"),
        "machine_id": machine,
        "machine": _ELF_MACHINES.get(machine, f"machine {machine}"),
    }
    try:
        if h[4] == 1 and len(h) >= 28:
            info["entry"] = struct.unpack(endian + "I", h[24:28])[0]
        elif h[4] == 2 and len(h) >= 32:
            info["entry"] = struct.unpack(endian + "Q", h[24:32])[0]
    except struct.error:
        pass
    return info


def sniff(path, filename):
    """.hex/.eep: accepted by extension (damage is reported, not refused).
    .bin/.elf: only an ELF whose machine is an embedded target."""
    ext = Path(filename).suffix.lower()
    if ext in HEX_EXTENSIONS:
        return True
    info = _elf_header(path)
    return bool(info and info["machine_id"] in _ELF_MACHINES)


# ---------------------------------------------------------------- Intel HEX

def _parse_record(raw):
    """One stripped line (bytes) -> (kind, length, addr, rtype, data) where
    kind is 'ok', 'badsum' or 'bad'. Never raises."""
    if not raw.startswith(b":") or len(raw) < 11:
        return "bad", 0, 0, 0, b""
    body = raw[1:]
    if len(body) % 2 or not _HEX_CHARS.match(body):
        return "bad", 0, 0, 0, b""
    data = bytes.fromhex(body.decode("ascii"))
    length, addr, rtype = data[0], (data[1] << 8) | data[2], data[3]
    if len(data) != length + 5:
        return "bad", 0, 0, 0, b""
    kind = "ok" if sum(data) & 0xFF == 0 else "badsum"
    return kind, length, addr, rtype, data[4:4 + length]


def parse_intel_hex(path):
    """Stream-parse an Intel HEX file -> stats dict."""
    records = data_bytes = bad_checksums = malformed = 0
    ranges = []           # merged [start, end) address ranges, in file order
    range_count = 0
    base = 0
    entry = None
    eof = False
    head = []

    def add_range(start, end):
        nonlocal range_count
        for r in ranges:
            if r[0] <= end and start <= r[1]:  # touching/overlapping: merge
                r[0], r[1] = min(r[0], start), max(r[1], end)
                return
        range_count += 1
        if len(ranges) < MAX_RANGES:
            ranges.append([start, end])

    with open(path, "rb") as f:
        while True:
            raw = f.readline(MAX_LINE)
            if not raw:
                break
            if not raw.endswith(b"\n") and len(raw) == MAX_LINE:
                # Over-long line: count it, discard the remainder.
                while True:
                    rest = f.readline(MAX_LINE)
                    if not rest or rest.endswith(b"\n"):
                        break
                malformed += 1
                continue
            line = raw.strip()
            if not line:
                continue
            kind, length, addr, rtype, data = _parse_record(line)
            if kind == "bad":
                malformed += 1
                continue
            records += 1
            if kind == "badsum":
                bad_checksums += 1
            if len(head) < HEAD_RECORDS:
                head.append(line.decode("ascii"))
            if rtype == 0:
                data_bytes += length
                if length:
                    start = base + addr
                    add_range(start, start + length)
            elif rtype == 1:
                eof = True
            elif rtype == 2 and len(data) == 2:
                base = ((data[0] << 8) | data[1]) << 4
            elif rtype == 4 and len(data) == 2:
                base = ((data[0] << 8) | data[1]) << 16
            elif rtype == 3 and len(data) == 4:
                entry = (((data[0] << 8) | data[1]) << 4) + ((data[2] << 8) | data[3])
            elif rtype == 5 and len(data) == 4:
                entry = struct.unpack(">I", data)[0]

    ranges.sort()
    return {
        "kind": "ihex",
        "records": records,
        "data_bytes": data_bytes,
        "ranges": ranges,
        "range_count": range_count,
        "entry": entry,
        "eof": eof,
        "bad_checksums": bad_checksums,
        "malformed": malformed,
        "head": head,
    }


# ---------------------------------------------------------------- hooks

def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn: parse once at upload."""
    try:
        path = Path(path)
        ext = path.suffix.lower()
        if ext in (".bin", ".elf"):
            info = _elf_header(path)
        else:
            info = parse_intel_hex(path)
            info["role"] = "EEPROM data" if ext == ".eep" else "Flash image"
        if not info:
            return {}
        return {"type_metadata": {STATS_KEY: info}}
    except Exception as e:
        print(f"Firmware parse failed for {path}: {e!r}")
        return {}


def _stats_for(row):
    """Stored stats, re-parsing only a row that predates the hook."""
    stats = (row.get("type_metadata") or {}).get(STATS_KEY)
    if not stats and row.get("stored_filename"):
        path = storage.path_for(row["stored_filename"])
        if path.exists():
            stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY)
    return stats or {}


def _hex(n):
    return f"0x{n:04X}"


def get_properties(row):
    """ObjectTypeSpec.properties_fn: format stored facts. {} on any failure."""
    try:
        stats = _stats_for(row)
        props = {}
        if stats.get("kind") == "elf":
            props["Format"] = f"ELF, {stats['bits']}-bit {stats['endian']}-endian"
            props["Architecture"] = stats["machine"]
            props["ELF type"] = stats["elf_type"]
            if stats.get("entry") is not None:
                props["Entry point"] = _hex(stats["entry"])
        elif stats.get("kind") == "ihex":
            props["Format"] = "Intel HEX"
            props["Role"] = stats.get("role", "Flash image")
            props["Records"] = f"{stats['records']:,}"
            props["Data bytes"] = f"{stats['data_bytes']:,}"
            ranges = stats.get("ranges") or []
            if ranges:
                parts = [f"{_hex(a)}–{_hex(b - 1)} ({b - a:,} bytes)" for a, b in ranges]
                extra = stats.get("range_count", len(ranges)) - len(ranges)
                props["Address range"] = "; ".join(parts) + (f" (+{extra} more)" if extra > 0 else "")
            if stats.get("entry") is not None:
                props["Entry point"] = _hex(stats["entry"])
            bad = stats.get("bad_checksums", 0)
            props["Checksums"] = ("all valid" if stats["records"] and not bad
                                  else f"{bad:,} of {stats['records']:,} invalid" if bad else "no records")
            if stats.get("malformed"):
                props["Malformed lines"] = f"{stats['malformed']:,}"
            if not stats.get("eof"):
                props["End-of-file record"] = "missing"
        guess = guess_mcu(row.get("filename") or "")
        if guess:
            props["Target MCU (guess from filename)"] = guess
        return props
    except Exception as e:
        print(f"Firmware properties failed for {row.get('slug')}: {e!r}")
        return {}


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn: make the firmware findable by name/MCU."""
    try:
        name = row.get("filename") or ""
        parts = [name, "firmware"]
        guess = guess_mcu(name)
        if guess:
            parts.append(guess)
        stats = _stats_for(row)
        if stats.get("kind") == "elf":
            parts.append(stats.get("machine", ""))
        return " ".join(p for p in parts if p)
    except Exception:
        return ""


def preview(ctx):
    """preview_fn: the first HEX records as escaped monospace text (live and
    export alike: it's static text). ELF/other: the generic file icon."""
    stats = (ctx.item.get("type_metadata") or {}).get(STATS_KEY) or {}
    head = stats.get("head") or []
    if not head:
        return _preview.file_icon(ctx)
    shown = [str(h)[:PREVIEW_LINE_CHARS] + ("…" if len(str(h)) > PREVIEW_LINE_CHARS else "") for h in head]
    total = stats.get("records", len(head))
    html = ('<pre class="ocr-text mono" style="max-height:70vh;overflow:auto">'
            f'{escape(chr(10).join(shown))}</pre>')
    if total > len(head):
        html += f'<p class="muted">First {len(head)} of {int(total):,} records</p>'
    return Markup(html)


register(ObjectTypeSpec(
    key="firmware",
    label="Firmware",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # lets text_extract_fn run (no real OCR: no thumbnail)
    caption_capable=False,
    extensions=EXTENSIONS,
    sniff_fn=sniff,
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,  # HEX parsed once, at upload
    preview_fn=preview,
    badge_icon="\U0001F4DF",  # pager (a chip-ish glyph)
    badge_text="FW",
))
