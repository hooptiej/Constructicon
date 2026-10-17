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

#701, "look, don't take": everything else that the browser can show by itself (PDF, JPEG/PNG/GIF/WebP,
video, audio, plain text: `object_types.BROWSER_VIEWABLE_TYPES`) is served `inline`, so "View original"
opens the browser's own viewer; anything else (STL, PSD, ZIP, EXE, fonts, ...) stays `attachment`. Both
keep the original name (`filename*=` / `filename=`), so Save-as names the file right. The decision uses
the detected kind first: a file that sniffs as HTML is a download whatever its name says.

#703, the raw view (`/f/<slug>/raw`, `serve_raw_text`): a type with a `raw_text_fn` (source code) shows
its file as `text/plain; charset=utf-8`, inline, nosniff, with the sandbox CSP on every response. It is
never served as its own type, so markup in it (HTML in a .py, a PHP template) is shown as text, not run.
"""

import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse, Response

from core import object_types

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
    # #701: anything that would be served inline is sniffed too, so markup renamed .pdf/.jpg/... is
    # caught here and never handed to the browser inline.
    if (guessed.startswith("text/") or guessed == "application/octet-stream"
            or object_types.opens_in_browser(media_type=guessed)):
        try:
            with open(path, "rb") as fh:
                return _kind_from_bytes(fh.read(SNIFF_BYTES))
        except OSError:  # silent-ok: unreadable file; the route reports it, and no bytes means nothing to render
            return None
    return None


def content_disposition(disposition, filename):
    """`inline` / `attachment` plus the original name: RFC 5987 `filename*=` when it isn't plain ASCII
    (spaces, quotes, unicode), else `filename="..."`."""
    if not filename:
        return disposition
    quoted = quote(filename)
    return f"{disposition}; filename*=utf-8''{quoted}" if quoted != filename else f'{disposition}; filename="{filename}"'


def _attachment(filename):
    return content_disposition("attachment", filename)


def disposition_for(kind, name=None, media_type=None):
    """#701: "inline" or "attachment" for a stored file whose active kind (`active_kind`) is `kind`.
    HTML/XHTML is always a download (#610); SVG/XML stay inline under the sandbox CSP; anything else
    is inline only when the browser shows it itself (`object_types.BROWSER_VIEWABLE_TYPES`)."""
    if kind == "html":
        return "attachment"
    if kind:
        return "inline"
    return "inline" if object_types.opens_in_browser(name, media_type) else "attachment"


def _headers_for(kind, path, name):
    headers = dict(NOSNIFF)
    if kind:
        headers["Content-Security-Policy"] = RENDERED_HTML_CSP
        if kind == "html":
            headers["Content-Disposition"] = _attachment(name or Path(str(path)).name)
    return headers


def file_headers(path, name=None, media_type=None):
    """The security headers for serving the stored file at `path` (its original name `name`)."""
    return _headers_for(active_kind(path, name, media_type), path, name)


RAW_TEXT_TYPE = "text/plain; charset=utf-8"


def raw_text_headers(filename):
    """#703: the headers of the raw plain-text view: inline under the original name, nosniff, and the
    sandbox CSP on every one (belt and braces: text/plain + nosniff already can't render markup, so an
    HTML/SVG/XML file shown here as text is no #610 hole, and the CSP holds even if a browser
    disagreed)."""
    headers = dict(NOSNIFF)
    headers["Content-Security-Policy"] = RENDERED_HTML_CSP
    headers["Content-Disposition"] = content_disposition("inline", filename)
    return headers


def serve_raw_text(path, filename, codec):
    """#703 `/f/<slug>/raw`: the stored file as `text/plain; charset=utf-8`, never as its own type (so
    never text/html, whatever it sniffs as). `codec` is what the type's raw_text_fn said (UTF-8 files
    stream as they are; UTF-16/32 are transcoded to UTF-8). The type caps the size before this runs."""
    headers = raw_text_headers(filename or Path(str(path)).name)
    if codec.replace("-sig", "") == "utf-8":
        return FileResponse(path, media_type=RAW_TEXT_TYPE, headers=headers)
    with open(path, "rb") as fh:
        body = fh.read().decode(codec, errors="replace").encode("utf-8")
    return Response(body, media_type=RAW_TEXT_TYPE, headers=headers)


def serve_file(path, filename=None, type_name=None, download=False):
    """A FileResponse for a user's stored file with the #610 headers. `filename` (the original name)
    names the file in Content-Disposition: inline for what the browser shows itself, attachment for
    the rest (#701). `type_name` names the file for type detection when the bytes served are not the
    original's (a thumbnail that fell back to the original). `download=True` makes it `attachment`
    whatever the type (#703: the raw view's fallback for a file it won't show as text)."""
    name = filename or type_name
    kind = active_kind(path, name)
    headers = _headers_for(kind, path, name)
    resp = FileResponse(path, filename=filename, headers=headers)
    if filename is not None:
        disposition = "attachment" if download else disposition_for(kind, filename)
        resp.headers["content-disposition"] = content_disposition(disposition, filename)
    elif "Content-Disposition" in headers:
        resp.headers["content-disposition"] = headers["Content-Disposition"]
    return resp
