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
from . import _textstats


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
        with path.open(encoding="utf-8", errors="replace") as f:
            sample = f.read(64 * 1024)  # 64 KB sample
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","

            f.seek(0)
            reader = csv.reader(f, delimiter=delimiter)
            rows = list(reader)

        if rows:
            # First row is header
            headers = rows[0]

            props["Columns"] = str(len(headers))
            props["Rows"] = f"{len(rows) - 1:,}"

            # Headers (truncated to 200 chars)
            headers_str = ", ".join(headers)
            if len(headers_str) > 200:
                headers_str = headers_str[:197] + "…"
            props["Headers"] = headers_str

            # Delimiter name
            if delimiter == ",":
                props["Delimiter"] = "comma"
            elif delimiter == ";":
                props["Delimiter"] = "semicolon"
            elif delimiter == "\t":
                props["Delimiter"] = "tab"
            elif delimiter == "|":
                props["Delimiter"] = "pipe"
            else:
                props["Delimiter"] = repr(delimiter)
    except Exception as e:
        print(f"CSV properties extraction failed for {path}: {e!r}")
        pass

    # Encoding from text stats
    stats = _textstats.text_file_stats(path, 64 * 1024)
    if stats and "encoding" in stats:
        props["Encoding"] = stats["encoding"]

    return props


def preview(ctx):
    """#449 preview_fn: CSV table preview (first 21 rows: header + 20 data)."""
    path = _stored_path(ctx.item)
    if not path:
        return None

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            sample = f.read(64 * 1024)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","

            f.seek(0)
            reader = csv.reader(f, delimiter=delimiter)
            rows = list(reader)

        if not rows:
            return None

        # Build HTML table (header + first 20 data rows)
        headers = rows[0]
        data_rows = rows[1:21]
        total_rows = len(rows) - 1

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
            html += f'<p class="muted">Showing the first 20 of {total_rows:,} rows</p>'

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
