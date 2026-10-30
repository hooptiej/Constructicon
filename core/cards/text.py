"""Face text (#596): set_text (synopsis, flavor), the write-up lead cache, face_text and text_fields.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import card_rules, changes, db, markdown_render
from .base import _changes_from_log, get_card, Result


# --- Face text: synopsis, flavor, the write-up lead (#596) ------------------------------
# The card face's text box holds, in order: the synopsis, else the write-up's lead, else the
# description (never whereabouts or provenance), then the italic flavor line. The lead is a
# CACHE (projects.writeup_lead): refreshed here whenever a write-up's body or a card's
# writeup_slug changes, so building a list of cards never reads a write-up body.

OP_SET_CARD_TEXT = "set_card_text"
FACE_TEXT_MAX = 320  # characters of synopsis / lead / description a face carries


def set_text(card, *, synopsis=..., flavor=..., dry_run=False, actor=None, batch_id=None):
    """Sets or clears the card's face text: `synopsis` (a few sentences; the text box's first
    choice) and `flavor` (one italic line). `...` = leave alone, None or "" = clear. Validated by
    card_rules.validate_card_text (bad_card_text, 422). One imaged row, undoable; setting what is
    already there writes nothing. data: text_fields() of the card after."""
    row = get_card(card)
    fields = card_rules.validate_card_text(synopsis, flavor)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_SET_CARD_TEXT, actor, batch_id, [row["slug"]]) as log:
            log.update("projects", {"id": row["id"]}, fields)
            muts = [{"op": OP_SET_CARD_TEXT, "affected_slugs": [row["slug"]], "mutations": list(log.muts)}]
        fresh = db.get_project(row["id"])
    flat = _changes_from_log(muts)
    return Result(True, flat, [] if flat else ["Nothing changed."], batch_id if flat else None, dry_run,
                  text_fields(fresh))


def writeup_lead(writeup_slug):
    """The cached lead for a write-up document: its opening paragraph(s) as plain text, clamped
    (markdown_render.lead). None when there is no such item, its type has no write-up body, or the
    body is blank / template-only."""
    from .. import object_types  # lazy: the registry imports a lot
    row = db.get_by_slug(writeup_slug) if writeup_slug else None
    body = object_types.writeup_body(row) if row else None
    if not body:
        return None
    return markdown_render.lead(body, FACE_TEXT_MAX) or None


def refresh_writeup_lead(log, writeup_slug):
    """Recomputes projects.writeup_lead for every card whose write-up is `writeup_slug`, on the
    caller's open ImageLog, so the cache change rides in the same change-log row as the edit
    that caused it (one undo restores both). Called by items.update when an item's
    type_metadata changes and by items.delete. A no-op for an item that is nobody's write-up."""
    ids = db.card_ids_with_writeup(writeup_slug)
    if ids:
        lead = writeup_lead(writeup_slug)
        for card_id in ids:
            log.update("projects", {"id": card_id}, {"writeup_lead": lead})


def face_text(card):
    """(text, source) for a card's text box: source is "synopsis", "writeup" (the cached lead),
    "description" or None (empty). Reads only the card row."""
    for source, value in (("synopsis", card.get("synopsis")),
                          ("writeup", card.get("writeup_lead") if card.get("writeup_slug") else None),
                          ("description", card.get("description"))):
        if (value or "").strip():
            return markdown_render.clamp(value, FACE_TEXT_MAX), source
    return "", None


def text_fields(card):
    """The face-text fields as pages and tools return them: the stored synopsis / flavor, the
    cached write-up lead, and what the face shows (`face_text`, `face_text_source`)."""
    text, source = face_text(card)
    return {"synopsis": card.get("synopsis"), "flavor": card.get("flavor"),
            "writeup_lead": card.get("writeup_lead") if card.get("writeup_slug") else None,
            "face_text": text, "face_text_source": source}
