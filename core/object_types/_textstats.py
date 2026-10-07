"""Shared helpers for text-based type stats (#449).

Pure utility functions: no markup/escaping here, just computed stats.
Exported for use by code.py, text.py, data.py, archive.py."""

import codecs
import io


def human_size(n):
    """Human-readable file size: B, KB, MB, GB with 1 decimal."""
    if n is None or n < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# #607: the encodings the text readers understand. (BOM prefix, python codec, label), longest BOM
# first (UTF-32 LE starts with the UTF-16 LE BOM, so it must be tested before it).
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32", "UTF-32 LE (BOM)"),
    (codecs.BOM_UTF32_BE, "utf-32", "UTF-32 BE (BOM)"),
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8 (BOM)"),
    (codecs.BOM_UTF16_LE, "utf-16", "UTF-16 LE (BOM)"),
    (codecs.BOM_UTF16_BE, "utf-16", "UTF-16 BE (BOM)"),
)
SNIFF_BYTES = 4096


def detect_encoding(head):
    """(python codec, label) for the leading bytes of a text file (#607).

    A byte-order mark decides it: UTF-8-sig, UTF-16 LE/BE, UTF-32 LE/BE (what Windows tools such
    as `gpresult /h`, `powercfg /batteryreport` and `msinfo32` write). With no BOM, UTF-16 is
    recognised by its NULs: ASCII-heavy text in UTF-16 has a zero byte in every other position, so
    a sample where one parity is mostly NUL and the other mostly isn't is UTF-16 (LE when the odd
    positions are the NULs). Anything else is UTF-8 (decoded with replacement, as before)."""
    for bom, codec, label in _BOMS:
        if head.startswith(bom):
            return codec, label
    sample = head[:SNIFF_BYTES]
    pairs = len(sample) // 2
    if pairs >= 4:
        even_nul = sum(1 for b in sample[0:pairs * 2:2] if b == 0)
        odd_nul = sum(1 for b in sample[1:pairs * 2:2] if b == 0)
        if odd_nul > pairs * 0.3 and even_nul < pairs * 0.05:
            return "utf-16-le", "UTF-16 LE (no BOM)"
        if even_nul > pairs * 0.3 and odd_nul < pairs * 0.05:
            return "utf-16-be", "UTF-16 BE (no BOM)"
    return "utf-8", "UTF-8"


def sniff_encoding(path):
    """detect_encoding for the file at `path` (reads the first few KB)."""
    with open(path, "rb") as f:
        return detect_encoding(f.read(SNIFF_BYTES))


def open_text(path, newline=None):
    """The file at `path` as a text stream in its detected encoding (#607), a BOM consumed, bad
    bytes replaced. Rewindable with seek(0) (the data type's CSV sniffing does that)."""
    codec, _label = sniff_encoding(path)
    return io.TextIOWrapper(open(path, "rb"), encoding=codec, errors="replace", newline=newline)


def read_text(path, max_chars):
    """Up to `max_chars` characters of the file at `path`, decoded per detect_encoding (#607)."""
    with open_text(path) as f:
        return f.read(max_chars)


def read_head_lines(path, max_lines, max_chars):
    """The start of a text file for a capped preview (#607): (text, lines_shown, cut) where `cut`
    says the file goes on past what is returned. Stops at whichever of `max_lines` lines or
    `max_chars` characters comes first, never reading the rest of the file."""
    out, size, shown = [], 0, 0
    with open_text(path) as f:
        while True:
            line = f.readline(max_chars - size + 1)  # never more than the budget, even for a one-line 13 MB file
            if not line:
                return "".join(out), shown, False
            if shown >= max_lines:
                return "".join(out), shown, True
            if size + len(line) > max_chars:
                room = max_chars - size
                if room > 0:
                    out.append(line[:room])  # a long last line is cut mid-line
                    shown += 1
                return "".join(out), shown, True
            out.append(line)
            size += len(line)
            shown += 1


def _newline_bytes(head):
    """The bytes one "
" is encoded as in the file whose leading bytes are `head`."""
    codec, _label = detect_encoding(head)
    if codec == "utf-16":
        codec = "utf-16-le" if head.startswith(codecs.BOM_UTF16_LE) else "utf-16-be"
    elif codec == "utf-32":
        codec = "utf-32-le" if head.startswith(codecs.BOM_UTF32_LE) else "utf-32-be"
    elif codec == "utf-8-sig":
        codec = "utf-8"
    return "\n".encode(codec)


def count_lines(path, budget_bytes):
    """(lines, complete): the file's line count, reading at most `budget_bytes` (#607). Counts the
    encoded newline, so UTF-16/32 files count right; `complete` is False when the budget ran out
    (the count is then a lower bound)."""
    count, read, tail = 0, 0, b""
    with open(path, "rb") as f:
        nl = _newline_bytes(f.read(SNIFF_BYTES))
        f.seek(0)
        while read < budget_bytes:
            chunk = f.read(min(1 << 20, budget_bytes - read))
            if not chunk:
                return count + (1 if tail and not tail.endswith(nl) else 0), True
            read += len(chunk)
            count += chunk.count(nl)
            tail = chunk[-len(nl):]
    return count, False


def text_file_stats(path, limit):
    """Extract stats from a text file at path (read at most limit bytes).

    Returns dict with keys:
      - "lines": line count (count of '\n' + 1 if non-empty)
      - "encoding": "UTF-8", "UTF-16 LE (BOM)" etc. (#607), or "not UTF-8 (shown with replacements)"
      - "line_endings": "CRLF", "LF", "mixed", or "CR"
      - "truncated": True if file exceeded limit

    Returns {} on any failure (missing file, unreadable)."""
    if not path:
        return {}
    try:
        # Read the file with replacement for non-UTF-8 bytes
        with open(path, "rb") as f:
            data = f.read(limit)
        is_truncated = len(data) == limit and path.stat().st_size > limit

        # Check encoding (#607: BOM / UTF-16 aware)
        codec, label = detect_encoding(data)
        is_utf8 = True
        if codec == "utf-8":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:  # silent-ok: not UTF-8 is reported as is_utf8=False below
                text = data.decode("utf-8", errors="replace")
                is_utf8 = False
        else:
            text = data.decode(codec, errors="replace")

        # Count lines
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)

        # Detect line endings
        crlf_count = text.count("\r\n")
        lf_only_count = text.count("\n") - crlf_count
        cr_only_count = text.count("\r") - crlf_count

        if crlf_count > 0 and lf_only_count == 0 and cr_only_count == 0:
            line_ending = "CRLF"
        elif lf_only_count > 0 and crlf_count == 0 and cr_only_count == 0:
            line_ending = "LF"
        elif cr_only_count > 0 and crlf_count == 0 and lf_only_count == 0:
            line_ending = "CR"
        elif crlf_count > 0 or lf_only_count > 0 or cr_only_count > 0:
            line_ending = "mixed"
        else:
            line_ending = "none"

        result = {
            "lines": lines,
            "encoding": label if is_utf8 else "not UTF-8 (shown with replacements)",
            "line_endings": line_ending,
        }
        if is_truncated:
            result["truncated"] = True
        return result
    except Exception as e:
        print(f"Text stats extraction failed for {path}: {e!r}")
        return {}
