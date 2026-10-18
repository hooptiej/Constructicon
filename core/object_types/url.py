"""URL type spec and registration.

Web pages are reachable from the UI via the upload drawer's paste-a-link field
(see web/app.py's /api/content endpoint) and via .url file drops. They're registered
as a concrete example of the CAPTURE strategy (issue #15 calls these out by name:
"URL -> a screen capture"). Whichever future issue implements the capture routine
fills in capture_fn and nothing outside this file changes.

Issue #194: a generic web page has no title-fetch path the way YouTube does
(real title via scripts/full_youtube_channel_sync.py's API call). Without a
pre_store hook, content_description stays empty and the display_name fallback
chain (filename/content_description/slug) shows only the bare random slug on the
page title/breadcrumb, with no visible trace of the URL the owner actually pasted.
This pre_store_fn seeded the URL itself as the description — no title-fetch API,
just "the URL is better than nothing" behavior.

#724 part d (owner decision 2026-10-17, "Title + summary, safe fetch"): the link's page gives its
searchable text. `page_text` (the type's text_extract_fn, so it runs in a parse_limits child like
every reader) fetches the page through core/safe_fetch.py (the SSRF guard: public addresses only,
pinned, redirects re-checked, 2 MB / 10 s, text/html only, one polite fetch at a time) and reads
its <title>, <meta name="description"> and og:title / og:description with the stdlib HTML parser
(no new dependency, +0 MB), deduplicated, as "Page title:" / "Page summary:" sections
(core/text_sources.combine; type_metadata.text_source 'page'). How it settles:
- a title or summary -> 'done' with that text;
- the site blocks fetching (cults3d.com's Cloudflare challenge), the guard refuses the address
  (private / loopback / odd port / not http), not an HTML page, or a page with no title or summary
  -> grey 'unsupported' with the reason ("web link: site blocks fetching (HTTP 403 challenge)"):
  NotCompatibleFile, nothing to retry;
- a page over 2 MB is not refused: its first 2 MB are read and parsed (the title and meta are in
  <head>; owner decision on PR #754); nothing found there -> grey "no title or summary found in the
  first 2,000,000 bytes";
- a dead link (HTTP 404 / 410) -> red 'failed' (owner: "Red, it's broken"), worded honestly: "page
  not found (HTTP 404): dead link, or a private page this server can't see" (GitHub answers 404 for
  a private repo); Retry re-fetches;
- a timeout, an unreachable site, HTTP 429 / 5xx -> 'failed' with a "try OCR again" reason
  (UnreadableFile): those can pass, and Retry runs the fetch again.
Nothing is ever done to get past a bot check.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlparse
from . import register, ObjectTypeSpec, ThumbnailSource, PreStore, IngestCandidate, NotCompatibleFile, UnreadableFile, _preview
from .. import safe_fetch, text_sources

TITLE_MAX_CHARS = 300
SUMMARY_MAX_CHARS = 1000
_COLLECT_MAX_CHARS = 4000  # an unclosed <title> can't swallow the whole page
_META_KEYS = ("description", "og:title", "og:description")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)


class _PageMeta(HTMLParser):
    """The page's first <title> (not one inside an inline <svg>) and its description / og: meta."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.title_len = 0
        self.in_title = False
        self.title_done = False
        self.svg_depth = 0
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag == "svg":
            self.svg_depth += 1
        elif tag == "title" and not self.title_done and not self.svg_depth:
            self.in_title = True
        elif tag == "meta":
            a = {(k or "").lower(): (v or "") for k, v in attrs}
            key = (a.get("name") or a.get("property") or "").strip().lower()
            content = a.get("content", "").strip()
            if key in _META_KEYS and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag):
        if tag == "svg" and self.svg_depth:
            self.svg_depth -= 1
        elif tag == "title" and self.in_title:
            self.in_title = False
            self.title_done = True

    def handle_data(self, data):
        if self.in_title and self.title_len < _COLLECT_MAX_CHARS:
            self.title_parts.append(data)
            self.title_len += len(data)


def _tidy(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _dedupe(values, also_against=()):
    """Drop empties, repeats, and a value already contained in one kept before it (case-folded)."""
    kept = []
    for v in values:
        if not v:
            continue
        low = v.casefold()
        if any(low == k.casefold() or low in k.casefold() for k in list(kept) + list(also_against)):
            continue
        kept.append(v)
    return kept


def _decode(body, charset):
    names = [charset] if charset else []
    m = _META_CHARSET.search(body[:4096])
    if m:
        names.append(m.group(1).decode("ascii", "replace"))
    for name in names + ["utf-8"]:
        try:
            return body.decode(name, errors="replace")
        except LookupError:  # silent-ok: an unknown charset name falls through to the next guess
            continue
    return body.decode("utf-8", errors="replace")


def page_summary(body, charset=""):
    """(titles, summaries) read from an HTML page's bytes: tidied, capped, deduplicated. Malformed
    markup gives whatever was read before the parser gave up."""
    parser = _PageMeta()
    try:
        parser.feed(_decode(body, charset))
        parser.close()
    except Exception as e:  # malformed markup: keep what was read before it
        print(f"web link: HTML parse stopped early: {e!r}")
    title = _tidy("".join(parser.title_parts), TITLE_MAX_CHARS)
    titles = _dedupe([title, _tidy(parser.meta.get("og:title"), TITLE_MAX_CHARS)])
    summaries = _dedupe([_tidy(parser.meta.get("description"), SUMMARY_MAX_CHARS),
                         _tidy(parser.meta.get("og:description"), SUMMARY_MAX_CHARS)], also_against=titles)
    return titles, summaries


def page_text(row):
    """#724 (d) text_extract_fn: the link's page title and summary, fetched through the guard. See
    the module docstring for how each outcome settles. None only when the row has no address."""
    url = (row.get("external_url") or "").strip()
    if not url:
        return None
    if not safe_fetch.enabled():
        raise NotCompatibleFile("web link: page fetching is turned off on this install")
    try:
        page = safe_fetch.fetch(url, truncate=True)  # owner: over 2 MB, read the first 2 MB (title/meta are in <head>)
    except safe_fetch.FetchError as e:
        reason = str(e)
        if e.transient:
            raise UnreadableFile(f"web link: {reason}" + ("" if "try again" in reason else "; try OCR again")) from None
        if getattr(e, "broken", False):  # owner: a dead link is red ("it's broken"); Retry still re-fetches
            raise UnreadableFile(f"web link: {reason}") from None
        raise NotCompatibleFile(f"web link: {reason}") from None
    titles, summaries = page_summary(page.body, page.charset)
    text, _source = text_sources.combine([
        text_sources.TextSource("page", "Page title", "\n".join(titles)),
        text_sources.TextSource("page", "Page summary", "\n".join(summaries)),
    ])
    if not text:
        if page.truncated:
            raise NotCompatibleFile(f"web link: no title or summary found in the first {safe_fetch.MAX_BYTES:,} bytes")
        raise NotCompatibleFile("web link: the page has no title or summary")
    return text


def url_pre_store(candidate: IngestCandidate) -> PreStore:
    """#194: if there's no content_description but there is an external_url, use the URL as the description."""
    if candidate.external_url and not candidate.content_description:
        return PreStore.accept(content_description=candidate.external_url)
    return PreStore.accept()


def preview(ctx):
    """#449 preview_fn: clickable link card with the URL.
    Live: external link button. Export: bare <a> with optional label."""
    url = ctx.item.get("external_url")
    if not url:
        return None
    label = ctx.item.get("display_name") if ctx.mode == "export" else None
    return _preview.link_card(ctx, url, label=label or url)


def get_properties(row):
    """#449 properties_fn: Domain (without www.), Scheme (uppercase), Path (if not / or empty)."""
    url = row.get("external_url")
    if not url:
        return {}

    try:
        parsed = urlparse(url)
        props = {}

        # Domain (without leading "www.")
        netloc = parsed.netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if netloc:
            props["Domain"] = netloc

        # Scheme (uppercase)
        if parsed.scheme:
            props["Scheme"] = parsed.scheme.upper()

        # Path (only if not empty or /)
        path = parsed.path
        if path and path != "/":
            if len(path) > 120:
                path = path[:117] + "…"
            props["Path"] = path

        return props
    except Exception as e:
        print(f"URL properties extraction failed for {url}: {e!r}")
        return {}


register(ObjectTypeSpec(
    key="url",
    label="Web page",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    url_fallback=True,  # #448: the fallback for URLs that don't match any specific type
    pre_store_fn=url_pre_store,  # #448: implement #194 rule
    capture_fn=None,  # TODO(future issue): screenshot the page
    preview_fn=preview,
    properties_fn=get_properties,
    text_extract_fn=page_text,  # #724 (d): the page's title + summary, through core/safe_fetch.py
    text_source="page",
    badge_icon="\U0001F517",
    badge_text="WEB",
    ocr_unsupported_note="web link: no page address to read",  # #724: only when the row has no external_url
))
