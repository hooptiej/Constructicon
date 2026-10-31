"""Helpers more than one tool module calls (#547 step 3; moved verbatim from server.py): server-side
import paths and the ingest wrapper, field-argument checks (`_fields_arg`, `_only_for`), the
"nothing changed" result, and the few helpers two tool modules share (captions to do, a card
delete, retry_ocr, the destructive-hint annotation).
"""

import json
from pathlib import Path

from mcp.types import ToolAnnotations

from mcp_server.app import IMPORT_DIR
from mcp_server.shapes import _to_public, _to_public_listed

from core import actor as actor_ctx, policy, captions, thumbnails, cards, db, ingest, item_title, object_types
from core import ocr, parse_limits
from core.errors import InvalidInput, NotFound


def _resolve_import_path(rel):
    """Resolve a relative path within IMPORT_DIR, rejecting absolute paths
    and escapes (../ or symlinks outside IMPORT_DIR). Returns Path | None."""
    # Reject absolute paths and path traversal attempts
    if Path(rel).is_absolute():
        return None
    candidate = (IMPORT_DIR / rel).resolve()
    # Ensure the resolved path stays within IMPORT_DIR bounds
    try:
        candidate.relative_to(IMPORT_DIR.resolve())
    except ValueError:  # silent-ok: outside IMPORT_DIR = None; both callers refuse or skip it
        # candidate is outside IMPORT_DIR
        return None
    # Verify it's a file and exists
    if not candidate.is_file():
        return None
    return candidate


def _run_in_thread(fn, *args):
    """Run a function in a background daemon thread, carrying the actor context (#560)."""
    actor_ctx.spawn(fn, *args)


def _ingest(filename, fileobj, file_size, description, tags, uploaded_by, source_modified_at, sensitive=False) -> dict:
    """Private helper for file ingestion: delegates to core/ingest.py.

    All post-validation logic is now centralized in the ingest module — this tool
    simply wraps its result for the MCP response format.

    Returns {"slug": ..., ..._to_public fields..., "duplicate": False} on success; a refusal
    raises InvalidInput (code upload_refused), which the tool wrapper turns into the shared
    error shape. May include "pending_decision_id" if the type's pre_store_fn deferred to the
    owner (#448).
    """
    result = ingest.ingest_file(
        fileobj,
        filename,
        size=file_size,
        source=uploaded_by,
        run_background=_run_in_thread,
        description=description,
        tags=tags,
        source_modified_at=source_modified_at,
        sensitive=sensitive,  # #603: set in the INSERT itself
    )

    if result.error:
        raise InvalidInput(result.error, code="upload_refused")
    elif result.duplicate:
        response = {**_to_public(result.row), "duplicate": True}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response
    else:
        response = {**_to_public(result.row), "duplicate": False}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response


# --- #746: helpers the merged tools and their aliases share ------------------------------------

_NOTHING_CHANGED = "Nothing changed."


def _viewable_item(slug):
    """One item row for a read door: not_found when missing OR hidden by the item policy."""
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    return row


def _related_listed(slug):
    """The items related to `slug`, as the item policy lets the caller see them (listed shape)."""
    return [_to_public_listed(r) for r in policy.filter_visible(db.list_related(slug))]


def _written(batch_id):
    """`batch_id` when that batch wrote anything (so undo has something to reverse), else None."""
    return batch_id if batch_id and db.get_change_rows(batch_id=batch_id) else None


def _dedupe(rows):
    """Change rows of several ops in one batch, each once (some ops report the whole batch so far)."""
    out, seen = [], set()
    for r in rows:
        key = json.dumps(r, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


class _Collect:
    """Gathers the changes and warnings of several core ops run under one batch."""

    def __init__(self):
        self.changes, self.warnings = [], []

    def __call__(self, res):
        self.changes.extend(getattr(res, "changes", None) or [])
        self.warnings.extend(w for w in (getattr(res, "warnings", None) or []) if w != _NOTHING_CHANGED)
        return res

    def result(self):
        return {"changes": _dedupe(self.changes), "warnings": list(dict.fromkeys(self.warnings))}


def _fields_arg(fields, spec, tool):
    """Validates a `fields={...}` argument against `spec` ({key: allowed python types}). Key present
    = set it (None clears where the field allows it), key absent = leave it."""
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise InvalidInput("fields must be an object, e.g. {\"highlight\": true}.", code="bad_fields")
    if "agent_notes" in fields:
        raise InvalidInput("agent_notes are written only with constructicon_set_agent_notes.", code="unknown_field")
    unknown = sorted(set(fields) - set(spec))
    if unknown:
        raise InvalidInput(f"Unknown field(s) for {tool}: {', '.join(unknown)}. Allowed: {', '.join(sorted(spec))}.",
                           code="unknown_field", details={"unknown": unknown, "allowed": sorted(spec)})
    for key, value in fields.items():
        types = spec[key]
        if not isinstance(value, types) or (bool not in types and isinstance(value, bool)):
            names = " or ".join("null" if t is type(None) else t.__name__ for t in types)
            raise InvalidInput(f"fields.{key} must be {names}.", code="bad_fields")
    return dict(fields)


def _only_for(label, /, **passed):
    """Refuses parameters that don't apply to the chosen action: {name: (value, default)}.
    `label` is positional-only, so a tool parameter may be called anything (caption_review's `what`)."""
    extra = [k for k, (value, default) in passed.items() if value != default]
    if extra:
        raise InvalidInput(f"{', '.join(extra)} {'does' if len(extra) == 1 else 'do'} not apply to {label}. "
                           "Nothing was changed.")


def _caption_context(row):
    """The cards (and their hobbies) an item sits in, so an agent captions with context."""
    cards_in, hobbies_in = [], []
    for p in db.list_projects_for_post(row["slug"]):
        cards_in.append({"slug": p.get("slug"), "title": p.get("title"), "kind": p.get("kind")})
        for h in db.list_hobbies_for_project(p["id"]):
            if h["name"] not in hobbies_in:
                hobbies_in.append(h["name"])
    return cards_in, hobbies_in


def _needs_caption(limit, include_failed):
    """#588: caption-capable items with no caption yet, with their cards and hobbies."""
    rows = captions.needs_caption(limit=limit, include_failed=include_failed)
    out = []
    for row in rows:
        cards_in, hobbies_in = _caption_context(row)
        out.append({
            "slug": row["slug"],
            "name": item_title.title_of(row),
            "type": row.get("media_type") or "image",
            "filename": row.get("filename"),
            "caption_status": (row.get("type_metadata") or {}).get("auto_caption_status"),
            "projects": cards_in,
            "hobbies": hobbies_in,
        })
    return {"items": out, "total": captions.count_needs_caption(include_failed=include_failed)}


_DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


def _delete_card(card, dry_run, batch_id):
    res = cards.delete_card(card, dry_run=dry_run, batch_id=batch_id)
    return {**res.to_dict(), **res.data}


RESERVED_ACTION = "retry_ocr"  # #746: built into constructicon_run_type_action; no type may declare it


def _retry_ocr(slug):
    """Force OCR to run (or re-run) on an item; refusals are bad_request, as they always were."""
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #746: was missing (the old net never reached it)
    if row["redacted"]:
        raise ValueError("File was redacted — there's no content left to OCR")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.ocr_capable:
        raise ValueError(f"OCR isn't available for {spec.label} content")
    parse_limits.forget(thumbnails.limit_key(row))  # #644: an explicit retry really runs again
    db.set_ocr_status(slug, "pending")
    actor_ctx.spawn(ocr.run_ocr, slug)
    return _to_public(db.get_by_slug(slug))
