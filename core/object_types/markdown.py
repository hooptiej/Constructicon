"""Markdown file support (issue #450): .md/.markdown uploads, rendered.

Split out of the #431 'text' type, which lumped Markdown in with plain text.
Markdown is markup, so the object page renders it (headings, lists, tables,
links); plain .txt stays verbatim in core/object_types/text.py.

Security: Constructicon has no auth, so anything an uploaded .md can smuggle
into its object page is stored XSS. The renderer runs with raw HTML disabled
(`html=False`: a literal `<script>` or `<img onerror>` comes out as escaped
text) and keeps markdown-it's default link validation, which drops
javascript:/vbscript:/file:/data: link targets.

Text extraction for search is shared with the text type (same #433 cap).
"""

from markupsafe import Markup

from .. import markdown_render  # #471: the one shared, safe renderer
from . import register, ObjectTypeSpec, ThumbnailSource
from .text import extract_text, extract_text_for_row, _stored_path

# Rendering past this many characters makes the page heavy for no reader
# benefit; the full text is still searchable and downloadable.
MAX_RENDER_CHARS = 200_000
MAX_OUTLINE_HEADINGS = 12



def _tokens(text):
    try:
        return markdown_render.parse(text)
    except Exception as e:
        print(f"Markdown parse failed: {e!r}")
        return []


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='markdown': Title (first
    H1), Outline (H1/H2 headings), Words (prose text, not markup), Links.
    Returns {} on any failure."""
    text = extract_text(_stored_path(row))
    if not text:
        return {}
    tokens = _tokens(text)

    headings = []  # (level, text)
    words = links = 0
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and i + 1 < len(tokens):
            headings.append((int(tok.tag[1]), tokens[i + 1].content.strip()))
        if tok.type == "inline":
            for child in tok.children or []:
                if child.type in ("text", "code_inline"):
                    words += len(child.content.split())
                elif child.type == "link_open":
                    links += 1

    props = {}
    title = next((t for level, t in headings if level == 1 and t), None)
    if title:
        props["Title"] = title
    outline = [t for level, t in headings if level <= 2 and t]
    if len(outline) > 1:
        shown = outline[:MAX_OUTLINE_HEADINGS]
        more = len(outline) - len(shown)
        props["Outline"] = " · ".join(shown) + (f" (+{more} more)" if more else "")
    props["Words"] = f"{words:,}"
    if links:
        props["Links"] = f"{links:,}"
    return props


def preview(ctx):
    """#450 preview_fn: the rendered document. Live mode wraps it in the
    .markdown-body prose styles (assets/prose.html); the static export gets
    the bare rendered HTML, which is plain semantic markup either way."""
    text = ctx.item.get("extracted_text") or ""
    if not text:
        return None
    truncated = len(text) > MAX_RENDER_CHARS
    html = markdown_render.render(text[:MAX_RENDER_CHARS], breaks=False)  # standard Markdown line handling for .md files
    if truncated:
        html += Markup(f'<p class="muted">…truncated, showing the first {MAX_RENDER_CHARS:,} characters</p>')  # Markup, or += would escape it
    if ctx.mode == "live":
        return Markup(f'<div class="markdown-body markdown-file">{html}</div>')
    return Markup(html)


register(ObjectTypeSpec(
    key="markdown",
    label="Markdown file",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # runs the background task that calls text_extract_fn (no actual OCR)
    extensions=frozenset({".md", ".markdown"}),
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    preview_fn=preview,
    preview_assets=("prose",),
    badge_icon="📝",
    badge_text="MD",
))
