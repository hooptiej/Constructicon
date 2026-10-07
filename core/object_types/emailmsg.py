"""Email messages (issue #582): .eml.

Read with the standard library's `email` package (policy.default), so no new
dependency. Nothing in the message is ever executed or fetched: the preview
is a header block plus the body as ESCAPED TEXT. An HTML-only message is
reduced to text by a small stripper built on html.parser (which never loads
anything); scripts, styles and remote images therefore cannot reach the page.

Facts (From, To, Cc, Date, Subject, Message-ID, the attachment list with
names/sizes/content types, and a capped body excerpt for the preview) are
computed once at upload (embedded_metadata_fn) and stored in type_metadata;
properties_fn and preview_fn only read them. The searchable text
(Subject + body) comes from text_extract_fn, which core/ocr.py runs in the
background after upload, like Word and PDF. The Date: header seeds
content_date (core/embedded_metadata.py applies its plausibility gate); the
Subject becomes the title.

NOT restricted: emails are ordinary archive content unless the owner marks a
given one otherwise. They do carry addresses, so the type fits #467's flow the
same way every non-restricted type does (the per-item restricted/redacted
controls); a default-restricted type is certkey's choice, not ours.

Attachments are listed only (v1). Pulling them out as items of their own,
linked back to the message, is a follow-up.

.msg (Outlook's OLE format) is deliberately not handled: see the PR for why.
"""

import datetime
import email
import email.utils
import re
from email import policy
from html.parser import HTMLParser
from pathlib import Path

from markupsafe import Markup, escape

from .. import storage
from . import _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "email_stats"
MAX_FILE_BYTES = 100 * 1024 * 1024   # larger than this isn't parsed (the upload still stores)
MAX_BODY_CHARS = 200_000             # searchable body cap
PREVIEW_CHARS = 20_000               # stored excerpt for the object page
MAX_ATTACHMENTS = 100
MAX_HEADER_CHARS = 2000
EXTENSIONS = frozenset({".eml"})

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BLANK_RUNS = re.compile(r"\n{3,}")


# ---------------------------------------------------------------- text

class _TextStripper(HTMLParser):
    """HTML -> plain text. Drops script/style/head content entirely; block
    tags become line breaks. html.parser only tokenizes; it never fetches."""

    _SKIP = {"script", "style", "head", "title", "template", "noscript"}
    _BREAK = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(html):
    """Plain text from an HTML body (no tags, no script/style, no URLs fetched)."""
    parser = _TextStripper()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:  # html.parser can raise on pathological input; keep what was gathered
        print(f"Email HTML strip stopped early: {e!r}")
    text = "".join(parser.parts)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_RUNS.sub("\n\n", text).strip()


def _clean(value, limit=MAX_HEADER_CHARS):
    """A header value as one safe line: control characters and newlines out, capped."""
    text = _CONTROL.sub("", " ".join(str(value or "").split()))
    return text[:limit]


# ---------------------------------------------------------------- parsing

def _header(msg, name):
    try:
        return _clean(msg[name])
    except Exception as e:  # a malformed header must not lose the whole message
        print(f"Email header {name} unreadable: {e!r}")
        return ""


def _raw_header(msg, name):
    """The header exactly as written (single line, cleaned). The Date: header is read this way
    because policy.default turns an unparseable one into an empty string."""
    wanted = name.lower()
    try:
        for key, value in msg.raw_items():
            if key.lower() == wanted:
                return _clean(value)
    except Exception as e:
        print(f"Email raw header {name} unreadable: {e!r}")
    return ""


def _parse_date(raw):
    """(epoch seconds or None) from a Date: header. Garbage gives None, not an error.
    A date with no zone is read as UTC."""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):  # silent-ok: an unparseable Date: header is simply no date
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):  # silent-ok: out-of-range date = no date
        return None


def _body_text(msg):
    """(text, source) where source is 'plain', 'html' or ''. Prefers the plain
    part; falls back to the HTML part, stripped."""
    for kind in ("plain", "html"):
        try:
            part = msg.get_body(preferencelist=(kind,))
        except Exception as e:
            print(f"Email body lookup failed ({kind}): {e!r}")
            part = None
        if part is None:
            continue
        try:
            content = part.get_content()
        except Exception as e:  # unknown charset / bad encoding: decode leniently by hand
            print(f"Email body decode fell back ({kind}): {e!r}")
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        text = strip_html(content) if kind == "html" else content
        text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
        if text:
            return text, kind
    return "", ""


def _attachments(msg):
    out = []
    try:
        parts = list(msg.iter_attachments())
    except Exception as e:
        print(f"Email attachment walk failed: {e!r}")
        return out
    for part in parts[:MAX_ATTACHMENTS]:
        try:
            name = part.get_filename()
            ctype = part.get_content_type()
            if part.is_multipart() or ctype == "message/rfc822":
                inner = part.get_payload(0) if part.is_multipart() else None
                size = len(part.as_bytes()) if hasattr(part, "as_bytes") else 0
                name = name or _clean(inner["Subject"] if inner is not None else "", 120) or "attached message"
            else:
                size = len(part.get_payload(decode=True) or b"")
            out.append({"name": _clean(name or "(unnamed)", 200), "size": size, "type": _clean(ctype, 100)})
        except Exception as e:
            print(f"Email attachment unreadable: {e!r}")
            out.append({"name": "(unreadable)", "size": 0, "type": ""})
    return out


def _load(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file too large to parse")
    with open(path, "rb") as f:
        return email.message_from_binary_file(f, policy=policy.default)


def parse(path):
    """The facts about one message, or raises. Cheap enough for upload time only."""
    msg = _load(path)
    body, source = _body_text(msg)
    raw_date = _raw_header(msg, "Date")
    attachments = _attachments(msg)
    return {
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "cc": _header(msg, "Cc"),
        "subject": _header(msg, "Subject"),
        "date_header": raw_date,
        "date": _parse_date(raw_date),
        "message_id": _header(msg, "Message-ID"),
        "attachments": attachments,
        "body_source": source,
        "body_chars": len(body),
        "body_excerpt": body[:PREVIEW_CHARS],
        "body_truncated": len(body) > PREVIEW_CHARS,
        "_body": body,
    }


# ---------------------------------------------------------------- hooks

def get_embedded_metadata(path):
    """embedded_metadata_fn: parse once at upload. {} if unreadable."""
    try:
        facts = parse(path)
    except Exception as e:
        print(f"Email parse failed for {path}: {e!r}")
        return {}
    facts.pop("_body", None)
    date = facts.pop("date")
    out = {"type_metadata": {STATS_KEY: facts}}
    if facts["subject"]:
        out["content_description"] = facts["subject"]
    if date is not None:
        out["content_date"] = date
    return out


def extract_text_for_row(row):
    """text_extract_fn: Subject + body for search, capped."""
    stored = row.get("stored_filename")
    if not stored:
        return ""
    path = storage.path_for(stored)
    if not path.exists():
        return ""
    try:
        facts = parse(path)
    except Exception as e:
        print(f"Email text extraction failed for {path}: {e!r}")
        return ""
    text = "\n\n".join(p for p in (facts["subject"], facts["_body"]) if p)
    return text[:min(MAX_BODY_CHARS, storage.MAX_EXTRACTED_TEXT_CHARS)]


def _stats(row):
    stats = (row.get("type_metadata") or {}).get(STATS_KEY)
    return stats if isinstance(stats, dict) else {}


def _fmt_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):  # silent-ok: display only; a bad size is shown as 0 B
        n = 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _attachment_label(a):
    a = a if isinstance(a, dict) else {}
    kind = f", {a.get('type')}" if a.get("type") else ""
    return f"{a.get('name') or '(unnamed)'} ({_fmt_size(a.get('size'))}{kind})"


def get_properties(row):
    """properties_fn: reads only what upload stored. {} on failure."""
    try:
        s = _stats(row)
        if not s:
            return {}
        props = {}
        for label, key in (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Subject", "subject")):
            if s.get(key):
                props[label] = str(s[key])
        if s.get("date_header"):
            props["Date"] = str(s["date_header"])
        if s.get("message_id"):
            props["Message-ID"] = str(s["message_id"])
        atts = s.get("attachments") or []
        props["Attachments"] = (f"{len(atts)}: " + "; ".join(_attachment_label(a) for a in atts)) if atts else "none"
        return props
    except Exception as e:
        print(f"Email properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """preview_fn: header block + the body as escaped text. Same in live and
    export modes (it is all text, nothing to link or load)."""
    s = _stats(ctx.item)
    if not s:
        return _preview.file_icon(ctx)
    rows = []
    for label, key in (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Date", "date_header"), ("Subject", "subject")):
        if s.get(key):
            rows.append(f"<tr><th>{escape(label)}</th><td>{escape(str(s[key]))}</td></tr>")
    atts = s.get("attachments") or []
    if atts:
        items = "".join(f"<li>{escape(_attachment_label(a))}</li>" for a in atts)
        rows.append(f"<tr><th>Attachments</th><td><ul>{items}</ul>"
                    f'<span class="muted">Listed only; not extracted.</span></td></tr>')
    body = str(s.get("body_excerpt") or "")
    more = ""
    if s.get("body_truncated"):
        more = '<p class="muted">…body truncated; download the .eml for the full message</p>'
    note = ' <span class="muted">(HTML message shown as plain text)</span>' if s.get("body_source") == "html" else ""
    body_html = (f'<pre style="white-space:pre-wrap;word-break:break-word">{escape(body)}</pre>' if body
                 else '<p class="muted">(no readable body)</p>')
    return Markup(
        f'<div class="content-text-preview email-preview">'
        f'<table class="email-headers"><tbody>{"".join(rows)}</tbody></table>'
        f'<h4>Message{note}</h4>{body_html}{more}</div>'
    )


register(ObjectTypeSpec(
    key="email",
    label="Email",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # runs the background task that calls text_extract_fn (no OCR for this type)
    caption_capable=False,
    extensions=EXTENSIONS,
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,  # parsed once, at upload
    preview_fn=preview,
    badge_icon="✉️",  # envelope
    badge_text="EMAIL",
))
