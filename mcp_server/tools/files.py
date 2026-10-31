"""The MCP tools of one area. Files in and out: version, upload, replace_file, search, get (+ its aliases),
download, import, list_import.

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import base64
import binascii
import io
import typing

from mcp_server.app import DOWNLOAD_INLINE_MAX_BYTES, IMPORT_DIR, mcp
from mcp_server.shapes import (TEXT_PREVIEW_CHARS, TEXT_PREVIEW_MAX_CHARS, _meta_fields, _to_public,
                               _to_public_listed, text_chars_arg)
from mcp_server._common import _ingest, _related_listed, _resolve_import_path, _run_in_thread, _viewable_item

from core import access_log, policy, db, errors, items, object_types, revisions, storage, version as version_info
from core import transcripts
from core.errors import InvalidInput, NotFound


@mcp.tool()
def constructicon_version() -> dict:
    """Which build is running: {version, commit, deployed_at, env}. CalVer YYYY.M.D (.N for a
    second release that day), "-dev" on a test instance; "dev" = no version file."""
    return version_info.get_version_info()


@mcp.tool()
def constructicon_upload(filename: str, content_base64: str, description: str = "", tags: list[str] | None = None,
                      uploaded_by: str = db.SOURCE_AUTHORED,
                      source_modified_at: float | None = None, sensitive: bool = False) -> dict:
    """Upload a file (base64) as a new item. Returns the item, with its hotlink url.

    filename: its extension decides the type (.png .pdf .stl .psd .svg .mp3 ...).
    tags: tag names to attach. description: the uploader's note.
    uploaded_by: a free-text Source NOTE (default "Claude — authored"), never who uploaded it: the
      uploader is the token's user (the install token = admin-owned). For migrated content pass
      "Claude — migrated from <source>" (sets channel "migration:<source>").
    source_modified_at: the file's own mtime (unix seconds). When filename, size and this match an
      existing item, that item comes back with "duplicate": true instead.
    sensitive: lock it from the start (admins only).
    OCR runs in the background (ocr_status "pending"; read extracted_text later with
    constructicon_get). An audio file's own tags fill its title and type_metadata at once.
    For big files use constructicon_import.
    """
    content = base64.b64decode(content_base64)
    return _ingest(filename, io.BytesIO(content), len(content), description, tags, uploaded_by, source_modified_at,
                   sensitive=sensitive)


@mcp.tool()
def constructicon_replace_file(slug: str, content_base64: str, filename: str | None = None) -> dict:
    """Replace an item's file bytes and KEEP the item: same slug and links, cards, tags, title,
    captions, relations and revision chain. Use it to revise a file in place.

    content_base64: the new bytes, standard base64 (no data: prefix). One MCP message, so at most
      the upload cap (constructicon_list_import's max_upload_mb); over it: file_too_large, nothing
      decoded. For a bigger file, constructicon_import it and use
      constructicon_revision(old, action="supersede", new=...).
    filename: the NEW file's name (decides the type check); omit to keep the item's name. A new
      extension keeps the stem and swaps the extension.
    Re-processed like an upload (thumbnail, text/OCR in the background, hash, captions, search).
    The old bytes go to the trash for 7 days; undo restores file, size and name.
    Returns the item plus {replaced, batch_id, previous_size, new_size, trash_days, expires_at}.
    Errors: replace_redacted, no_file, bad_base64, empty_file, file_too_large, unsupported_type,
    content_mismatch, replace_type_mismatch (another type: upload a new item and supersede),
    replace_needs_decision, replace_rejected, file_unchanged.
    """
    encoded = "".join((content_base64 or "").split())
    if len(encoded) * 3 // 4 > storage.MAX_BYTES + 3:
        raise errors.AppError("file_too_large", f"The replacement is about {len(encoded) * 3 // 4 / 1048576:.0f} MB "
                              f"as sent, over the {storage.MAX_MB} MB upload limit. Nothing was changed.", status=413,
                              details={"max_bytes": storage.MAX_BYTES})
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise InvalidInput(f"content_base64 is not valid base64 ({e}). Send the raw file bytes, standard base64, "
                           "no data: prefix. Nothing was changed.", code="bad_base64") from e
    result = items.replace_file(slug, content, filename, run_background=_run_in_thread)
    return {**_to_public(result.item), "replaced": True, "batch_id": result.batch_id,
            "previous_size": result.data["previous_size"], "new_size": result.data["new_size"],
            "trash_days": result.data["trash_days"], "expires_at": result.data["expires_at"]}


@mcp.tool()
def constructicon_search(query: str | None = None, tags: list[str] | None = None,
                         text_chars: int = TEXT_PREVIEW_CHARS) -> list[dict]:
    """Search items by name (title, any filename it has had), tags, card titles, note or extracted
    text. Each word matches the START of a word ("tun" finds "Tuning"; a run-together filename is
    split too, so "camino" finds "ElCamino.jpeg"); all words are required; best match first. When
    nothing matches that way, the words run together are looked for INSIDE filenames ("el camino"
    finds "Elcamino4.jpeg"); each result's `matched` says which: "word_start" or "inside_filename"
    (null with no query). query and tags together: both must match.
    tags (any of them, any case) filters on the tag tree: an item carrying one of these tags, or any
    tag under one (a card's own tag counts). Redacted items are left out
    (constructicon_list_items(which="redacted")). The text preview is centred on the first query
    word found. Older revisions are found too, marked superseded_by.
    """
    chars = text_chars_arg(text_chars)
    # #477: old revisions are still found, marked with `superseded_by` (the current revision's slug) and `rev`.
    # #557: db.search applies the policy's browse clause; the per-item policy is checked here too.
    # #762: the filename fallback, and `matched` on every result (never a silent change of meaning).
    return revisions.decorate([
        {**_to_public_listed(r, query, chars),
         "matched": r.get("search_matched", db.SEARCH_MATCHED_WORD_START) if query else None}
        for r in policy.filter_visible(db.search(query=query, tags=tags, filename_fallback=True))])


GET_INCLUDES = ("related", "revisions", "agent_notes")
GET_WINDOW_MAX_CHARS = 50_000  # #722: the most text one windowed constructicon_get returns


def _whole_number(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput(f"{name} must be a whole number.")
    return value


def _text_window(row, text_offset, text_chars):
    """#722: one window of the row's extracted_text, plus where it sits and where the next one starts.
    text_chars None = from text_offset to the end (capped at GET_WINDOW_MAX_CHARS). Refuses an offset
    past the end or a size out of range (bad_request, saying the text's length)."""
    text = row.get("extracted_text") or ""
    total = len(text)
    _whole_number("text_offset", text_offset)
    if text_chars is not None:
        _whole_number("text_chars", text_chars)
        if not 1 <= text_chars <= GET_WINDOW_MAX_CHARS:
            raise InvalidInput(f"text_chars must be from 1 to {GET_WINDOW_MAX_CHARS:,}; this item's text is "
                               f"{total:,} characters.")
    if text_offset < 0 or (text_offset >= total and not (text_offset == 0 and total == 0)):
        raise InvalidInput(f"text_offset {text_offset:,} is outside this item's text ({total:,} characters"
                           + (f": use 0 to {total - 1:,})." if total else ", so only 0 works)."),
                           details={"extracted_text_chars": total})
    end = min(total, text_offset + (GET_WINDOW_MAX_CHARS if text_chars is None else text_chars))
    return {"extracted_text": text[text_offset:end] if text else row.get("extracted_text"),
            "extracted_text_chars": total, "text_offset": text_offset,
            "extracted_text_truncated": text_offset > 0 or end < total,
            "next_offset": end if end < total else None,
            # #789: the moment this window starts at, when it falls in a video's transcript
            **transcripts.cite(row, text_offset)}


@mcp.tool()
def constructicon_get(slug: str,
                      include: list[typing.Literal["related", "revisions", "agent_notes"]] | None = None,
                      text_offset: int = 0, text_chars: int | None = None) -> dict:
    """One item by slug: metadata, hotlink url, its WHOLE extracted_text and type_metadata, and
    `superseded_by` / `rev` (its place in a revision chain).
    Reading a long text by the page: text_chars=N (1..50000) returns only extracted_text[text_offset
    : text_offset+N] (text_offset alone = from there to the end, at most 50000), plus
    extracted_text_chars (the whole length), text_offset, extracted_text_truncated, next_offset
    (where the next page starts; null at the end) and timestamp_seconds / timestamp_url (for a YouTube
    video whose transcript has cue times: the moment that position is said, a &t= link; else null).
    A windowed read also trims big type_metadata values (type_metadata_truncated) as list results do.
    E.g. constructicon_get(slug, text_chars=20000), then text_offset=next_offset.
    include adds, on request: "related" -> `related`, the items linked to it (listed shape);
    "revisions" -> `revisions`, its chain {in_chain, chain: [{slug, title, filename, rev,
    is_current, is_this, redacted}], current, superseded, rev, of}; "agent_notes" ->
    `agent_notes`, the private notes from constructicon_set_agent_notes.
    E.g. constructicon_get(slug, include=["related"]).
    """
    wanted = list(dict.fromkeys(include or []))
    bad = [w for w in wanted if w not in GET_INCLUDES]
    if bad:
        raise InvalidInput(f"include: unknown {', '.join(map(str, bad))}; use {', '.join(GET_INCLUDES)}.")
    windowed = text_chars is not None or text_offset != 0
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    window = _text_window(row, text_offset, text_chars) if windowed else None  # validated before the access log
    policy.note_access(row, access_log.HOW_MCP_GET)  # #604 follow-up 7: sensitive items only
    out = revisions.decorate([_to_public(row)])[0]
    if window is not None:
        out.update(window)
        out.update(_meta_fields(out["type_metadata"], TEXT_PREVIEW_MAX_CHARS))
    if "related" in wanted:
        out["related"] = _related_listed(slug)
    if "revisions" in wanted:
        out["revisions"] = revisions.revision_view(slug)
    if "agent_notes" in wanted:
        out["agent_notes"] = row.get("agent_notes")
    return out


@mcp.tool(alias_of=("constructicon_get", None))
def constructicon_get_related(slug: str) -> list[dict]:
    _viewable_item(slug)
    return _related_listed(slug)


@mcp.tool(alias_of=("constructicon_get", None))
def constructicon_list_revisions(slug: str) -> dict:
    _viewable_item(slug)
    return {"ok": True, **revisions.revision_view(slug)}


@mcp.tool()
def constructicon_download(slug: str) -> dict | None:
    """An item's file bytes, base64, with filename and media_type. Over ~25 MB: metadata and the
    hotlink url instead (content_base64 null); fetch the url.
    Errors: no_file (a link or other file-less item), file_missing (gone from disk).
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557 (the #561 note: restricted downloads are the policy's call)
    if not row.get("stored_filename"):
        raise InvalidInput("This object has no uploaded file to download", code="no_file")
    policy.note_access(row, access_log.HOW_MCP_DOWNLOAD)  # #604 follow-up 7: sensitive items only
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        raise NotFound("File missing on disk", code="file_missing")
    size = path.stat().st_size
    if size > DOWNLOAD_INLINE_MAX_BYTES:
        # Return URL instead of base64 for large files
        return {
            "slug": row["slug"],
            "filename": row["filename"],
            "media_type": row.get("media_type") or "image",
            "size_bytes": size,
            "url": _to_public(row)["url"],
            "content_base64": None,
            "note": f"File is {size // (1024*1024)} MB, over the {DOWNLOAD_INLINE_MAX_BYTES // (1024*1024)} MB inline limit; fetch it from url instead.",
        }
    content = path.read_bytes()
    return {
        "slug": row["slug"],
        "filename": row["filename"],
        "media_type": row.get("media_type") or "image",
        "content_base64": base64.b64encode(content).decode("utf-8"),
    }


@mcp.tool()
def constructicon_import(path: str, description: str = "", tags: list[str] | None = None,
                        uploaded_by: str = db.SOURCE_AUTHORED,
                        source_modified_at: float | None = None, sensitive: bool = False) -> dict:
    """Import a file from the server's import inbox (for files too big to send base64, up to the
    upload limit). path is relative to the inbox, e.g. "drawings/set-A.pdf"; nothing outside it.
    The file is COPIED (it stays in the inbox) and named by its basename. source_modified_at
    defaults to its mtime, so re-importing an unchanged file returns the existing item with
    duplicate: true. Otherwise as constructicon_upload. Error: import_unavailable (no inbox).
    """
    if not IMPORT_DIR.is_dir():
        raise errors.AppError("import_unavailable", f"Import inbox {IMPORT_DIR} is not mounted on this server",
                              status=503)
    p = _resolve_import_path(path)
    if p is None:
        raise NotFound(f"Not a file inside the import inbox: {path}")
    st = p.stat()
    with p.open("rb") as f:
        return _ingest(p.name, f, st.st_size, description, tags, uploaded_by,
                      source_modified_at if source_modified_at is not None else st.st_mtime, sensitive=sensitive)


@mcp.tool()
def constructicon_list_import() -> dict:
    """The files waiting in the import inbox (recursive, hidden ones skipped), sorted by path:
    {inbox, max_upload_mb, files: [{path, size_bytes, modified_at, supported}], truncated?}
    (at most 1000 files). Error: import_unavailable.
    """
    if not IMPORT_DIR.is_dir():
        raise errors.AppError("import_unavailable", f"Import inbox {IMPORT_DIR} is not mounted on this server",
                              status=503)

    files = []
    try:
        for p in sorted(IMPORT_DIR.rglob("*")):
            rel = p.relative_to(IMPORT_DIR)
            if any(part.startswith(".") for part in rel.parts):
                continue
            # Same check constructicon_import applies: only list what it would
            # accept (regular files, symlinks resolving inside the inbox).
            if _resolve_import_path(rel) is None:
                continue
            st = p.stat()
            files.append({
                "path": rel.as_posix(),
                "size_bytes": st.st_size,
                "modified_at": st.st_mtime,
                "supported": object_types.detect_media_type(p.name) is not None,
            })
            if len(files) >= 1000:
                break
    except Exception as e:
        raise errors.AppError("import_error", f"Error listing import inbox: {e}", status=500) from e

    truncated = len(files) >= 1000
    result = {
        "inbox": str(IMPORT_DIR),
        "max_upload_mb": storage.MAX_MB,
        "files": files,
    }
    if truncated:
        result["truncated"] = True
    return result
