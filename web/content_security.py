"""Serving a user's stored file safely (#610).

`/f/<slug>` and friends hand back bytes somebody uploaded, on the app's own origin, where browsers
carry the session cookie. An uploaded .html or .svg that is rendered as a page there is stored XSS
against whoever opens it, an admin included. The rule (CLAUDE.md, "Auth enforcement"): **user files
are never served as active content on the app origin.**

- Every such response carries `X-Content-Type-Options: nosniff`.
- Active content (HTML, XHTML, SVG, XML, by media type, by extension, or by a sniff of the first
  bytes) also carries the sandbox CSP: the browser runs it in an opaque origin with no script and no
  network, even if the URL is opened directly.
- HTML and XHTML are additionally `Content-Disposition: attachment` (a download, not a page). The
  in-app Rendered view (`/api/image/<slug>/rendered`, #607) is how to look at one.
- SVG stays inline so a hotlinked `<img src="/f/<slug>">` keeps working; the CSP only bites when the
  SVG is opened as a document.
"""

import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse

# The sandbox policy for HTML shown or served on the app origin. `sandbox` (no tokens) = no scripts,
# forms, popups, same-origin or top navigation even if the URL is opened directly; default-src 'none'
# = no network (images/fonts/media only as data: URIs, styles only inline). Shared by the Rendered
# view (web/routes/items.py) and every /f/ response for active content.
RENDERED_HTML_CSP = ("sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
                     "media-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")

NOSNIFF = {"X-Content-Type-Options": "nosniff"}

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
SVG_TYPES = frozenset({"image/svg+xml"})
XML_TYPES = frozenset({"text/xml", "application/xml"})
HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml", ".xht", ".shtml", ".mht", ".mhtml"})
SVG_EXTENSIONS = frozenset({".svg", ".svgz"})
XML_EXTENSIONS = frozenset({".xml", ".xsl", ".xslt", ".rdf", ".atom", ".rss"})

SNIFF_BYTES = 2048


def _kind_from_type(media_type):
    """'html' | 'svg' | 'xml' | None from a declared media type."""
    mt = (media_type or "").split(";")[0].strip().lower()
    if mt in HTML_TYPES:
        return "html"
    if mt in SVG_TYPES:
        return "svg"
    if mt in XML_TYPES or mt.endswith("+xml"):
        return "xml"
    return None


def _kind_from_name(name):
    ext = Path(name or "").suffix.lower()
    if ext in HTML_EXTENSIONS:
        return "html"
    if ext in SVG_EXTENSIONS:
        return "svg"
    if ext in XML_EXTENSIONS:
        return "xml"
    return None


def _kind_from_bytes(head):
    """Sniff what a browser might take for markup, from the start of the file."""
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n\x00").lower()
    if text.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = head.decode("utf-16", errors="ignore").lstrip().lower().encode("utf-8", errors="ignore")
        except (UnicodeError, ValueError):  # silent-ok: decoding a sniff buffer; failure just means "not markup"
            return None
    if text.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<script", b"<iframe", b"<meta")):
        return "html"
    if b"<html" in text[:SNIFF_BYTES] or b"<script" in text[:SNIFF_BYTES]:
        return "html"
    if text.startswith(b"<svg") or b"<svg" in text[:SNIFF_BYTES]:
        return "svg"
    if text.startswith(b"<?xml"):
        return "xml"
    return None


def active_kind(path, name=None, media_type=None):
    """What kind of active content a stored file is, or None. Looks at the declared media type, the
    file name's extension (the original name, falling back to the stored path), and, for types a
    browser might sniff (text/*, unknown), the first bytes."""
    kind = _kind_from_type(media_type) or _kind_from_name(name) or _kind_from_name(str(path))
    if kind:
        return kind
    guessed = media_type or mimetypes.guess_type(name or str(path))[0] or "application/octet-stream"
    if guessed.startswith("text/") or guessed == "application/octet-stream":
        try:
            with open(path, "rb") as fh:
                return _kind_from_bytes(fh.read(SNIFF_BYTES))
        except OSError:  # silent-ok: unreadable file; the route reports it, and no bytes means nothing to render
            return None
    return None


def _attachment(filename):
    if not filename:
        return "attachment"
    quoted = quote(filename)
    return f"attachment; filename*=utf-8''{quoted}" if quoted != filename else f'attachment; filename="{filename}"'


def file_headers(path, name=None, media_type=None):
    """The security headers for serving the stored file at `path` (its original name `name`)."""
    headers = dict(NOSNIFF)
    kind = active_kind(path, name, media_type)
    if kind:
        headers["Content-Security-Policy"] = RENDERED_HTML_CSP
        if kind == "html":
            headers["Content-Disposition"] = _attachment(name or Path(str(path)).name)
    return headers


def serve_file(path, filename=None, type_name=None):
    """A FileResponse for a user's stored file with the #610 headers. `filename` (the original name)
    sets the download name as before; `type_name` names the file for type detection when the bytes
    served are not the original's (a thumbnail that fell back to the original)."""
    name = filename or type_name
    headers = file_headers(path, name)
    resp = FileResponse(path, filename=filename, headers=headers)
    if "Content-Disposition" in headers:
        resp.headers["content-disposition"] = headers["Content-Disposition"]
    elif filename is not None and "Content-Security-Policy" in headers:
        # SVG / XML: inline (hotlinked <img> keeps working); the CSP stops script if opened directly.
        resp.headers["content-disposition"] = resp.headers["content-disposition"].replace("attachment", "inline", 1)
    return resp
