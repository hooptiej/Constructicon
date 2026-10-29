"""Document type spec and registration.

Documents are text/written posts with no visual representation — they use
the NONE thumbnail strategy and are not OCR-capable (they're already text).
"""

from markupsafe import Markup

from .. import markdown_render
from . import register, ObjectTypeSpec, ThumbnailSource, MetadataField, _preview


def preview(ctx):
    """#449 preview_fn. #471: a document with a body shows that body,
    rendered as safe Markdown (the same renderer as the project page and the
    site export). Without one (#556): "No write-up yet.", never the uploader's
    note (description) or the title standing in for a body; both still show in
    the details panel. Export renders nothing, as it did
    before #449: a document's write-up body is exported separately by
    core/site_export.py, so an extra paragraph here would duplicate/clutter."""
    if ctx.mode != "live":
        return None
    body = (ctx.item.get("type_metadata") or {}).get("body") or ""
    if body.strip():
        return Markup(f'<div class="markdown-body markdown-file">{markdown_render.render(body)}</div>')
    return _preview.text_block(ctx, NO_WRITEUP)


NO_WRITEUP = "No write-up yet. Edit this post's WRITE-UP to write one."


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
    preview_assets=("prose",),  # #471: styles for the rendered body
    properties_fn=get_properties,
    writeup_body_key="body",
    edit_fields=(
        MetadataField(
            key="body",
            label="WRITE-UP",
            input="textarea",
            help_text="Write your document here…",
        ),
    ),
))
