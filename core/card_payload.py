"""Slim item records for the pages that embed a grid of item cards as inline JSON (#517).

Home's Files panel, /unfiled, the user gallery and the hobby page's loose objects render their
cards client-side (static/js/cards.js ItemCards), from items embedded in the HTML. The full
public record (web/app.py _to_public) is ~2.1 KB per item, 70% of it OCR text and
type_metadata that no card shows in full. CARD_ITEM_FIELDS is the whitelist of what the card
renderer and the pages' sort / filter / bulk scripts read; scripts/test_home_payload.py keeps it
in step with the JS. Dependency-free on purpose (the test imports it without the web stack).
"""

CARD_ITEM_FIELDS = (
    "slug", "display_name", "media_type", "type_label", "type_icon", "type_badge",
    "card_date", "thumb_url", "has_thumbnail", "redacted", "highlight",
    "stacked",  # #596: title of the card the file is on (the face's "Stacked · <card>"); provenance left the face
    "client", "uploaded_by_display", "uploaded_at", "tags", "codes",
    "ocr_status", "extracted_text", "caption_capable", "type_metadata",
    "superseded_by", "rev",  # #477: revision chain (slug of the current revision | None, 1-based position | None)
)
# Lamp tooltips (OCR text, auto-caption) are a hover hint, not the document: clip them.
CARD_TOOLTIP_CHARS = 100
# The only type_metadata keys the card reads (thumbnail rotation, caption lamp).
CARD_TYPE_METADATA_KEYS = ("rotation", "auto_caption_status", "auto_caption")


def clip_tooltip(text):
    if not isinstance(text, str) or len(text) <= CARD_TOOLTIP_CHARS:
        return text
    return text[:CARD_TOOLTIP_CHARS].rstrip() + "…"


def card_item_public(it):
    """Project one _public_items() record down to what item cards need."""
    out = {k: it.get(k) for k in CARD_ITEM_FIELDS}
    out["extracted_text"] = clip_tooltip(out["extracted_text"])
    tm = it.get("type_metadata") or {}
    slim = {k: tm[k] for k in CARD_TYPE_METADATA_KEYS if k in tm}
    if "auto_caption" in slim:
        slim["auto_caption"] = clip_tooltip(slim["auto_caption"])
    out["type_metadata"] = slim
    return out
