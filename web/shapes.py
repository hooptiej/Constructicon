"""Response shapers shared by the routers (#547): the `_to_*` functions that turn DB rows
into the dicts pages and the JSON API return, plus the small pure helpers they use.
Moved verbatim from web/app.py."""

from core import captions, card_payload, cards, db, object_types, revisions, storage, timeline


def _has_thumbnail(row, spec=None):
    """Whether `row` should expose a /f/<slug>/thumb URL at all — true for
    any type whose spec has a thumbnail strategy (see core/object_types.py),
    even if the thumbnail hasn't actually been produced yet (get_thumbnail
    below fetches/generates it lazily on first request). False for types
    with ThumbnailSource.NONE — a plain document post (no file at all) or,
    since #28, an uploaded audio file (a real file, but no visual frame to
    show as a thumbnail). Dispatches purely off the spec rather than
    short-circuiting on "row has a filename", since that assumption (true
    for image/pdf/stl/psd/svg/eps, whose uploaded-file types always have
    *some* visual to show) no longer holds once a type can have a stored
    file with nothing image-like to derive a thumbnail from.

    A CAPTURE-strategy type with no `capture_fn` yet (e.g. `url` — see
    core/object_types/url.py) can never actually produce one, regardless of
    thumbnail_source, so it's excluded here too rather than optimistically
    claiming a /f/<slug>/thumb URL that will only ever 404 (#197)."""
    spec = spec or object_types.get_object_type(row.get("media_type"))
    if spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE and spec.capture_fn is None:
        return False
    if spec.has_thumbnail_fn is not None:
        # #478: the type knows per row (e.g. a .docx saved without a preview)
        try:
            return bool(spec.has_thumbnail_fn(row))
        except Exception as e:
            print(f"has_thumbnail_fn failed for {row.get('slug')}: {e!r}", flush=True)
            return False
    return spec.thumbnail_source != object_types.ThumbnailSource.NONE


def _should_advertise_thumb(row, spec=None):
    """Whether a card/detail shape should expose thumb_url. The type must have
    a thumbnail strategy (_has_thumbnail), AND the thumbnail must be obtainable:
    an UPLOADED_FILE type needs its stored file present, but FETCH_URL/CAPTURE
    types derive or fetch the thumbnail lazily (the /f/<slug>/thumb route does
    it on first request) even with NO stored_filename — e.g. youtube. The old
    blanket `and bool(stored_filename)` guard (#222) wrongly hid youtube
    thumbnails (no stored file, but a perfectly fetchable img.youtube.com thumb),
    leaving broken cards."""
    spec = spec or object_types.get_object_type(row.get("media_type"))
    if not _has_thumbnail(row, spec):
        return False
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        return bool(row.get("stored_filename"))
    return True


def _to_public(row):
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    # A row with no stored_filename has no file left, and /f/<slug>/thumb
    # answers 410/404 for it -- don't advertise a thumbnail URL that can't
    # be fetched (#222). Keyed on stored_filename, not `redacted`: a
    # currently-redacted row has stored_filename cleared by
    # db.mark_redacted (#282), but so does a row that WAS redacted and got
    # un-redacted since -- unmark_redacted only restores visibility, never
    # the file itself, so `redacted` alone stopped being a reliable "no
    # file" signal the moment that became possible. Matches what the
    # project-card shape below already does; the card templates also check
    # this themselves, so this is about the API shape being consistent for
    # any consumer that doesn't.
    has_thumb = _should_advertise_thumb(row, spec)
    return {
        "slug": row["slug"],
        "url": f"/f/{row['slug']}",
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb else None,
        "filename": row["filename"],
        # filename is None for content-only rows (youtube/document posts —
        # see insert_content in core/db.py); the gallery cards need
        # something readable to show in its place rather than the literal
        # string "null".
        # row["display_name"] (#11) is a per-object override — set via
        # /api/image/<slug> or the constructicon_rename MCP tool, or seeded
        # from an audio file's own tag title at upload time (#255, see
        # core/embedded_metadata.py for why the title lands here and not
        # only in content_description) — that takes priority over the old
        # filename/content_description/slug fallback chain when present.
        "display_name": row.get("display_name") or row["filename"] or row.get("content_description") or row["slug"],
        # File-kind badge (issue #12) — driven entirely by the type's
        # ObjectTypeSpec (core/object_types.py) so gallery cards never need
        # an if/else on media_type; a new type registered there picks up a
        # badge automatically. row["icon"] (#11) is a per-object override
        # that takes priority over the type's generic badge_icon.
        "media_type": media_type,
        "type_label": spec.label,
        "type_icon": row.get("icon") or spec.badge_icon,
        "type_badge": spec.badge_text,
        # Whether the gallery/home cards should render the actual thumbnail
        # image (thumb_url above) or a generic file icon — driven by the
        # type's spec (same _has_thumbnail used server-side for the detail
        # page and project covers), not a hardcoded filename-extension
        # check, so a type with a generated thumbnail (a PDF's rendered
        # first page, once a stream/URL capture is wired up) picks this up
        # for free instead of always falling back to the file icon.
        "has_thumbnail": has_thumb,
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        "uploaded_at": row["timestamp"],
        # content_date (#265): the content's own real-world date (EXIF
        # capture time, a video's creation_time, a YouTube publishedAt),
        # distinct from uploaded_at above — omitted here until now, even
        # though the MCP server's equivalent shape already included it.
        "content_date": row.get("content_date"),
        # uploaded_by keeps the exact Source string (identity/filter key —
        # used by /api/gallery and search); uploaded_by_display is the short
        # grouping label (see core/db.py's source_group()) for compact card
        # UI, so a long Source sentence like "Hooptie J (me) — manual upload"
        # doesn't overflow a gallery card's small meta line. The full string
        # is only spelled out in full on the object detail page.
        "uploaded_by": row["tech"],
        "uploaded_by_display": db.source_group(row["tech"]),
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        # #248: lets the gallery/list cards' caption lamp tell "captionable
        # but never attempted" (grey dot) apart from "not a captionable type"
        # (no dot at all). ocr_status already carries that distinction for
        # OCR via None-means-not-capable (set at insert time); captions keep
        # their status in type_metadata, where absent is ambiguous without
        # this. Same gate the detail page's caption panel uses.
        "caption_capable": captions.should_caption(spec),
        "artifact_link": row["artifact_link"],
        "type_metadata": row.get("type_metadata", {}),
        # Curator (#341): object provenance classification + highlight flag, so
        # every object response (gallery/detail/update) carries them for the UI
        # and the completeness scoring/export to read.
        "provenance": row.get("provenance"),
        "highlight": bool(row.get("highlight")),
        # #514: the item card's date line is the item's own effective date (same
        # resolver as the project detail page's file cards), in the same label format.
        "card_date": cards._day_label(timeline.resolve_item_date(row)),
    }


def _public_items(rows):
    """_to_public for a whole grid, plus the hobby group codes carried by each item's tags
    (item cards, #514). The tag -> code map is read once per grid, not once per row."""
    code_map = db.hobby_codes_by_tag_name()
    out = []
    for r in rows:
        it = _to_public(r)
        it["codes"] = [code_map[t] for t in (it["tags"] or []) if t in code_map]
        out.append(it)
    return revisions.decorate(out)  # #477: superseded_by / rev for the card badge


# #517: the item grids (home Files panel, /unfiled, user gallery, hobby loose objects) embed
# their items as inline JSON. They get the slim card_payload projection, not the full
# _to_public record (see core/card_payload.py for the whitelist).
def _card_items(rows):
    """_public_items(rows) slimmed for embedding in a page (#517)."""
    return [card_payload.card_item_public(it) for it in _public_items(rows)]


def _split_revisions(rows, show_all):
    """#477: browse grids list only the CURRENT revision of each chain. Returns (rows to show,
    how many superseded rows the grid has); with show_all the older revisions stay (their cards
    carry a Superseded badge). `rows` are capture_events dicts fetched WITH superseded items."""
    sup = db.superseded_slugs()
    if not sup:
        return rows, 0
    n = sum(1 for r in rows if r["slug"] in sup)
    return (rows if show_all else [r for r in rows if r["slug"] not in sup]), n



def _friendly_date(epoch):
    """'%-d'-style formatting (no leading zero) without relying on the
    platform-specific %-d/%-e strftime extension, which isn't available on
    Windows — this runs cross-platform.

    Renders in timeline.LOCAL_TIMEZONE (Mountain Time), not the process's
    own system timezone (UTC inside this app's container) — a bare
    datetime.fromtimestamp(epoch) here previously showed an 18:31 MDT photo
    as the next calendar day."""
    if not epoch:
        return None
    dt = timeline.epoch_to_local(epoch)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _friendly_datetime(epoch):
    if not epoch:
        return None
    dt = timeline.epoch_to_local(epoch)
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{_friendly_date(epoch)} at {hour12}:{dt.minute:02d} {ampm}"


def _datetime_local_value(epoch):
    """'%Y-%m-%dT%H:%M'-shaped string an <input type="datetime-local">
    accepts as its value attribute. None when epoch is None, so an unset
    override renders as an empty (placeholder-only) field rather than
    Jan 1 1970.

    Mountain Time, same as _friendly_date — the owner reads and edits
    dates in their own timezone, not the container's."""
    if epoch is None:
        return None
    return timeline.epoch_to_local(epoch).strftime("%Y-%m-%dT%H:%M")


def _friendly_file_size(size_bytes):
    """Convert a file size in bytes to a human-readable string (e.g.,
    '1.2 MB', '340 KB', '12 B'). Returns None if size_bytes is None."""
    if size_bytes is None:
        return None
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _call_properties_fn(spec, row):
    """Call the spec's properties_fn if it exists, with defensive error
    handling. Returns {} on any failure, same best-effort discipline as
    core/ocr.py/core/thumbnails.py's defensive-call patterns."""
    if not spec.properties_fn:
        return {}
    try:
        return spec.properties_fn(row) or {}
    except Exception as e:
        print(f"properties_fn failed for {row.get('slug')}: {e!r}")
        return {}


def _to_object_detail(row):
    """Full detail-page shape for GET /object/<slug> — unlike _to_public,
    this works for any media_type, not just uploaded images. filename can be
    None (a youtube/document row with no local file — see insert_content in
    core/db.py), so nothing here assumes it's set.
    """
    filename = row.get("filename")
    # is_file/has_thumb key off stored_filename (whether a real file
    # currently exists), not the historical `filename` column -- filename
    # is kept forever for display even once a row is redacted (or
    # redacted-then-un-redacted, #282), but stored_filename is cleared by
    # db.mark_redacted the moment the actual file is deleted and never
    # comes back. Without this, an un-redacted row would fall through the
    # template's redacted-placeholder branch straight into is_image_file/
    # thumb_url and try to render a file that's permanently gone.
    is_file = bool(row.get("stored_filename"))
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    has_thumb = _should_advertise_thumb(row, spec)
    item = {
        "slug": row["slug"],
        # Curator (#341): provenance + highlight so the detail page's classifier
        # controls preselect correctly (and never render Undefined -> tojson 500).
        "provenance": row.get("provenance"),
        "highlight": bool(row.get("highlight")),
        # #350: brand-kit flag/role so the detail page's brand controls preselect
        # (and never render Undefined -> tojson 500, same as provenance above).
        "is_brand_asset": bool(row.get("is_brand_asset")),
        "brand_role": row.get("brand_role"),
        "media_type": media_type,
        "type_label": spec.label,
        "type_icon": spec.badge_icon,
        "type_badge": spec.badge_text,
        "ocr_capable": spec.ocr_capable,
        # #239: drives the "Suggested caption" panel — the caption itself
        # lives in type_metadata.auto_caption (see core/captions.py).
        "caption_capable": spec.caption_capable and not captions.DISABLED,
        "filename": filename,
        "is_file": is_file,
        # Drives the full-size <img src="{{ item.url }}"> preview branch in
        # object_detail.html. Registry-driven (#218): a type whose thumbnail
        # *is* the uploaded file (image, gif -- see core/object_types/) can be
        # shown directly by the browser. The old hardcoded suffix tuple here
        # missed .webp/.bmp/.tiff/.ico, which image.py registers, so those
        # uploads fell through to the 400px-thumbnail branch instead.
        "is_image_file": is_file and spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE,
        "url": f"/f/{row['slug']}" if is_file else None,
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb else None,
        "external_url": row.get("external_url"),
        "content_description": row.get("content_description"),
        "content_date_display": _friendly_date(row.get("content_date")),
        # #54: freeform per-type metadata (view/like/comment counts, full
        # description, uploading channel if not the owner's own — see
        # core/object_types.py's "youtube" metadata_fields and
        # scripts/full_youtube_channel_sync.py). {} for every row nothing
        # has ever written type_metadata for, same "always a dict, never
        # missing" contract as row["tags"].
        "type_metadata": row.get("type_metadata") or {},
        # See _to_public's matching comment — row["display_name"]/row["icon"]
        # (#11) are per-object overrides that win over the generic fallbacks.
        "display_name": row.get("display_name") or filename or row.get("content_description") or row["slug"],
        "icon": row.get("icon") or spec.badge_icon,
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        # #47: current project membership — a project selector needs to
        # show what's already attached, not just a blank picker, and (per
        # #51's backfill) an object can now belong to a project it was
        # never uploaded with. _to_project_option is the same slim shape
        # the upload drawer's dropdown already uses.
        "projects": [_to_project_option(p) for p in db.list_projects_for_post(row["slug"])],
        "uploaded_at": row["timestamp"],
        "uploaded_at_display": _friendly_datetime(row["timestamp"]),
        # Full Source string, unshortened — this is the one place it's meant
        # to be spelled out in full (see _to_public for the compact/grouped
        # version used everywhere else).
        "source_display": row["tech"],
        "redacted": bool(row["redacted"]),
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        # #135: file size and original modification date for the Properties panel
        "file_size": row.get("file_size"),
        "file_size_display": _friendly_file_size(row.get("file_size")),
        "source_modified_at_display": _friendly_date(row.get("source_modified_at")),
        # #135: type-specific properties (dimensions, duration, etc.) via the
        # properties_fn hook, wrapped in defensive try/except at the call site
        # (properties_fn implementations are best-effort internally; this adds
        # a second layer of safety matching core/ocr.py/core/thumbnails.py's
        # defensive-call pattern).
        "properties": _call_properties_fn(spec, row),
        # Timeline feature: manual override for this object's position on
        # the Constructicon timeline. display_date_override is the raw
        # epoch (None if unset); *_input is pre-formatted for the
        # datetime-local field's value attribute; effective_date_display
        # is what the timeline actually uses today (override, else
        # content_date, else uploaded_at) — see core/timeline.py.
        "display_date_override": row.get("display_date_override"),
        "display_date_override_input": _datetime_local_value(row.get("display_date_override")),
        "effective_date_display": _friendly_datetime(timeline.resolve_item_date(row)),
        # #448: per-type actions available on this object (e.g. YouTube's "fetch real date" moves here in PR 2).
        # The type file owns the handler; actions are declared in ObjectTypeSpec.actions.
        # #446: filter actions to only those that applies_to(row).
        "actions": [{"key": a.key, "label": a.label, "confirm": a.confirm} for a in spec.actions if a.applies_to(row)],
        # #448: editable form fields for type_metadata. Only includes fields with input set.
        "edit_fields": [{"key": f.key, "label": f.label, "input": f.input, "help_text": f.help_text, "value": (row.get("type_metadata") or {}).get(f.key)} for f in spec.edit_fields if f.input],
        # #449: external link button label (types may override).
        "external_link_label": spec.external_link_label,
    }
    # #449: the type's own preview, built from the finished item dict (not the
    # raw row) so preview_fn sees display_name/type_label/icon/url/thumb_url.
    # None -> the template's generic chain renders instead.
    item["preview_html"] = object_types.render_preview(spec, object_types.PreviewContext(
        item=item,
        media_url=item.get("url") if item.get("is_file") else None,
        thumb_url=item.get("thumb_url"),
        page_url=item.get("external_url"),
        mode="live",
        file_path=storage.path_for(row["stored_filename"]) if row.get("stored_filename") else None,
    ))
    item["preview_assets"] = list(spec.preview_assets)
    return item


def _flatten_tags(nodes):
    """Depth-first flatten of the list_tag_tree() structure — used to look
    up a tag by slug from a query param without a dedicated db helper."""
    flat = []
    for node in nodes:
        flat.append(node)
        flat.extend(_flatten_tags(node.get("children") or []))
    return flat


def _project_cover_url(cover_slug):
    """cover_slug references a capture_events row (see projects.cover_slug
    in core/db.py) — reuse the same thumb route the gallery uses for images.
    A row whose type has no thumbnail concept (e.g. a plain document post),
    or a missing/deleted/redacted slug, falls back to None so the template
    can render a placeholder instead of a broken image."""
    if not cover_slug:
        return None
    row = db.get_by_slug(cover_slug)
    if not row or row.get("redacted") or not _has_thumbnail(row):
        return None
    return f"/f/{row['slug']}/thumb"


def _project_effective_cover_url(project):
    """Resolves a project's effective cover URL, whether from cover_slug
    (#325) or cover_project_id (#356, borrowed from a child project).

    Uses db.resolve_project_cover_slug to follow the proxy chain and find
    the effective cover slug, then delegates to _project_cover_url."""
    effective_slug = db.resolve_project_cover_slug(project)
    return _project_cover_url(effective_slug) if effective_slug else None


def _to_card_face(project, items=None):
    """One home-page card (docs/design/v2-cards.md 8.1): core.cards.card_face (which reuses
    card_level / resolve_home / list_links) plus the cover URL, which needs the web layer's
    thumb-route knowledge. The home page reads the live kind / activity / stage only."""
    face = cards.card_face(project, items)
    face["cover_url"] = _project_effective_cover_url(project)
    return face


def _to_timeline_project(project):
    """Shape for one entry on the gallery timeline rail (web/static/js/timeline-rail.js).
    Unlike _to_card_face, this includes children (is_child) -- the rail
    shows every project, the grid deliberately doesn't (#149)."""
    items = db.list_project_items(project["id"])
    effective_start, effective_end = timeline.resolve_project_span(project, items)
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "cover_url": _project_effective_cover_url(project),
        "effective_start": effective_start,
        "effective_end": effective_end,
        "is_child": project.get("parent_id") is not None,
    }


def _project_has_tag(project, member_slugs):
    return any(item["slug"] in member_slugs for item in db.list_project_items(project["id"]))


def _to_content_public(row, project_slug=None):
    """Public shape for a project-item card. Broader than _to_public: a
    project can contain backfilled youtube/document posts as well as real
    uploaded files, and those have no filename/stored_filename to build a
    thumb from (see core/db.py's insert_content) — but every row, regardless
    of media_type, now gets its own local /object/<slug> detail page, so
    cards always link locally instead of bouncing straight to external_url.

    If project_slug is provided, appends ?from=project:{project_slug} to the
    link for breadcrumb navigation (#137).
    """
    # is_file/external stay keyed on `filename` (this row's TYPE -- was it
    # ever an uploaded file at all, vs. a backfilled youtube/document post
    # with no local-file concept -- see the docstring above), which is
    # preserved forever even once redacted. has_thumb is the separate
    # question of whether a real file exists RIGHT NOW to actually thumb --
    # that one has to key on stored_filename (cleared by db.mark_redacted,
    # #282, and never restored by unmark_redacted), not `redacted`, or a
    # redacted-then-un-redacted row would advertise a thumb_url for a file
    # that's permanently gone.
    is_file = bool(row.get("filename"))
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    has_thumb = _should_advertise_thumb(row)
    link = f"/object/{row['slug']}"
    if project_slug:
        link = f"{link}?from=project:{project_slug}"
    return {
        "slug": row["slug"],
        "title": row.get("content_description") or row.get("description") or row.get("filename") or row["slug"],
        "media_type": media_type,
        "type_icon": spec.badge_icon,
        "type_badge": spec.badge_text,
        "is_file": is_file,
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb else None,
        "link": link,
        "external": not is_file,
        "tags": row["tags"],
        "content_date": row.get("content_date"),
        "timestamp": row.get("timestamp"),
    }


def _to_project_option(project):
    """Slim shape for the upload drawer's Project dropdown — just enough to
    populate a <select> and let the client hand project_id back on upload.
    parent_id (#133) is included too — project_detail.html's parent-project
    selector reuses this same endpoint and needs it client-side to exclude
    a project's own descendants from its own "choose a parent" dropdown
    (the backend's cycle check is the real guard; this just keeps the
    dropdown itself from offering a choice guaranteed to be rejected).
    writeup_slug (#156) is also included so the backfill script can see
    which projects already have writeups."""
    return {"id": project["id"], "slug": project["slug"], "title": project["title"], "status": project["status"], "parent_id": project.get("parent_id"), "writeup_slug": project.get("writeup_slug"), "kind": project.get("kind") or "project", "activity": project.get("activity"), "stage": project.get("stage"), "stop_reason": project.get("stop_reason")}


# --- Blog Entries API ---

def _to_blog_entry_detail(entry):
    """Full detail shape for GET /api/blog-entries/{slug}, including hydrated
    projects and items. Same structure as the db layer returns but with
    attached project and item details."""
    projects = db.list_entry_projects(entry["id"])
    items = db.list_entry_items(entry["id"])
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
        # Layer the per-attachment note + sort_order (from list_entry_projects/
        # _items) on top of the standard project/object shape — the reshaping
        # helpers don't carry them, but they're the point of the attachment.
        "projects": [{**_to_project_option(p), "note": p.get("note", ""), "sort_order": p.get("sort_order")} for p in projects],
        "items": [{**_to_content_public(i), "note": i.get("note", ""), "sort_order": i.get("sort_order")} for i in items],
    }
