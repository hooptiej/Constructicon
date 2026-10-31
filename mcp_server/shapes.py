"""The response shapers every tool shares (#547 step 3; moved verbatim from server.py): `_to_public`
(one item, whole), `_to_public_listed` (#697: an item in a list, with the text preview), the type
metadata trim, and the card, hobby and blog entry shapes.
"""

import json
import re
import unicodedata

from mcp_server.app import BASE_URL

from core import mcp_tokens, policy, users, cards, db, item_dates, item_title, object_types, timeline
from core import tags as tags_svc, transcripts
from core.errors import InvalidInput


def _to_public(row):
    spec = object_types.get_object_type(row.get("media_type"))
    return {
        "slug": row["slug"],
        "url": f"{BASE_URL}/f/{row['slug']}",
        "filename": row["filename"],
        # display_name (#11) here is the item's one canonical title (core/item_title.py, #542):
        # rename, then short title, then filename, then slug. "icon" is the type's badge (#704: the
        # per-object icon override column is gone).
        "display_name": item_title.title_of(row),
        "icon": spec.badge_icon,
        "media_type": row.get("media_type") or "image",
        "description": row["description"],
        "content_description": row.get("content_description"),  # #588: the item's long caption / description
        "short_title": row.get("short_title"),  # #542: the short name; the long caption is never the title
        "tags": tags_svc.visible_chip_names(row["tags"]),  # #555: the derived chips, none naming a hidden card
        "redacted": bool(row["redacted"]),
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        # #261: per-type metadata bag (auto_caption/auto_caption_status,
        # YouTube view/like/comment counts, ID3 tags, ...) -- previously
        # invisible through every MCP tool. Same "always a dict, never
        # None/missing" contract as web/app.py's _to_object_detail;
        # core/db.py's _row_to_dict already parses the JSON column, so
        # this is only a guard against a stored JSON null.
        "type_metadata": row.get("type_metadata") or {},
        "timestamp": row["timestamp"],
        "content_date": row.get("content_date"),
        "display_date_override": row.get("display_date_override"),
        # #725: the REAL date or None (undated: the upload `timestamp` above is never the item's date);
        # date_shown is what its card says ("≈ <range> · from <card>" when it inherits its card's range).
        "effective_date": timeline.resolve_item_date(row),
        "date_shown": item_dates.item_date(row)["label"],
        # Curator (#341): provenance classification + highlight flag, so every
        # MCP object read (get_project items, search, list) carries them.
        "provenance": row.get("provenance"),
        "highlight": bool(row.get("highlight")),
        # Brand assets (#350): is_brand_asset flag + optional brand_role label,
        # so every MCP object read carries brand kit metadata.
        "is_brand_asset": bool(row.get("is_brand_asset")),
        "brand_role": row.get("brand_role"),
        # #448: per-type actions available on this object (e.g. YouTube's "fetch real date" moves here in PR 2).
        # The type file owns the handler; actions are declared in ObjectTypeSpec.actions.
        "actions": [a.key for a in spec.actions],
        # #604 step 1: the user who uploaded it ({id, username, name}); null = admin-owned (the
        # install token, the MCP, a script). #603: the sensitive lock and why (type or flag).
        "uploaded_by_user": users.owner_info(row.get("uploaded_by_user_id")),
        # #778: the personal MCP key it was uploaded with, as {"text": "<user>'s <token name>", ...}
        # (null = none: the web, the install token). #560: HOW it arrived (web | mcp | migration:<source>
        # | ...); `uploaded_by` above stays the free-text Source note, never who uploaded it.
        "uploaded_via": mcp_tokens.credit(row.get("uploaded_by_user_id"), row.get("uploaded_via_token_id")),
        "channel": row.get("channel"),
        "source_note": row.get("tech"),
        "sensitive": policy.restriction_reason(row) is not None,
        "sensitive_reason": policy.restriction_reason(row),
    }


# #697: a tool that returns MANY items sends a short preview of each one's extracted_text (OCR,
# document text, a transcript can each be hundreds of KB), never the whole text. One item's full
# text comes from constructicon_get. The preview is a window around the first word of the query
# found in the text (word-start, case and accents folded, like the search itself), else its start.
TEXT_PREVIEW_CHARS = 300
TEXT_PREVIEW_MAX_CHARS = 5000
_ELLIPSIS = "…"


def _fold(text):
    """Lowercased, accent-folded, SAME LENGTH as `text` (so a match offset is an offset in `text`)."""
    if text.isascii():
        return text.lower()
    return "".join((unicodedata.normalize("NFKD", c)[:1] or c).lower()[:1] or c for c in text)


def _first_hit(text, query):
    """Offset of the first word-start match of any query word in `text`, or None."""
    words = db._SEARCH_WORD_RE.findall(query or "")[:db._SEARCH_MAX_WORDS]
    if not words or not text:
        return None
    folded = _fold(text)
    best = None
    for w in {_fold(w) for w in words}:
        m = re.search(r"(?<![^\W_])" + re.escape(w), folded)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def text_chars_arg(text_chars):
    """Validate a tool's `text_chars`: 0 (no text) up to TEXT_PREVIEW_MAX_CHARS; more is clamped."""
    if text_chars is None:
        return TEXT_PREVIEW_CHARS
    if isinstance(text_chars, bool) or not isinstance(text_chars, int) or text_chars < 0:
        raise InvalidInput(f"text_chars must be a whole number from 0 to {TEXT_PREVIEW_MAX_CHARS}.")
    return min(text_chars, TEXT_PREVIEW_MAX_CHARS)


def _text_preview(text, query=None, chars=TEXT_PREVIEW_CHARS):
    """{extracted_text: the preview, extracted_text_chars: full length, extracted_text_truncated}.
    The preview is at most `chars` characters of the text, plus "…" at each end that was cut."""
    full = len(text or "")
    if text is None or chars <= 0:
        return {"extracted_text": None if text is None or full else text,
                "extracted_text_chars": full, "extracted_text_truncated": full > 0}
    if full <= chars:
        return {"extracted_text": text, "extracted_text_chars": full, "extracted_text_truncated": False}
    hit = _first_hit(text, query)
    start = 0 if hit is None else min(max(0, hit - chars // 3), full - chars)
    end = start + chars
    preview = (_ELLIPSIS if start > 0 else "") + text[start:end] + (_ELLIPSIS if end < full else "")
    return {"extracted_text": preview, "extracted_text_chars": full, "extracted_text_truncated": True}


# #722: a multi-item result trims big type_metadata values too (a write-up's `body`, a video's
# `transcript` and its cue list can each be hundreds of KB). A text value longer than the preview is
# cut to it (with "…"); any other value (a list, an object) over META_VALUE_MAX_CHARS as JSON is
# left out. `type_metadata_truncated` says whether anything was, `type_metadata_cut_keys` which keys.
# constructicon_get(slug) still returns the whole type_metadata.
META_VALUE_MAX_CHARS = 1000


def _trim_meta(meta, chars=TEXT_PREVIEW_CHARS):
    """(type_metadata for a list result, [keys cut or left out])."""
    limit = max(chars or 0, TEXT_PREVIEW_CHARS)
    out, cut = {}, []
    for key, value in (meta or {}).items():
        if isinstance(value, str):
            if len(value) > limit:
                out[key] = value[:limit] + _ELLIPSIS
                cut.append(key)
            else:
                out[key] = value
        elif isinstance(value, (list, dict)) and len(json.dumps(value, default=str)) > META_VALUE_MAX_CHARS:
            cut.append(key)
        else:
            out[key] = value
    return out, cut


def _meta_fields(meta, chars=TEXT_PREVIEW_CHARS):
    """{type_metadata, type_metadata_truncated[, type_metadata_cut_keys]} for a trimmed answer."""
    trimmed, cut = _trim_meta(meta, chars)
    out = {"type_metadata": trimmed, "type_metadata_truncated": bool(cut)}
    if cut:
        out["type_metadata_cut_keys"] = cut
    return out


def _to_public_listed(row, query=None, text_chars=TEXT_PREVIEW_CHARS):
    """_to_public for an item in a multi-item result: extracted_text is a preview (#697), big
    type_metadata values are trimmed (#722), and a transcript hit carries its moment (#789:
    timestamp_seconds / timestamp_url for the first query word found in the transcript)."""
    pub = _to_public(row)
    out = {**pub, **_meta_fields(pub["type_metadata"], text_chars),
           **_text_preview(row.get("extracted_text"), query, text_chars)}
    if query and transcripts.has_cues(row):
        hit = _first_hit(row.get("extracted_text"), query)
        stamp = transcripts.cite(row, hit)
        if stamp["timestamp_seconds"] is not None:
            out.update(stamp)
    return out


def _to_public_project(project):
    items = db.list_project_items(project["id"])
    effective_start, effective_end = cards.card_span(project, items)  # #772: groups roll up members
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        **cards.status_fields(project),
        **cards.whereabouts_fields(project),
        "cover_slug": project.get("cover_slug"),
        "writeup_slug": project.get("writeup_slug"),
        "parent_id": project.get("parent_id"),
        "created_at": project["created_at"],
        "created_by_user": users.owner_info(project.get("created_by_user_id")),  # #604 step 1 (null = admin-owned)
        "created_via": mcp_tokens.credit(project.get("created_by_user_id"), project.get("created_via_token_id")),  # #778
        "start_date_override": project.get("start_date_override"),
        "end_date_override": project.get("end_date_override"),
        "effective_start": effective_start,
        "effective_end": effective_end,
    }


def _hobby_creator(hobby_id):
    """#778: {created_by_user, created_via} for a hobby (null = admin-owned / no personal key)."""
    hp = db.hobby_privacy(hobby_id)
    return mcp_tokens.creator_fields(hp["created_by_user_id"], hp["created_via_token_id"])


def _to_public_blog_entry(entry):
    """Full detail shape for a blog entry, including hydrated projects and items."""
    projects = policy.filter_visible_cards(db.list_entry_projects(entry["id"]))  # #604 step 3
    items = policy.filter_visible(db.list_entry_items(entry["id"]))  # #557
    return {
        "id": entry["id"],
        "slug": entry["slug"],
        "title": entry["title"],
        "subtitle": entry.get("subtitle", ""),
        "body": entry.get("body", ""),
        "status": entry["status"],
        "cover_slug": entry.get("cover_slug"),
        "content_date": entry.get("content_date"),
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
        # #604 / #778: {created_by_user, created_via} ("<user>'s <token name>" for a personal key)
        **mcp_tokens.creator_fields(entry.get("created_by_user_id"), entry.get("created_via_token_id")),
        # Carry the per-attachment note + sort_order (the reshaping helpers
        # drop them, but they're the point of the attachment).
        "projects": [{**_to_public_project(p), "note": p.get("note", ""), "sort_order": p.get("sort_order")} for p in projects],
        "items": [{**_to_public_listed(i), "note": i.get("note", ""), "sort_order": i.get("sort_order")} for i in items],
    }
