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

import re

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


def _inline_text(children):
    words = []
    for child in children or []:
        if child.type in ("text", "code_inline"):
            words.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            words.append(" ")
    return " ".join("".join(words).split())


def clamp(text, max_chars):
    """`text` cut to at most `max_chars` (plus the ellipsis) at a word boundary, newlines kept.
    For the card face (#596): the box is fixed, so the excerpt is too."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > max_chars // 2:
        cut = cut[:space]
    return cut.rstrip(" \n,;:-—") + "…"


# A write-up's own production note ("Reconstructed by Claude from the project's files...", "Written
# up by ... from conversation"): about how the text was made, not what the card is. The lead skips it.
_EDITORIAL_NOTE = re.compile(r"^(reconstructed|written up|drafted|transcribed|compiled)\b[^.]{0,80}\bby\b", re.I)


def lead(text, max_chars=320, min_chars=160):
    """The opening of a Markdown write-up as plain text (#596, the card face's text box): its first
    top-level paragraph(s), Markdown stripped, clamped to `max_chars`. Skipped: headings, lists,
    quotes and code (only top-level paragraphs count), a paragraph that is entirely italic (an
    editorial note such as "*The owner's own account, recorded ...*") and one that opens like a
    production note (`_EDITORIAL_NOTE`). Paragraphs are added until `min_chars` is reached, never
    across a heading. "" for a blank or template-only write-up."""
    tokens = parse(text)
    paras = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and paras:
            break
        if tok.type != "inline" or i == 0 or tokens[i - 1].type != "paragraph_open" or tokens[i - 1].level != 0:
            continue
        kids = [c for c in tok.children or [] if not (c.type == "text" and not c.content.strip())]
        if kids and kids[0].type == "em_open" and kids[-1].type == "em_close":
            continue
        plain = _inline_text(tok.children)
        if not plain or _EDITORIAL_NOTE.match(plain):
            continue
        paras.append(plain)
        if sum(len(p) for p in paras) >= min_chars:
            break
    return clamp("\n".join(paras), max_chars)
