"""The one answer to "what is this item called?" (#542).

Every surface that shows an item's name (cards, object page, project grid and timeline, piles,
MCP, search, the static export, the blog builder, the curation queue) calls `title_of(row)`.
The order is decided here once (owner decision 2026-10-18 on #542, "a separate short title field"):

    display_name  (the owner's rename override, #11/#241)
    -> short_title  (a few words: the content's own title, or the short title a caption came with)
    -> filename
    -> slug

The long caption (`content_description`) is never the title: Claude's and the captioner's
captions are paragraphs, and tiles were showing paragraphs as names. A caption now comes with a
short title (core/captions.py, MCP constructicon_set_caption(title=...), the caption review page).
A content's OWN title (a YouTube video's title, an e-mail subject, a document's title, a write-up's
"<card> - Write-up") lands in `short_title` when the row is made (db.insert_upload) or its file's
metadata is read (core/embedded_metadata.py), and the `short_title_seed_542` migration copied every
existing title-shaped content_description into it, so those keep their names.

`description` is never a title either: it is the uploader's note about the file (see capture_events
in core/db.py), so it is shown as text, not as a name. `scripts/test_item_title_542.py` fails if a
hand-rolled chain of these fields reappears outside this module.

A leaf module (imports nothing from core) so core/cards.py, core/revisions.py and the object-type
previews can use it without a cycle. The row may be a raw capture_events row or any dict that
carries the same keys (a public item dict already holds the resolved title in `display_name`).
"""

import re

# The longest short title the field takes (a YouTube title is at most 100 characters, too).
SHORT_TITLE_MAX = 100
# A caption at most this long, on one line, already reads as a name: the seed migration and the
# caption review keep it as the short title. A longer one is a description and needs its own.
CAPTION_AS_TITLE_MAX = 80

_SPACE = re.compile(r"\s+")
_QUOTES = "\"'`“”‘’*_"


def title_of(row):
    return (row.get("display_name") or row.get("short_title")
            or row.get("filename") or row.get("slug") or "")


def title_shaped(text, limit=CAPTION_AS_TITLE_MAX):
    """True when `text` can be a name as it is: non-blank, one line, at most `limit` characters."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    return bool(t) and "\n" not in t and "\r" not in t and len(t) <= limit


def clean_short_title(text, limit=SHORT_TITLE_MAX):
    """Normalises a proposed short title (a model's answer, an agent's or the owner's text): one line,
    single spaces, no wrapping quotes or markdown emphasis, no "Title:" label, no trailing full stop.
    A text longer than `limit` is cut at the last word boundary that fits (never mid-word). Returns
    None when nothing is left."""
    if not isinstance(text, str):
        return None
    t = _SPACE.sub(" ", text).strip()
    if t.lower().startswith("title:"):
        t = t[6:].strip()
    t = t.strip(_QUOTES).strip()
    while t.endswith(".") and not t.endswith(".."):
        t = t[:-1].rstrip()
    if len(t) > limit:
        cut = t[:limit + 1].rsplit(" ", 1)[0] if " " in t[:limit + 1] else ""
        t = cut.rstrip(" ,;:-")
    return t or None
