"""Audio type spec and registration.

Audio files are file uploads with no visual thumbnail frame. The object
detail page renders a <audio controls> mini player instead of a thumbnail
image (see web/app.py's is_audio_file and object_detail.html), and gallery
tiles fall back to the generic file icon with this type's badge overlaid.
"""

from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="audio",
    label="Audio file",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    extensions=frozenset({".mp3", ".m4a", ".ogg", ".wav"}),
    badge_icon="\U0001F3B5",
    badge_text="AUDIO",
))
