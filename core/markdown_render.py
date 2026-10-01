"""Shared, safe Markdown rendering (#471; first built for .md files in #450).

One place that turns authored Markdown into HTML for every surface:
project write-ups (live project page, the static site export), blog entry
bodies (export), a write-up document's own object page, and .md uploads
(core/object_types/markdown.py).

Security: raw HTML is disabled (`html=False`: a literal <script> or
<img onerror> comes out as escaped text) and markdown-it's default link
validation drops javascript:/vbscript:/file:/data: link targets. The export
used to insert bodies with `| safe`; this replaces that, so whatever is typed
into a write-up can't become live markup on the public site.

`breaks=True` (the default here) turns single newlines into <br>, so the
plain-text bodies the archive actually has (all ten blog entries on
2026-10-01) keep their line structure instead of collapsing into one block.
The .md file type passes breaks=False for standard Markdown behaviour.
"""

from markdown_it import MarkdownIt
from markupsafe import Markup

_RENDERERS = {
    breaks: MarkdownIt("commonmark", {"html": False, "breaks": breaks}).enable(["table", "strikethrough"])
    for breaks in (True, False)
}


def render(text, breaks=True):
    """Markdown `text` -> safe HTML (Markup). "" for empty input."""
    if not text or not str(text).strip():
        return Markup("")
    return Markup(_RENDERERS[breaks].render(str(text)))


def parse(text, breaks=False):
    """markdown-it tokens, for callers that want structure (headings, links)."""
    return _RENDERERS[breaks].parse(str(text or ""))


def to_text(text):
    """Plain text of a Markdown body (no #, **, link syntax): for excerpts."""
    words = []
    for tok in parse(text):
        if tok.type == "inline":
            for child in tok.children or []:
                if child.type in ("text", "code_inline"):
                    words.append(child.content)
                elif child.type in ("softbreak", "hardbreak"):
                    words.append(" ")
            words.append(" ")
    return " ".join("".join(words).split())
