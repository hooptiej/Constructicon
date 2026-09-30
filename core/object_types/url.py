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
"""

from . import register, ObjectTypeSpec, ThumbnailSource, PreStore, IngestCandidate


def url_pre_store(candidate: IngestCandidate) -> PreStore:
    """#194: if there's no content_description but there is an external_url, use the URL as the description."""
    if candidate.external_url and not candidate.content_description:
        return PreStore.accept(content_description=candidate.external_url)
    return PreStore.accept()


register(ObjectTypeSpec(
    key="url",
    label="Web page",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    url_fallback=True,  # #448: the fallback for URLs that don't match any specific type
    pre_store_fn=url_pre_store,  # #448: implement #194 rule
    capture_fn=None,  # TODO(future issue): screenshot the page
    badge_icon="\U0001F517",
    badge_text="WEB",
))
