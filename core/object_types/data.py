"""Data file support (issue #132): raw file text extraction for searchable
indexing.

Data files (CSV and similar formats) are stored as-is with their raw UTF-8
text content extracted and indexed for search. No visual thumbnail concept —
data files are NONE-sourced, same as written posts or archives.

Text extraction reads the file as UTF-8 with best-effort error handling
(corrupt/legacy encodings are replaced rather than erroring), returning the
full raw content as searchable text.

Best-effort, same as every other type's text_extract_fn in this codebase: a
missing file or unreadable encoding returns "" rather than raising, so a bad
upload never breaks the upload response or the OCR background task.
"""

import csv
from markupsafe import Markup, escape

from .. import storage
from . import _preview, _textstats


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def extract_text(path):
    """Raw UTF-8 text content of the data file at `path` (capped at
    storage.MAX_EXTRACTED_TEXT_CHARS, #433), or "" on any failure (missing
    file, encoding issues)."""
    if not path:
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(storage.MAX_EXTRACTED_TEXT_CHARS).strip()
    except Exception as e:
        print(f"Data text extraction failed for {path}: {e!r}")
        return ""


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='data' — see
    core/ocr.py."""
    path = _stored_path(row)
    return extract_text(path) if path else ""


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='data': Rows, Columns,
    Headers, Delimiter, Encoding. Returns {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}

    props = {}

    # Try to parse the CSV
    try:
        scan = _scan(path)
        if scan:
            delimiter, head, counted, complete = scan
            headers = head[0]
            props["Columns"] = str(len(headers))
            rows = counted - 1
            props["Rows"] = f"{rows:,}" if complete else f"{rows:,}+ (first {_COUNT_BUDGET // (1024 * 1024)} MB counted)"

            # Headers (truncated to 200 chars)
            headers_str = ", ".join(headers)
            if len(headers_str) > 200:
                headers_str = headers_str[:197] + "…"
            props["Headers"] = headers_str
            props["Delimiter"] = _DELIMITER_NAMES.get(delimiter, repr(delimiter))
    except Exception as e:
        print(f"CSV properties extraction failed for {path}: {e!r}")

    # Encoding from text stats
    stats = _textstats.text_file_stats(path, 64 * 1024)
    if stats and "encoding" in stats:
        props["Encoding"] = stats["encoding"]

    return props


_DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
# #449: page views stream the file and stop counting rows after this many
# bytes; uploads can be up to the 2 GB limit (#440), so never read it all.
_COUNT_BUDGET = 16 * 1024 * 1024


def _scan(path, keep=21):
    """One streaming pass: (delimiter, first `keep` rows, rows counted,
    complete?) or None for an empty file. Never loads the whole file."""
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(64 * 1024)
        try:
            delimiter = csv.Sniffer().sniff(sample).delimiter
        except csv.Error:  # silent-ok: the sniffer can't tell; comma is the documented default
            delimiter = ","
        f.seek(0)
        # f.tell() raises while a text file is being iterated, so track the
        # characters consumed ourselves (close enough to bytes for a budget).
        consumed = [0]

        def lines():
            for line in f:
                consumed[0] += len(line)
                yield line

        head, counted, complete = [], 0, True
        for row in csv.reader(lines(), delimiter=delimiter):
            if counted < keep:
                head.append(row)
            counted += 1
            if consumed[0] > _COUNT_BUDGET:
                complete = False
                break
    return (delimiter, head, counted, complete) if head else None


def preview(ctx):
    """#449 preview_fn: CSV table preview (header + first 20 data rows).
    Reads the stored file via ctx.file_path; export keeps the download link."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    path = ctx.file_path
    if not path:
        return None

    try:
        scan = _scan(path)
        if not scan:
            return None
        _delimiter, head, counted, complete = scan

        # Build HTML table (header + first 20 data rows)
        headers = head[0]
        data_rows = head[1:21]
        total_rows = counted - 1

        html = '<div class="data-preview"><table><thead><tr>'
        for header in headers:
            html += f'<th>{escape(header)}</th>'
        html += '</tr></thead><tbody>'

        for row in data_rows:
            html += '<tr>'
            for cell in row:
                html += f'<td>{escape(cell)}</td>'
            html += '</tr>'

        html += '</tbody></table></div>'

        if total_rows > 20:
            of = f"{total_rows:,}" if complete else f"{total_rows:,}+"
            html += f'<p class="muted">Showing the first 20 of {of} rows</p>'

        return Markup(html)
    except Exception as e:
        print(f"CSV preview failed for {path}: {e!r}")
        return None


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="data",
    label="Data file",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset({".csv"}),
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    preview_fn=preview,
    preview_assets=("datatable",),
    badge_icon="📊",
    badge_text="DATA",
))
