"""The one answer to "what is this item called?" (#542).

Every surface that shows an item's name (cards, object page, project grid and timeline, piles,
MCP, search, the static export, the blog builder, the curation queue) calls `title_of(row)`.
The order is decided here once (owner decisions on #542: 2026-10-18 "a separate short title field",
refined 2026-10-25 "keep the description as the title fallback ONLY when it is hand-written"):

    display_name  (the owner's rename override, #11/#241)
    -> short_title  (a few words: the content's own title, or the short title a caption came with)
    -> content_description, ONLY when it is hand-written (description_is_handwritten)
    -> filename
    -> slug

A model's caption is never the title: Claude's and the captioner's captions are paragraphs, and
tiles were showing paragraphs as names. A caption now comes with a short title (core/captions.py,
MCP constructicon_set_caption(title=...), the caption review page).

Hand-written vs model caption (description_is_handwritten), read from what the row records about
where its content_description came from (type_metadata, written when a caption is accepted):
  * description_caption_model == "owner" (typed or edited by hand: on the review page, or any later
    hand edit through items.update)                                         -> hand-written
  * description_caption_model is anything else (moondream, "mcp-agent"), or
    description_caption_used_at is set with no model (an older review-page Accept)  -> a model caption
  * the text is exactly the item's caption suggestion (auto_caption)                 -> a model caption
  * no caption record at all (the uploader's or a migration's own words, a content's own title,
    an owner edit from before captions were tracked)                                 -> hand-written
Hand-written text that is long shows as a paragraph until a short title is accepted; the
words-first proposals (captions.propose_short_titles) offer one cut from those very words.
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

import json
import re

# The longest short title the field takes (a YouTube title is at most 100 characters, too).
SHORT_TITLE_MAX = 100
# A caption at most this long, on one line, already reads as a name: the seed migration and the
# caption review keep it as the short title. A longer one is a description and needs its own.
CAPTION_AS_TITLE_MAX = 80

_SPACE = re.compile(r"\s+")
_QUOTES = "\"'`“”‘’*_"


HANDWRITTEN_MODEL = "owner"  # = captions.OWNER_MODEL (this module imports nothing from core)


def _meta(row):
    tm = row.get("type_metadata")
    if isinstance(tm, str):
        try:
            tm = json.loads(tm or "{}")
        except ValueError:  # silent-ok: an unreadable bag records no caption, so the text counts as typed
            tm = {}
    return tm if isinstance(tm, dict) else {}


def description_is_handwritten(row):
    """True when the row's content_description is the owner's (or uploader's) own words, False when
    it is a model's caption, None when there is no description. The rule is in the module docstring."""
    cd = (row.get("content_description") or "").strip()
    if not cd:
        return None
    tm = _meta(row)
    model = tm.get("description_caption_model")
    if model == HANDWRITTEN_MODEL:
        return True
    if model or tm.get("description_caption_used_at"):
        return False
    return cd != (tm.get("auto_caption") or "").strip()


def title_of(row):
    return (row.get("display_name") or row.get("short_title")
            or (row.get("content_description") if description_is_handwritten(row) else None)
            or row.get("filename") or row.get("slug") or "")


_SENTENCE_END = re.compile(r"(?<=[!?])\s|(?<=\.)\s(?=\S)")
_WORD_CUTS = (" - ", " \u2014 ", " \u2013 ", "; ", ": ", ", ", " (")
_TRAILING_STOP = frozenset("a an the and or with of on in at to for by from its his her their is are as but so".split())


def title_from_words(text, max_words=8):
    """#542, words-first: a short title cut from an item's own (hand-written) description, on word
    and phrase boundaries only, no model: its first sentence (keeping a "!" or "?", dropping a full
    stop), then its first clause (cut at " - ", a dash, ";", ":", ","), at most `max_words` words with
    no dangling "and"/"with" at the end. "Skinned and test fit! - it actually glides..." ->
    "Skinned and test fit!". None when nothing usable is left."""
    t = " ".join((text or "").split())
    if not t:
        return None
    t = _SENTENCE_END.split(t, 1)[0]
    for cut in _WORD_CUTS:
        i = t.find(cut)
        if i > 0 and (cut != ", " or len(t[:i].split()) >= 3):  # "Oct 21, 2023" / "Backbox, Glass" stay whole
            t = t[:i]
    words = t.split()[:max_words]
    while len(words) > 1 and words[-1].lower().strip(",.;:") in _TRAILING_STOP:
        words.pop()
    title = clean_short_title(" ".join(words).rstrip(",;:-—– "))
    if not title or not any(ch.isalpha() for ch in title):
        return None
    return title


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
