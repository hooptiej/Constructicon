"""Document type spec and registration.

Documents are text/written posts with no visual representation — they use
the NONE thumbnail strategy and are not OCR-capable (they're already text).
"""

from . import register, ObjectTypeSpec, ThumbnailSource, MetadataField, _preview


def preview(ctx):
    """#449 preview_fn: text block with the body content (from content_description
    or description as fallback). Export: bare paragraph."""
    text = ctx.item.get("content_description") or ctx.item.get("description") or "No preview available for this content."
    return _preview.text_block(ctx, text)


def get_properties(row):
    """#449 properties_fn: word and character counts from the body."""
    body = (row.get("type_metadata") or {}).get("body") or ""
    if not body:
        return {}
    props = {
        "Words": f"{len(body.split()):,}",
        "Characters": f"{len(body):,}",
    }
    return props


register(ObjectTypeSpec(
    key="document",
    label="Written post",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    badge_icon="\U0001F4DD",
    badge_text="POST",
    preview_fn=preview,
    properties_fn=get_properties,
    edit_fields=(
        MetadataField(
            key="body",
            label="WRITE-UP",
            input="textarea",
            help_text="Write your document here…",
        ),
    ),
))
