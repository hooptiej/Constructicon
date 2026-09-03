"""Animated GIF support (issue #93): .gif file uploads with frame 0
thumbnail and OCR support.

GIFs are stored as-is (UPLOADED_FILE, like images) so that animated GIFs
remain animated on the detail page's full preview. The thumbnail is
generated from frame 0 by the standard PIL-based thumbnail pipeline
(core/storage.py), which already handles GIF's palette mode correctly.

OCR is enabled for tutorial/reaction/text-overlay GIFs (see issue #93),
which can contain readable text that's worth extracting.
"""

from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="gif",
    label="Animated GIF",
    thumbnail_source=ThumbnailSource.UPLOADED_FILE,
    ocr_capable=True,
    extensions=frozenset({".gif"}),
    badge_icon="\U0001F4CF",
    badge_text="GIF",
))
