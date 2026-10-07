"""Source code file support (issue #132): raw file text extraction for
searchable indexing.

Source code files (PHP, Python, JavaScript, shell scripts, PowerShell, JSON,
YAML, HTML, CSS, SQL, Lua — #297; Arduino .ino and C/C++ sources and
headers — #469) are stored as-is with their raw UTF-8 text
content extracted and indexed for search. No visual thumbnail concept — code
files are NONE-sourced, same as written posts or archives.

Text extraction decodes the file by its byte-order mark (UTF-8-sig, UTF-16, UTF-32; #607) or a
UTF-16-without-BOM heuristic, else UTF-8 with replacement for bad bytes, returning the raw source
code as searchable UTF-8 text.

Best-effort, same as every other type's text_extract_fn in this codebase: a
missing file or unreadable encoding returns "" rather than raising, so a bad
upload never breaks the upload response or the OCR background task.
"""

import logging
from pathlib import Path
from markupsafe import Markup, escape

from .. import besteffort, storage
from . import _preview, _textstats

log = logging.getLogger("constructicon.code")


# Language detection by extension
_LANGUAGE_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".php": "PHP",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".html": "HTML",
    ".css": "CSS",
    ".sql": "SQL",
    ".lua": "Lua",
    # #469: Arduino and the C family
    ".ino": "Arduino (C++)",
    ".c": "C",
    ".h": "C/C++ header",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++ header",
    ".hh": "C++ header",
}

# #469: highlight.js language per extension, so the preview doesn't rely on
# auto-detection (which can't tell C from C++ from Arduino reliably). Only
# languages in the bundled build (web/static/vendor/highlight/highlight.min.js);
# anything not listed (e.g. .ps1, which the bundle lacks) falls back to
# auto-detection.
_HLJS_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".php": "php", ".sh": "bash",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".html": "xml",
    ".css": "css", ".sql": "sql", ".lua": "lua",
    ".ino": "cpp", ".c": "c", ".h": "cpp", ".cpp": "cpp", ".cc": "cpp",
    ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def extract_text(path):
    """Raw UTF-8 text content of the code file at `path` (capped at
    storage.MAX_EXTRACTED_TEXT_CHARS, #433), or "" on any failure (missing
    file, encoding issues)."""
    if not path:
        return ""
    try:
        return _textstats.read_text(path, storage.MAX_EXTRACTED_TEXT_CHARS).strip()  # #607: BOM / UTF-16 aware
    except Exception as e:
        print(f"Code text extraction failed for {path}: {e!r}")
        return ""


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='code' — see
    core/ocr.py."""
    path = _stored_path(row)
    return extract_text(path) if path else ""


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='code': Language, Lines,
    Encoding, Line endings. Returns {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}

    filename = row.get("filename") or ""
    props = {}

    # Language from extension
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in _LANGUAGE_MAP:
        props["Language"] = _LANGUAGE_MAP[ext]

    # Text stats
    stats = _textstats.text_file_stats(path, storage.MAX_EXTRACTED_TEXT_CHARS)
    if stats:
        if "lines" in stats:
            line_count = stats["lines"]
            if stats.get("truncated"):
                props["Lines"] = f"{line_count:,} (first {storage.MAX_EXTRACTED_TEXT_CHARS // (1024*1024)}+ MB)"
            else:
                props["Lines"] = f"{line_count:,}"
        if "encoding" in stats:
            props["Encoding"] = stats["encoding"]
        if "line_endings" in stats and stats["line_endings"] != "none":
            props["Line endings"] = stats["line_endings"]

    return props


# #607: what the object page shows of a big file. The whole file stays on disk, searchable (the
# extracted text) and downloadable; the page only ever carries this much of it.
PREVIEW_MAX_LINES = 2000
PREVIEW_MAX_CHARS = 200_000
PREVIEW_MAX_ESCAPED = 140_000  # ...and the HTML-escaped block (`<` -> `&lt;` makes markup 4x) stays under this, so the page stays near 300 KB
HIGHLIGHT_MAX_CHARS = 60_000          # above this the block stays plain: highlight.js on 200 KB freezes a tab
COUNT_BUDGET_BYTES = 64 * 1024 * 1024  # the "of N lines" count reads at most this much of the file
RENDER_BIG_BYTES = 8 * 1024 * 1024     # a bigger HTML file waits for a click before the rendered frame loads
HTML_EXTENSIONS = frozenset({".html"})


def is_html(filename):
    return Path(filename or "").suffix.lower() in HTML_EXTENSIONS


RENDER_MAX_CHARS = 32_000_000  # the rendered-frame endpoint serves at most this much of an HTML file


def render_source(path):
    """(html text, truncated?) for the sandboxed rendered view (#607): the file decoded by its BOM or
    UTF-16 heuristic and normalised to UTF-8 text, capped at RENDER_MAX_CHARS."""
    text = _textstats.read_text(path, RENDER_MAX_CHARS + 1)
    return text[:RENDER_MAX_CHARS], len(text) > RENDER_MAX_CHARS


def _lines_note(path, shown, media_url, html):
    """"Showing the first 2,000 of 312,000 lines" + Download / Open raw, for a file cut for the page."""
    total, complete = _textstats.count_lines(path, COUNT_BUDGET_BYTES)
    of = f"{total:,}" if complete else f"at least {total:,}"
    links = ""
    if media_url:
        links = f' <a href="{escape(media_url)}" download>Download</a>'
        if not html:  # a raw .html would run as a page in the app's own origin; only offer the download
            links += f' &middot; <a href="{escape(media_url)}" target="_blank" rel="noopener">Open raw</a>'
    return (f'<p class="muted code-preview-note">Showing the first {shown:,} of {of} lines.{links}</p>')


def preview(ctx):
    """#449 preview_fn: syntax-highlighted source code. #607: reads only the head of the stored file
    (2,000 lines, about 140 KB of page markup), says how much more there is, leaves very long blocks unhighlighted, and
    for HTML adds a Rendered / Code toggle (the rendered view is a sandboxed frame, see
    web/routes/items.py api_rendered_html). The static export keeps its download link."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    path = ctx.file_path
    if not path:
        return None
    try:
        text, shown, cut = _textstats.read_head_lines(path, PREVIEW_MAX_LINES, PREVIEW_MAX_CHARS)
    except OSError as e:
        besteffort.warn(log, "code preview read", e, path=str(path))
        return None
    if not text.strip():
        return None
    esc = escape(text)
    if len(esc) > PREVIEW_MAX_ESCAPED:  # markup-heavy source: trim to whole lines until the escaped block fits
        while len(esc) > PREVIEW_MAX_ESCAPED:
            text = text[:max(1, int(len(text) * PREVIEW_MAX_ESCAPED / len(esc) * 0.95))]
            esc = escape(text)
        text = text[:text.rfind("\n") + 1] if "\n" in text else text
        esc = escape(text)
        shown, cut = max(1, text.count("\n")), True
    filename = ctx.item.get("filename") or ""
    lang = _HLJS_LANGUAGE.get(Path(filename).suffix.lower())
    css = f"hljs language-{lang}" if lang else "hljs"  # #469: explicit language when known
    nohl = ' data-nohl="1"' if len(text) > HIGHLIGHT_MAX_CHARS else ""
    block = f'<pre><code id="code-preview-block" class="{css}"{nohl}>{esc}</code></pre>'
    note = _lines_note(path, shown, ctx.media_url, is_html(filename)) if cut else ""
    if not is_html(filename):
        return Markup(f'<div class="code-preview">{block}</div>{note}')
    slug = ctx.item.get("slug") or ""
    big = (ctx.item.get("file_size") or 0) > RENDER_BIG_BYTES
    return Markup(
        '<div class="html-preview" id="html-preview"'
        f' data-big="{"1" if big else "0"}" data-size="{escape(ctx.item.get("file_size_display") or "")}">'
        '<div class="view-toggle" role="group" aria-label="How to show this HTML file">'
        '<button type="button" class="vt-btn" data-view="rendered" aria-pressed="true">Rendered</button>'
        '<button type="button" class="vt-btn" data-view="code" aria-pressed="false">Code</button>'
        '</div>'
        '<div class="hp-pane" data-pane="rendered">'
        # sandbox="" = every restriction: no scripts, no forms, no popups, no same-origin, no top navigation.
        f'<iframe class="hp-frame" id="html-frame" sandbox="" referrerpolicy="no-referrer" loading="lazy"'
        f' title="Rendered view of {escape(filename)}" data-src="/api/image/{escape(slug)}/rendered"></iframe>'
        '</div>'
        f'<div class="hp-pane" data-pane="code" hidden><div class="code-preview">{block}</div>{note}</div>'
        '</div>'
    )


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="code",
    label="Source code",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset(_LANGUAGE_MAP),  # #469: one list, so Language and accepted extensions can't drift
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    preview_fn=preview,
    preview_assets=("highlightjs",),
    badge_icon="💻",
    badge_text="CODE",
))
