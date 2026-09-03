"""Document type spec and registration.

Documents are text/written posts with no visual representation — they use
the NONE thumbnail strategy and are not OCR-capable (they're already text).
"""

from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="document",
    label="Written post",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    badge_icon="\U0001F4DD",
    badge_text="POST",
))
