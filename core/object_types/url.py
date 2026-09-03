"""URL type spec and registration.

Web pages are not yet reachable from the UI or any insert path — registered
ahead of time as a concrete example of the CAPTURE strategy (issue #15 calls
these out by name: "URL -> a screen capture"). Whichever future issue implements
the capture routine fills in capture_fn and nothing outside this file changes.
"""

from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="url",
    label="Web page",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    capture_fn=None,  # TODO(future issue): screenshot the page
    badge_icon="\U0001F517",
    badge_text="WEB",
))
