"""Item rows (capture_events): insert, read, item fields, redaction, delete-to-trash and the trash.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import json
import time

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import policy
from .conn import get_conn, _row_to_dict
from .labels import channel_for, source_group
from .revisions import _unlink_revision
from .search_index import _search_match


def insert_upload(slug, filename, stored_filename, uploaded_by, description="", tags=None,
                   file_size=None, source_modified_at=None, ocr_status=None,
                   media_type="image", external_url=None, content_description=None, content_date=None,
                   type_metadata=None, sensitive=False, short_title=None):
    """Creates a capture_events row. filename/stored_filename are for uploaded files and can be
    None for content that lives elsewhere (media_type='youtube' + external_url, for example) —
    there's no requirement that a row correspond to an actual file on disk.

    media_type/external_url/content_description/content_date describe the content itself,
    separate from description, which is about how/why the row was captured. See the
    capture_events column comments in SCHEMA for the distinction. type_metadata is a freeform
    dict for whatever per-type properties don't fit those generic columns — see
    core/object_types.py's MetadataField and set_type_metadata below.

    #604 step 1: `uploaded_by_user_id` is the signed-in user of the current actor context
    (users.user_id_for_actor), NULL for the token / MCP / scripts / system = admin-owned. This is
    the ONE place an item row is created, so every path (web upload and content, MCP upload,
    import and add_content, a card's write-up) records it the same way. `uploaded_by` above is
    the free-text Source label (`tech`), unrelated.
    #603: `sensitive=True` (the upload dialog's checkbox) sets the flag in the same INSERT, so the
    item is never visible unlocked, not even for a moment.
    #542: a content_description given at creation is the content's OWN title (a video's, a
    write-up's, a link's; never a caption, which arrives later as a suggestion), so it is also the
    short title unless `short_title` is given or the text isn't name-shaped (one line, at most
    item_title.SHORT_TITLE_MAX characters).
    """
    from .. import actor as actor_ctx, item_title, users  # lazy: users imports this module
    if short_title is None and item_title.title_shaped(content_description, item_title.SHORT_TITLE_MAX):
        short_title = content_description.strip()
    who = actor_ctx.current_actor()
    owner_id = users.user_id_for_actor(who)
    # #560: HOW it arrived, from the context (never from the free-text Source label, except that a
    # label naming a migration source says where migrated content came from). #778: the personal MCP
    # key, only when the uploader is that key's user.
    channel = channel_for(uploaded_by, actor_ctx.current_channel())
    via_token = actor_ctx.token_id_for(who) if owner_id is not None else None
    conn = get_conn()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO capture_events (slug, timestamp, tech, description, "
            "extracted_text, tags, filename, stored_filename, file_size, source_modified_at, ocr_status, ocr_started_at, "
            "media_type, external_url, content_description, content_date, type_metadata, uploaded_by_user_id, "
            "sensitive, sensitive_by, sensitive_at, short_title, channel, uploaded_via_token_id) "
            "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, now, uploaded_by, description,
             "[]", filename, stored_filename, file_size, source_modified_at, ocr_status,
             now if ocr_status == "pending" else None,
             media_type, external_url, content_description, content_date, json.dumps(type_metadata or {}), owner_id,
             1 if sensitive else 0, who if sensitive else None, now if sensitive else None, short_title or None,
             channel, via_token),
        )
        conn.commit()
    finally:
        conn.close()
    # #555: the tags an upload names go into the tag tree (the one store) through the tag service,
    # imaged (op upload_tags); the row's `tags` column is the derived chip cache.
    if tags:
        from .. import tags as tags_svc  # lazy: core.tags imports this module
        tags_svc.attach_on_upload(slug, tags)


def insert_content(slug, uploaded_by, media_type, external_url=None, content_description=None, content_date=None,
                    description="", tags=None, type_metadata=None, sensitive=False):
    """Thin wrapper around insert_upload for rows with no uploaded file — e.g. a YouTube video,
    where the content lives at external_url rather than in local storage. filename/stored_filename
    are left None. ocr_status starts "pending" whenever the type is OCR-capable (see
    core/object_types.py) — same convention as insert_upload's image path — so the background
    OCR pass (against the type's fetched/captured thumbnail, not a local file) picks it up the
    same way an uploaded screenshot does."""
    from .. import object_types  # local import: object_types never needs db, so no cycle, but
    # keeping it out of the module-level imports keeps db.py's own dependency footprint (pure
    # stdlib + sqlite3) obvious at a glance.
    spec = object_types.get_object_type(media_type)
    insert_upload(
        slug, None, None, uploaded_by, description=description, tags=tags,
        media_type=media_type, external_url=external_url,
        content_description=content_description, content_date=content_date,
        ocr_status="pending" if spec.ocr_capable else None,
        type_metadata=type_metadata, sensitive=sensitive,
    )


def _set_type_metadata(slug, metadata):
    """Replaces a row's type_metadata dict wholesale — callers that want to
    merge should read get_by_slug(slug)["type_metadata"] first."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET type_metadata = ? WHERE slug = ?", (json.dumps(metadata or {}), slug))
        conn.commit()
    finally:
        conn.close()


def _update_content_metadata(slug, content_description=None, type_metadata=None, short_title=None):
    """Partial update for the two content-description-shaped fields that
    neither update_tags (description/tags — see
    api_update_image in web/app.py) nor rename_object (display_name,
    #11) cover: content_description itself, and type_metadata.

    Added for #54's full YouTube channel sync: correcting a row's
    site-scraped content_description (used as the video's title — see
    core/db.py's insert_content docstring and object_detail.html) with the
    real title from the YouTube Data API, while also recording
    view/like/comment counts (and, when it isn't the channel owner's own
    upload, the uploading channel's name) in type_metadata.

    Unlike set_type_metadata (wholesale replace — the caller is expected to
    read-modify-write itself if it wants to merge), this MERGES the given
    type_metadata into whatever is already stored, keying on top-level dict
    keys. A correction pass re-writing the same fields with the same values
    is what makes re-running #54's sync script a safe no-op; a merge (not a
    plain replace) also means this can be called by more than one future
    writer for the same row (e.g. a stats-only refresh later) without one
    call's fields clobbering another's.

    content_description=None leaves the existing value alone (same
    None-means-"don't touch" convention as update_tags/rename_object);
    passing "" clears it, same as those two.
    """
    existing = get_by_slug(slug)
    if existing is None:
        return None
    new_content_description = content_description if content_description is not None else existing["content_description"]
    new_short_title = short_title if short_title is not None else existing.get("short_title")  # #542: same rule
    if type_metadata:
        merged = dict(existing.get("type_metadata") or {})
        merged.update(type_metadata)
    else:
        merged = existing.get("type_metadata") or {}
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE capture_events SET content_description = ?, type_metadata = ?, short_title = ? WHERE slug = ?",
            (new_content_description, json.dumps(merged), new_short_title or None, slug),
        )
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def get_by_slug(slug):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM capture_events WHERE slug = ?", (slug,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def find_duplicate(filename, file_size, source_modified_at):
    """A prior capture_event is only treated as a duplicate when filename,
    file size, and the source file's own last-modified time all agree —
    matching just the name or just the size is too easy to collide on by
    coincidence (e.g. two unrelated 'screenshot.png' drops).
    """
    if not filename or file_size is None or source_modified_at is None:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM capture_events WHERE filename = ? AND file_size = ? AND source_modified_at = ? "
            "ORDER BY timestamp ASC LIMIT 1",
            (filename, file_size, source_modified_at),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _rename_object(slug, display_name):
    """Sets an object's display_name override (#11). Passing an empty string clears it back to
    the default title (core/item_title.py) rather than leaving the previous override in place,
    since "" is never a meaningful display name. (#704: the `icon` override is gone.)"""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET display_name = ? WHERE slug = ?", (display_name or None, slug))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _set_provenance(slug, provenance):
    """Sets or clears an object's provenance classification (#341).

    provenance: an ACTIVE key of the editable file list (#529), the row's current value
    (even if retired), or None to clear. Anything else raises
    CardError('bad_provenance') listing the active keys.
    Returns the updated row, or None if not found."""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    from .. import provenance_options
    provenance = provenance_options.validate("file", provenance, current=existing.get("provenance"))
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET provenance = ? WHERE slug = ?", (provenance, slug))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _set_highlight(slug, on: bool):
    """Sets or clears an object's highlight flag (#341).

    on: True to mark as highlighted, False to clear.
    Returns the updated row, or None if not found."""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET highlight = ? WHERE slug = ?", (1 if on else 0, slug))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _set_brand_asset(slug, is_brand, brand_role=None):
    """Sets or clears an object's brand asset status and role (#350).

    is_brand: True to flag as brand asset, False to clear.
    brand_role: optional role label (one of BRAND_ROLES or custom). When is_brand
                is falsey, brand_role is also cleared.
    Returns the updated row, or None if not found."""
    existing = get_by_slug(slug)
    if existing is None:
        return None
    conn = get_conn()
    try:
        if not is_brand:
            # Clear both flag and role when marking as not a brand asset
            conn.execute("UPDATE capture_events SET is_brand_asset = 0, brand_role = NULL WHERE slug = ?", (slug,))
        else:
            conn.execute(
                "UPDATE capture_events SET is_brand_asset = 1, brand_role = ? WHERE slug = ?",
                (brand_role or None, slug)
            )
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def list_brand_assets():
    """Returns all brand assets (where is_brand_asset=1), non-redacted,
    ordered by brand_role then recency (timestamp desc). Each row includes
    slug, brand_role (the title is core/item_title.py's, #542),
    and availability for thumb/file URLs."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE is_brand_asset = 1 AND redacted = 0 "
            "ORDER BY brand_role, timestamp DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# Tag names the Wallpaper surface (#422) gathers: a small set of synonyms rather than one
# canonical tag (the owner's "Desktop Picture" pile and the archive import's "wallpaper" tag mean
# the same thing). Matched case-insensitively. Editable in Admin since #556: the vocab_terms table
# (vocab 'wallpaper_tag', core/vocab.py; seeded with wallpaper + desktop picture).


def list_wallpapers():
    """Objects carrying any wallpaper tag (vocab_terms, vocab 'wallpaper_tag'), non-redacted,
    newest first (#422). The Wallpaper page's data source (and its zip, #780): a tag-based
    sibling of list_brand_assets (wallpaper is just a tag, not a flag)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ce.* FROM capture_events ce "
            "JOIN post_tags pt ON pt.post_slug = ce.slug "
            "JOIN blog_tags t ON t.id = pt.tag_id "
            "WHERE lower(t.name) IN (SELECT term FROM vocab_terms WHERE vocab = 'wallpaper_tag') "
            "AND ce.redacted = 0 ORDER BY ce.timestamp DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_uploaders(query=None):
    """Distinct Source *groups* (see source_group()) matching the given
    filters, each with their total count and most recent capture time — used
    to group the gallery fairly so one prolific uploader can't crowd others
    out of a single global LIMIT. Grouped in Python rather than SQL `GROUP BY
    tech` since Source values are now full sentences (e.g. "Hooptie J (me) —
    manual upload" vs "... — automated upload") and the gallery groups on the
    shorter "who" prefix, not the exact string — row counts here are small
    enough (single-owner site) that this is simpler than fighting SQL string
    slicing.
    """
    conn = get_conn()
    try:
        # #282: these totals sit next to /api/gallery's per-uploader item
        # lists, which come from search() and hide redacted rows -- count
        # the same set so "N items" matches what's actually listed.
        # #603: and the same browse clause search() applies, so a hidden item's description can't be
        # probed through these counts (a ?query= that only matches a sensitive item counts 0).
        # #543: the text filter is the SAME one search() uses (item_search), so a gallery's per-uploader
        # "total" and its listed items agree when a query is given.
        clauses, params = ["ce.redacted = 0" + policy.sql_browse_clause("ce.")], []
        if query:
            match = _search_match(query)
            clauses.append("ce.id IN (SELECT rowid FROM item_search WHERE item_search MATCH ?)" if match else "0")
            if match:
                params.append(match)
        where = f"WHERE {' AND '.join(clauses)}"
        rows = conn.execute(f"SELECT ce.tech AS tech, ce.timestamp AS timestamp FROM capture_events ce {where}",
                            params).fetchall()
        groups = {}
        for r in rows:
            key = source_group(r["tech"])
            g = groups.setdefault(key, {"uploaded_by": key, "total": 0, "most_recent": 0})
            g["total"] += 1
            g["most_recent"] = max(g["most_recent"], r["timestamp"])
        return sorted(groups.values(), key=lambda g: g["most_recent"], reverse=True)
    finally:
        conn.close()


def source_label_exists(label):
    """#560: True when any un-redacted item's Source label is `label` or starts with it as its group
    prefix (the same match as search(uploaded_by=)). Counts hidden items too: it only says the label
    is a real one, so its gallery explains an empty list instead of calling the label unknown."""
    if not label:
        return False
    conn = get_conn()
    try:
        return conn.execute("SELECT 1 FROM capture_events WHERE redacted = 0 AND (tech = ? OR tech LIKE ? OR tech LIKE ?) "
                            "LIMIT 1", (label, f"{label} —%", f"{label} -%")).fetchone() is not None
    finally:
        conn.close()


def list_restricted():
    """#443: every non-redacted row of a restricted type, newest first, for
    the admin pane's restricted list (and the MCP). #603: plus every row flagged
    "This is sensitive" (capture_events.sensitive), of any type."""
    from core import object_types  # lazy: type modules import db
    keys = object_types.restricted_types()
    type_clause = f"media_type IN ({', '.join('?' * len(keys))}) OR " if keys else ""
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM capture_events WHERE redacted = 0 AND ({type_clause}sensitive = 1) "
            "ORDER BY timestamp DESC",
            list(keys),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _mark_redacted(slug):
    """File removed (sensitive content), metadata kept for future correlation.

    The caller (web/app.py's /api/image/{slug}/redact, the
    constructicon_redact MCP tool) deletes the stored file first; this also
    clears stored_filename to NULL -- the row's one authoritative "is there
    a real file right now" signal, since #282's unmark_redacted can flip
    `redacted` back to 0 without ever being able to restore the actual file.
    Every has_thumb/is_file-style check in web/app.py keys off
    stored_filename for exactly that reason, not off `redacted` (which was
    a sufficient proxy before un-redact existed, and stopped being one the
    moment a row could become un-redacted-but-still-fileless).

    filename (the original human-readable name) is deliberately left alone
    -- "metadata kept for future correlation" means that one, not the
    internal storage path, which has no display purpose once the file it
    pointed to is gone. Since #282 a redacted row is hidden from every
    list/browse/search query and reachable only by its direct /object/<slug>
    link or list_redacted()."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET redacted = 1, stored_filename = NULL WHERE slug = ?", (slug,))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def _unmark_redacted(slug):
    """Reverse of mark_redacted (#282): clears the flag so the row rejoins
    ordinary browsing/search. Deliberately does NOT touch stored_filename --
    it's already NULL (mark_redacted cleared it) and stays that way, since
    the file itself was deleted and can't be restored. That's what keeps
    web/app.py's has_thumb/is_file checks correctly file-less after this
    call despite `redacted` now being 0 again."""
    conn = get_conn()
    try:
        conn.execute("UPDATE capture_events SET redacted = 0 WHERE slug = ?", (slug,))
        conn.commit()
        return get_by_slug(slug)
    finally:
        conn.close()


def list_redacted():
    """Every currently-redacted row, most recently uploaded first (#282).
    The admin pane's "Redacted items" view -- once redacted rows are hidden
    from every other list this is the only place they can be found again
    without already knowing the slug. There's no redacted_at column, so
    this can't say *when* a row was redacted; the audit log has the web
    route's call if that matters."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE redacted = 1 ORDER BY timestamp DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _delete_upload(slug):
    """Full delete — removes the metadata row entirely. Caller is responsible
    for deleting the actual file(s) from storage first.

    Also cleans up every row elsewhere in the schema that references this
    slug — capture_event_relations (already handled here before #16),
    plus project_items and post_tags (#16 — flagged by #1's PR review as a
    pre-existing gap: deleting a slug left its curated-project membership
    and tag links behind as orphans, invisible but never cleaned up since
    nothing ever queries project_items/post_tags for a slug that no longer
    has a capture_events row)."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM capture_events WHERE slug = ?", (slug,))
        conn.execute("DELETE FROM capture_event_relations WHERE slug_a = ? OR slug_b = ?", (slug, slug))
        # #477: take the item out of its revision chain, closing the gap (A -> B -> C minus B = A -> C).
        _unlink_revision(conn, slug)
        conn.execute("DELETE FROM project_items WHERE post_slug = ?", (slug,))
        conn.execute("DELETE FROM post_tags WHERE post_slug = ?", (slug,))
        # #240: an open "which project?" question about a row that no longer
        # exists is just noise in the admin pane's queue.
        conn.execute("DELETE FROM pending_decisions WHERE post_slug = ?", (slug,))
        conn.commit()
    finally:
        conn.close()


# --- Item service reads + trash (#541 phase B; the writes go through ImageLog in core/items.py) ---

def item_references(slug):
    """Every row elsewhere that an item delete removes or re-links, as image keys:
    {relations: [(slug_a, slug_b)], successor, predecessor, project_items: [project_id],
    post_tags: [tag_id], blog_entry_items: [entry_id], decisions: [id]}. Same set
    delete_upload cleans, plus the blog-entry links it missed."""
    conn = get_conn()
    try:
        succ = conn.execute("SELECT new_slug FROM item_revisions WHERE old_slug = ?", (slug,)).fetchone()
        pred = conn.execute("SELECT old_slug FROM item_revisions WHERE new_slug = ?", (slug,)).fetchone()
        return {
            "relations": [(r["slug_a"], r["slug_b"]) for r in conn.execute(
                "SELECT slug_a, slug_b FROM capture_event_relations WHERE slug_a = ? OR slug_b = ? ORDER BY rowid",
                (slug, slug))],
            "successor": succ["new_slug"] if succ else None,
            "predecessor": pred["old_slug"] if pred else None,
            "project_items": [r["project_id"] for r in conn.execute(
                "SELECT project_id FROM project_items WHERE post_slug = ? ORDER BY rowid", (slug,))],
            "post_tags": [r["tag_id"] for r in conn.execute(
                "SELECT tag_id FROM post_tags WHERE post_slug = ? ORDER BY rowid", (slug,))],
            "blog_entry_items": [r["entry_id"] for r in conn.execute(
                "SELECT entry_id FROM blog_entry_items WHERE post_slug = ? ORDER BY rowid", (slug,))],
            "decisions": [r["id"] for r in conn.execute(
                "SELECT id FROM pending_decisions WHERE post_slug = ? ORDER BY id", (slug,))],
        }
    finally:
        conn.close()


def get_embedding(slug):
    conn = get_conn()
    try:
        row = conn.execute("SELECT embedding FROM capture_events WHERE slug = ?", (slug,)).fetchone()
        return row["embedding"] if row else None
    finally:
        conn.close()


def get_trash_row(batch_id, slug):
    """One trash entry, embedding included (None when absent)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM trash WHERE batch_id = ? AND slug = ?", (batch_id, slug)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _set_trash_embedding(batch_id, slug, embedding):
    conn = get_conn()
    try:
        conn.execute("UPDATE trash SET embedding = ? WHERE batch_id = ? AND slug = ?", (embedding, batch_id, slug))
        conn.commit()
    finally:
        conn.close()


def list_trash(expired_before=None, holds=None):
    """Unpurged trash entries, oldest first (no embedding). `expired_before` (epoch) keeps only
    those whose expires_at is at or before it. `holds`: None = all, False = ordinary deletes only
    (expires_at set), True = redact holds only (expires_at NULL: kept until the owner decides)."""
    conn = get_conn()
    try:
        sql = ("SELECT batch_id, slug, stored_filename, has_original, has_thumb, dir, size_bytes, title, reason, "
               "created_at, expires_at FROM trash WHERE purged_at IS NULL")
        args = []
        if holds is True:
            sql += " AND expires_at IS NULL"
        elif holds is False:
            sql += " AND expires_at IS NOT NULL"
        if expired_before is not None:
            sql += " AND expires_at IS NOT NULL AND expires_at <= ?"
            args.append(expired_before)
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at, slug", args)]
    finally:
        conn.close()


def get_redact_hold(slug):
    """The live redact hold for `slug` (file still stored in the trash, no expiry), else None."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM trash WHERE slug = ? AND reason = 'redact' AND expires_at IS NULL "
                           "AND purged_at IS NULL ORDER BY created_at DESC LIMIT 1", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def redact_hold_slugs():
    conn = get_conn()
    try:
        return {r["slug"] for r in conn.execute(
            "SELECT slug FROM trash WHERE reason = 'redact' AND expires_at IS NULL AND purged_at IS NULL")}
    finally:
        conn.close()


def _mark_trash_purged(batch_id, slug, when):
    """The entry's files are gone for good; the row stays as a record (and makes undo refuse)."""
    conn = get_conn()
    try:
        conn.execute("UPDATE trash SET purged_at = ?, embedding = NULL WHERE batch_id = ? AND slug = ?",
                     (when, batch_id, slug))
        conn.commit()
    finally:
        conn.close()


def list_all_item_files():
    """[{slug, stored_filename}] for EVERY item row (redacted and brand assets included)."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT slug, stored_filename FROM capture_events ORDER BY id")]
    finally:
        conn.close()


def blank_document_body(slug):
    """True when `slug` is a document whose body is empty/whitespace (a blank write-up)."""
    row = get_by_slug(slug)
    if row is None or row.get("media_type") != "document":
        return False
    return not ((row.get("type_metadata") or {}).get("body") or "").strip()


def media_type_counts():
    """Return a list of dicts {media_type, count} for all non-redacted items,
    grouped by media_type, ordered by count descending. Used by the storage
    stats panel (#352) to show file type distribution."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT media_type, COUNT(*) as count FROM capture_events WHERE redacted = 0 GROUP BY media_type ORDER BY count DESC"
        ).fetchall()
        return [{"media_type": row["media_type"], "count": row["count"]} for row in rows]
    finally:
        conn.close()


def get_by_slugs(slugs):
    """Full item rows (the get_by_slug shape) for `slugs`, in the order given; unknown slugs are
    left out. Read in chunks (SQLite's parameter limit)."""
    slugs = list(dict.fromkeys(slugs))
    found = {}
    conn = get_conn()
    try:
        for i in range(0, len(slugs), 500):
            chunk = slugs[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT * FROM capture_events WHERE slug IN ({marks})", chunk):
                found[r["slug"]] = _row_to_dict(r)
    finally:
        conn.close()
    return [found[s] for s in slugs if s in found]
