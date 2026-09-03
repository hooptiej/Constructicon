"""Stream type spec and registration.

Live streams are not yet reachable from the UI or any insert path — registered
ahead of time as a concrete example of the CAPTURE strategy (issue #15 calls
these out by name: "Stream -> an image grab"). Whichever future issue implements
the capture routine fills in capture_fn and nothing outside this file changes.
"""

from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="stream",
    label="Live stream",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    capture_fn=None,  # TODO(future issue): grab a frame of the stream's OSD/wait-card
    badge_icon="\U0001F4E1",
    badge_text="STREAM",
))
