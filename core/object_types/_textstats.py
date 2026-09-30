"""Shared helpers for text-based type stats (#449).

Pure utility functions: no markup/escaping here, just computed stats.
Exported for use by code.py, text.py, data.py, archive.py."""


def human_size(n):
    """Human-readable file size: B, KB, MB, GB with 1 decimal."""
    if n is None or n < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def text_file_stats(path, limit):
    """Extract stats from a text file at path (read at most limit bytes).

    Returns dict with keys:
      - "lines": line count (count of '\n' + 1 if non-empty)
      - "encoding": "UTF-8" or "not UTF-8 (shown with replacements)"
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

        # Check encoding
        is_utf8 = True
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
            is_utf8 = False

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
            "encoding": "UTF-8" if is_utf8 else "not UTF-8 (shown with replacements)",
            "line_endings": line_ending,
        }
        if is_truncated:
            result["truncated"] = True
        return result
    except Exception as e:
        print(f"Text stats extraction failed for {path}: {e!r}")
        return {}
