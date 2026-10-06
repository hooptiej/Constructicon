"""Excel workbooks (#478): .xlsx .xlsm .xltx .xltm (openpyxl) and old .xls (xlrd).

Shown like a CSV (the data type): each sheet as a table, the header plus
the first PREVIEW_ROWS rows, reusing the data type's "datatable" styles,
one collapsible section per sheet. Formulas show their saved values
(openpyxl data_only), the numbers Excel last calculated.

Everything visible is read ONCE at upload (embedded_metadata_fn, the STL
#449 lesson): sheet names, sizes, hidden sheets, a small preview grid per
sheet, and the workbook's properties (title, author, dates, macros). Cell
text from every sheet feeds search via text_extract_fn, capped like CSV.

Password-protected workbooks are recognised and reported, not refused.
"""

import datetime
import zipfile
from pathlib import Path

from markupsafe import Markup, escape

from .. import storage
from . import _office, _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "spreadsheet_stats"
MODERN = {".xlsx", ".xlsm", ".xltx", ".xltm"}
PREVIEW_ROWS = 20      # data rows under the header, like CSV
PREVIEW_COLS = 20
PREVIEW_SHEETS = 12
CELL_CHARS = 60


def _path(row):
    stored = row.get("stored_filename")
    p = storage.path_for(stored) if stored else None
    return p if p and p.exists() else None


def sniff(path, filename):
    ext = Path(filename).suffix.lower()
    if ext in MODERN:
        return _office.is_ooxml(path, "xl/workbook.xml") or _office.is_encrypted_ooxml(path)
    if ext == ".xls":
        streams = _office.ole_streams(path)
        return bool(streams and ({"Workbook", "Book"} & streams))
    return False


def _cell(value):
    """A cell value as display text."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, datetime.datetime):
        value = value.strftime("%Y-%m-%d %H:%M") if (value.hour or value.minute) else value.strftime("%Y-%m-%d")
    text = str(value).strip()
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS - 1] + "…"


def _trim(grid):
    """Drop trailing empty rows/columns from a preview grid."""
    while grid and not any(grid[-1]):
        grid.pop()
    width = max((max((i + 1 for i, c in enumerate(r) if c), default=0) for r in grid), default=0)
    return [r[:width] for r in grid]


def _sheets_xlsx(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            info = {"name": ws.title, "hidden": ws.sheet_state != "visible"}
            want_preview = len(sheets) < PREVIEW_SHEETS
            # Excel records each sheet's used range (<dimension>); when it's
            # there, read only the preview rows instead of walking a sheet
            # that might have a million of them.
            if ws.max_row and ws.max_column and ws.max_row > 1:
                rows, cols = ws.max_row, ws.max_column
                grid = [[_cell(v) for v in r] for r in ws.iter_rows(
                    max_row=PREVIEW_ROWS + 1, max_col=PREVIEW_COLS, values_only=True)] if want_preview else []
            else:
                grid, rows, cols = [], 0, 0
                for r in ws.iter_rows(values_only=True):
                    rows += 1
                    filled = [i for i, v in enumerate(r) if v not in (None, "")]
                    if filled:
                        cols = max(cols, filled[-1] + 1)
                    if want_preview and len(grid) < PREVIEW_ROWS + 1:
                        grid.append([_cell(v) for v in r[:PREVIEW_COLS]])
            info["preview"] = _trim(grid)
            info["cols"] = cols
            info["rows"] = rows
            sheets.append(info)
        return sheets
    finally:
        wb.close()


def _sheets_xls(path):
    import xlrd
    book = xlrd.open_workbook(path, on_demand=True)
    try:
        sheets = []
        for i in range(book.nsheets):
            sh = book.sheet_by_index(i)
            grid = []
            if i < PREVIEW_SHEETS:
                for r in range(min(sh.nrows, PREVIEW_ROWS + 1)):
                    row = []
                    for c in range(min(sh.ncols, PREVIEW_COLS)):
                        cell = sh.cell(r, c)
                        v = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            try:
                                v = xlrd.xldate.xldate_as_datetime(v, book.datemode)
                            except (ValueError, OverflowError):  # silent-ok: an out-of-range date cell shows its raw number
                                pass
                        row.append(_cell(v))
                    grid.append(row)
            sheets.append({"name": sh.name, "hidden": sh.visibility != 0, "rows": sh.nrows,
                           "cols": sh.ncols, "preview": _trim(grid)})
            book.unload_sheet(i)
        return sheets
    finally:
        book.release_resources()


def get_embedded_metadata(path):
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext == ".xls":
            stats = {"format": "Excel 97–2003 workbook", **_office.ole_props(path), "sheets": _sheets_xls(path)}
        elif _office.is_encrypted_ooxml(path):
            stats = {"format": "Excel workbook", "encrypted": True}
        else:
            with zipfile.ZipFile(path) as z:
                stats = {"format": "Excel workbook", **_office.ooxml_props(z)}
            stats["sheets"] = _sheets_xlsx(path)
            stats["has_thumbnail"] = bool(_office.ooxml_thumbnail(path))
    except Exception as e:
        print(f"Spreadsheet scan failed for {path}: {e!r}")
        return {}
    out = {"type_metadata": {STATS_KEY: stats}}
    if stats.get("title"):
        out["content_description"] = stats["title"]
    if stats.get("created"):
        out["content_date"] = stats["created"]
    return out


def extract_text_for_row(row):
    """text_extract_fn: cell text from every sheet, row by row, capped."""
    path = _path(row)
    if not path:
        return ""
    cap = storage.MAX_EXTRACTED_TEXT_CHARS
    out, size = [], 0
    try:
        if path.suffix.lower() == ".xls":
            import xlrd
            book = xlrd.open_workbook(path, on_demand=True)
            try:
                for i in range(book.nsheets):
                    sh = book.sheet_by_index(i)
                    out.append(f"[{sh.name}]")
                    for r in range(sh.nrows):
                        line = "\t".join(str(v) for v in sh.row_values(r) if v not in ("", None))
                        if line:
                            out.append(line)
                            size += len(line)
                            if size > cap:
                                break
                    book.unload_sheet(i)
                    if size > cap:
                        break
            finally:
                book.release_resources()
        elif path.suffix.lower() in MODERN and not _office.is_encrypted_ooxml(path):
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            try:
                for ws in wb.worksheets:
                    out.append(f"[{ws.title}]")
                    for r in ws.iter_rows(values_only=True):
                        line = "\t".join(str(v) for v in r if v not in ("", None))
                        if line:
                            out.append(line)
                            size += len(line)
                            if size > cap:
                                break
                    if size > cap:
                        break
            finally:
                wb.close()
    except Exception as e:
        print(f"Spreadsheet text extraction failed for {path}: {e!r}")
    return "\n".join(out)[:cap]


def has_thumbnail(row):
    """has_thumbnail_fn: only files that carry their own preview image."""
    return bool(((row.get("type_metadata") or {}).get(STATS_KEY) or {}).get("has_thumbnail"))


def capture_thumbnail(row):
    path = _path(row)
    return _office.ooxml_thumbnail(path) if path and path.suffix.lower() in MODERN else None


def get_properties(row):
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats and _path(row):
            stats = (get_embedded_metadata(_path(row)).get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            return {}
        props = {"Format": stats.get("format", "Excel workbook")}
        props.update(_office.common_props(stats))
        sheets = stats.get("sheets") or []
        if sheets:
            def describe(s):
                hidden = ", hidden" if s.get("hidden") else ""
                return f"{s['name']} ({s.get('rows', 0):,} × {s.get('cols', 0):,}{hidden})"
            shown = "; ".join(describe(s) for s in sheets[:10])
            props["Sheets"] = shown + (f"; +{len(sheets) - 10} more" if len(sheets) > 10 else "")
        return props
    except Exception as e:
        print(f"Spreadsheet properties failed for {row.get('slug')}: {e!r}")
        return {}


def _table(grid, total_rows):
    if not grid:
        return '<p class="muted">Empty sheet</p>'
    head, body = grid[0], grid[1:PREVIEW_ROWS + 1]
    width = max(len(r) for r in grid)
    html = '<div class="data-preview"><table><thead><tr>'
    html += "".join(f"<th>{escape(c)}</th>" for c in head + [""] * (width - len(head)))
    html += "</tr></thead><tbody>"
    for r in body:
        html += "<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r + [""] * (width - len(r))) + "</tr>"
    html += "</tbody></table></div>"
    if total_rows - 1 > PREVIEW_ROWS:
        html += f'<p class="muted">Showing the first {PREVIEW_ROWS} of {total_rows - 1:,} rows</p>'
    return html


def preview(ctx):
    """Each sheet as a table (first sheet open, the rest collapsed)."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    stats = (ctx.item.get("type_metadata") or {}).get(STATS_KEY) or {}
    if stats.get("encrypted"):
        return None
    sheets = stats.get("sheets") or []
    if not sheets:
        return None
    parts = []
    for i, s in enumerate(sheets[:PREVIEW_SHEETS]):
        label = escape(s["name"]) + (' <span class="muted">(hidden)</span>' if s.get("hidden") else "")
        parts.append(f'<details class="sheet-preview"{" open" if i == 0 else ""}>'
                     f'<summary>{label}</summary>{_table(s.get("preview") or [], s.get("rows", 0))}</details>')
    if len(sheets) > PREVIEW_SHEETS:
        parts.append(f'<p class="muted">…and {len(sheets) - PREVIEW_SHEETS} more sheets</p>')
    return Markup("".join(parts))


register(ObjectTypeSpec(
    key="spreadsheet",
    label="Excel workbook",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    caption_capable=False,
    extensions=frozenset(MODERN | {".xls"}),
    sniff_fn=sniff,
    capture_fn=capture_thumbnail,
    has_thumbnail_fn=has_thumbnail,  # only when the file embeds a preview
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,
    preview_fn=preview,
    preview_assets=("datatable",),
    badge_icon="\U0001F4CA",  # bar chart
    badge_text="XLS",
))
