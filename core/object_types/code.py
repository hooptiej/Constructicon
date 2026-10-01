"""Source code file support (issue #132): raw file text extraction for
searchable indexing.

Source code files (PHP, Python, JavaScript, shell scripts, PowerShell, JSON,
YAML, HTML, CSS, SQL, Lua — #297; Arduino .ino and C/C++ sources and
headers — #469) are stored as-is with their raw UTF-8 text
content extracted and indexed for search. No visual thumbnail concept — code
files are NONE-sourced, same as written posts or archives.

Text extraction reads the file as UTF-8 with best-effort error handling
(corrupt/legacy encodings are replaced rather than erroring), returning the
full raw source code as searchable text.

Best-effort, same as every other type's text_extract_fn in this codebase: a
missing file or unreadable encoding returns "" rather than raising, so a bad
upload never breaks the upload response or the OCR background task.
"""

from pathlib import Path
from markupsafe import Markup, escape

from .. import storage
from . import _preview, _textstats


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
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(storage.MAX_EXTRACTED_TEXT_CHARS).strip()
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


def preview(ctx):
    """#449 preview_fn: syntax-highlighted source code from extracted_text.
    The static export keeps its download link (it has no highlighter/CSS)."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    text = ctx.item.get("extracted_text") or ""
    if not text:
        return None
    lang = _HLJS_LANGUAGE.get(Path(ctx.item.get("filename") or "").suffix.lower())
    css = f"hljs language-{lang}" if lang else "hljs"  # #469: explicit language when known
    return Markup(
        f'<div class="code-preview"><pre><code id="code-preview-block" class="{css}">{escape(text)}</code></pre></div>'
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
