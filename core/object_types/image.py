"""Image type spec and registration.

Images are the simplest object type: the uploaded file itself IS the
thumbnail (UPLOADED_FILE strategy), they're text-extractable via OCR,
and they need no dedicated capture or text-extraction logic module.
"""

from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="image",
    label="Image",
    thumbnail_source=ThumbnailSource.UPLOADED_FILE,
    ocr_capable=True,
    extensions=frozenset({".png", ".jpg", ".jpeg"}),
    badge_icon="\U0001F5BC️",
    badge_text="IMAGE",
))
