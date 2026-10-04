"""Admin routes (#547): settings, backup, delete-all, audit log, redacted/restricted lists,
storage stats, provenance options, caption tuning + breaker, desktop-app build upload."""

import io
import zipfile
from pathlib import Path

from fastapi import Request, Form, UploadFile, File, HTTPException, APIRouter
from fastapi.responses import JSONResponse

from core import backup, captions, db, items, object_types, storage
from core import provenance_options
from web.common import DESKTOP_APP_BUILD_DIR, DESKTOP_APP_BUILD_PATH
from web.shapes import _call_properties_fn, _friendly_datetime, _has_thumbnail, _to_public

router = APIRouter()


DELETE_ALL_PHRASE = "DELETE EVERYTHING"


@router.post("/api/delete-all")
def api_delete_all(confirm: str = Form("")):
    """Wipe every capture_events row (and its files), plus tags and
    projects — a full reset. Stands in for imagerepo's old per-user
    'delete my uploads' button now that multi-user accounts are gone;
    single-owner site, so 'my uploads' and 'everything' are the same set.
    Development convenience while content/schema are still in flux, not a
    feature meant to stick around once the site has real content worth
    protecting.

    #558: requires the typed phrase (confirm=DELETE EVERYTHING), so a forged or
    accidental bodiless POST can't wipe the archive. The audit middleware writes
    the row for the request (with the confirm field).

    #541: still the raw, unlogged path (phase C gives delete-all one core implementation).
    It does NOT use the trash: files are erased on the spot, and it leaves the `trash` table
    and <storage>/.trash alone. Phase C should run items.delete (or purge the trash too)."""
    if confirm.strip() != DELETE_ALL_PHRASE:
        raise HTTPException(status_code=400, detail=f"Type {DELETE_ALL_PHRASE!r} in the confirm field to delete everything")
    # include_redacted (#282) / include_brand (#417): search() hides redacted
    # rows and brand assets by default; a full reset has to take them too or
    # they'd survive as orphaned rows + storage files.
    rows = db.search(limit=100000, include_redacted=True, include_brand=True)
    for row in rows:
        if row.get("stored_filename"):
            storage.delete_files(row["slug"], row["stored_filename"])
        db.delete_upload(row["slug"])
    conn = db.get_conn()
    conn.execute("DELETE FROM post_tags")
    conn.execute("DELETE FROM project_items")
    for _t in ("project_relations", "family_members", "project_hobbies", "blog_entry_projects"):
        conn.execute(f"DELETE FROM {_t}")
    conn.execute("DELETE FROM projects")
    conn.execute("DELETE FROM blog_tags")
    conn.commit()
    conn.close()
    return JSONResponse({"deleted": len(rows)})


@router.get("/api/trash")
def api_trash():
    """#541 phase B: what the trash holds. Ordinary deletes (kept 7 days):
    {count, bytes, oldest, next_expiry, days, items}; redact holds (no expiry) under `held`."""
    return JSONResponse(items.trash_summary())


@router.post("/api/trash/empty")
def api_empty_trash(confirm: str = Form("")):
    """#541: purge every ordinary deleted item's file now (typed phrase EMPTY TRASH, like
    delete-all). Redact holds are NOT touched (`held_kept` says how many were skipped). The
    deletes those files came from can no longer be undone (undo answers trash_expired)."""
    return JSONResponse(items.empty_trash(confirm))


@router.post("/api/backup")
def api_backup():
    """Standalone backup safety net (#20) — zips the DB and every file in
    storage/ into a timestamped archive under core.backup.BACKUP_DIR, then
    prunes down to the most recent BACKUP_RETENTION_COUNT archives.

    Deliberately its own button/endpoint, not called from /api/delete-all or
    /api/delete: a backup that only ran as a side effect of a delete could
    be mistaken for "already backed up" when it wasn't (see #19's history).
    Triggered on demand only — no scheduled job here, see #20's discussion
    for that as separate future work."""
    try:
        info = backup.create_backup()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")
    return JSONResponse(info)


# API Keys (#55) — the admin page's allowlist of settings keys it knows how
# to display/accept. A secret's storage/endpoints (below, and
# core.db.get_setting/has_setting/set_setting) are generic key/value, so a
# future second key (or any other app-level setting) only needs an entry
# here plus a labeled row in admin.html, not a schema change.
# thingiverse_app_token (#62): read-only Thingiverse API access token for
# pulling the owner's own public models — no user-auth flow needed on
# Thingiverse's side, so this is exactly the same "paste one static secret"
# shape as youtube_data_api_key. No storage/endpoint changes required; this
# confirms #55/#59's genericness holds for a second key.
KNOWN_SETTINGS = {
    "youtube_data_api_key": "YouTube Data API Key",
    "thingiverse_app_token": "Thingiverse App Token",
    "pages_publish_token": "GitHub Pages Publish Token",
    "pages_publish_targets": "GitHub Pages Publish Targets",
}


@router.get("/api/settings")
def api_get_settings():
    """Presence-only view of every known setting — never the actual value.
    {"youtube_data_api_key": true} means a key is stored, not what it is.
    This is deliberately the only way the admin page's UI learns whether a
    setting exists; the real value is never sent to the browser, on this
    route or any other, after it's been saved (see api_set_setting)."""
    return JSONResponse({key: db.has_setting(key) for key in KNOWN_SETTINGS})


@router.post("/api/settings")
def api_set_setting(key: str = Form(...), value: str = Form("")):
    """Saves one named setting (or, given an empty value, clears it). `key`
    must be one of KNOWN_SETTINGS above — the storage layer is generic, but
    this endpoint only accepts keys the app actually knows how to use, so it
    can't become an arbitrary junk-drawer for an unauthenticated LAN app.
    Deliberately returns only the same presence flag GET /api/settings
    reports, never the value it was just given, so the browser can't get the
    real value echoed back to it after a save."""
    if key not in KNOWN_SETTINGS:
        raise HTTPException(status_code=400, detail=f"Unknown setting key: {key!r}")
    db.set_setting(key, value)
    return JSONResponse({key: db.has_setting(key)})


@router.get("/api/audit-log")
def api_get_audit_log(limit: int = 100):
    """Fetch recent audit log entries (most recent first). Returns a list of
    audit log rows, each with method, path, scrubbed form_body, affected_slugs,
    status_code, error_detail, and a human-friendly timestamp."""
    rows = db.list_recent_audit_logs(limit=limit)
    # Add human-friendly timestamp to each row
    result = []
    for row in rows:
        result.append(
            {
                **row,
                "timestamp_friendly": _friendly_datetime(row["timestamp"]),
            }
        )
    return JSONResponse(result)


@router.get("/api/redacted")
def api_list_redacted():
    """#282: every currently-redacted row, for the admin page's "Redacted
    items" list. Redacted rows are hidden from every list/search/project/
    tag query, so this is the only way to find one again without already
    knowing its slug. Same card shape as the gallery (_to_public -- thumb_url
    is always None here, the file is gone) plus the direct /object link,
    which keeps working for a redacted row."""
    holds = db.redact_hold_slugs()
    items = [{**_to_public(row), "link": f"/object/{row['slug']}", "held": row["slug"] in holds}
             for row in db.list_redacted()]
    return JSONResponse({"count": len(items), "items": items})


@router.get("/api/restricted")
def api_list_restricted():
    """#443: every item of a restricted type (private keys, certificates),
    for the admin page's "Keys & certificates" list. They're kept out of
    general browsing and never exported, so this (plus the projects they're
    attached to) is where they live until authentication (#467) locks them
    properly. Each carries its type's properties and the projects it's on."""
    items = []
    for row in db.list_restricted():
        spec = object_types.get_object_type(row.get("media_type"))
        items.append({
            **_to_public(row),
            "link": f"/object/{row['slug']}",
            "properties": _call_properties_fn(spec, row),
            "projects": [{"title": p["title"], "slug": p["slug"]} for p in db.list_projects_for_post(row["slug"])],
        })
    return JSONResponse({"count": len(items), "items": items})


@router.get("/api/admin/storage-stats")
def api_storage_stats():
    """#352: storage statistics for the admin page. Returns:
    - silo: {storage_bytes, db_bytes, total_bytes}
    - by_type: [{media_type, count, bytes}]
    - exports_bytes: size of exports/current/ directory
    - backups: {count, bytes}
    """
    # Per-type counts from DB
    type_counts = {t["media_type"]: t["count"] for t in db.media_type_counts()}

    # Per-type bytes and storage total: walk storage directory and stat files
    storage_total = 0
    type_bytes = {}
    if storage.STORAGE_DIR.is_dir():
        # Get all non-redacted items to map stored_filename -> media_type
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT media_type, stored_filename FROM capture_events WHERE redacted = 0 AND stored_filename IS NOT NULL"
            ).fetchall()
            stored_by_type = {}
            for row in rows:
                media_type = row["media_type"]
                stored_filename = row["stored_filename"]
                if media_type not in stored_by_type:
                    stored_by_type[media_type] = []
                stored_by_type[media_type].append(stored_filename)

            # Sum file sizes by type and total
            for media_type, filenames in stored_by_type.items():
                type_bytes[media_type] = 0
                for filename in filenames:
                    fpath = storage.STORAGE_DIR / filename
                    if fpath.exists():
                        type_bytes[media_type] += fpath.stat().st_size
                        storage_total += fpath.stat().st_size

            # Also add thumbnail files to the total (they belong to all types)
            for thumb_path in storage.STORAGE_DIR.glob("*_thumb.jpg"):
                storage_total += thumb_path.stat().st_size
        finally:
            conn.close()

    # DB size
    db_bytes = 0
    if db.DB_PATH.exists():
        db_bytes = db.DB_PATH.stat().st_size

    # Exports size
    exports_bytes = 0
    exports_dir = Path(__file__).resolve().parent.parent.parent / "exports"
    if exports_dir.is_dir():
        for fpath in exports_dir.rglob("*"):
            if fpath.is_file():
                exports_bytes += fpath.stat().st_size

    # Backups: count and total size
    backup_files = backup._existing_backups()
    backups_count = len(backup_files)
    backups_bytes = sum(f.stat().st_size for f in backup_files if f.exists())

    # Build response: by_type list with counts + bytes, sorted by bytes desc
    by_type = []
    for media_type, count in type_counts.items():
        by_type.append({
            "media_type": media_type,
            "count": count,
            "bytes": type_bytes.get(media_type, 0),
        })
    by_type.sort(key=lambda x: x["bytes"], reverse=True)

    return JSONResponse({
        "silo": {
            "storage_bytes": storage_total,
            "db_bytes": db_bytes,
            "total_bytes": storage_total + db_bytes,
        },
        "by_type": by_type,
        "exports_bytes": exports_bytes,
        "backups": {
            "count": backups_count,
            "bytes": backups_bytes,
        },
    })


@router.get("/api/captions/defaults")
def api_caption_defaults():
    """#239: what the pipeline actually runs with, so the admin page's
    tuning panel starts from production's real values rather than its own
    copy of them. #454: includes circuit-breaker status."""
    return JSONResponse({
        "model": captions.OLLAMA_MODEL,
        "prompt": captions.DEFAULT_PROMPT,
        "temperature": captions.DEFAULT_TEMPERATURE,
        "num_predict": captions.DEFAULT_NUM_PREDICT,
        "ollama_up": captions.is_ollama_up(),
        "docker_socket": captions.docker_socket_available(),
        "breaker": captions.breaker_status(),
    })


@router.post("/api/captions/test")
def api_caption_test(
    slug: str = Form(...),
    temperature: float = Form(captions.DEFAULT_TEMPERATURE),
    num_predict: int = Form(captions.DEFAULT_NUM_PREDICT),
    prompt: str = Form(""),
):
    """#239: the admin page's live tuning panel — one synchronous model call
    against an existing object's real preview image with the given
    settings, WITHOUT writing anything to the row. Goes through the exact
    same caption_once() cycle as the pipeline (lock + guarded restart on
    failure or RAM ceiling, #454), so a tuning run can never overlap a real
    one and measures the same thing production will."""
    row = db.get_by_slug(slug.strip())
    if row is None:
        raise HTTPException(status_code=404, detail="No object with that slug")
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="That object was redacted")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.caption_capable:
        raise HTTPException(status_code=400, detail=f"{spec.label} objects aren't caption-capable (see core/object_types)")
    image_path = captions._caption_source_path(row, spec)
    if image_path is None:
        raise HTTPException(status_code=400, detail="No preview image available for that object")
    if not 0.0 <= temperature <= 2.0:
        raise HTTPException(status_code=400, detail="temperature must be between 0 and 2")
    if not 1 <= num_predict <= 1000:
        raise HTTPException(status_code=400, detail="num_predict must be between 1 and 1000")
    result = captions.caption_once(
        image_path,
        prompt=prompt.strip() or None,
        temperature=temperature,
        num_predict=num_predict,
    )
    return JSONResponse({
        **result,
        "slug": row["slug"],
        "media_type": spec.key,
        "thumb_url": f"/f/{row['slug']}/thumb" if _has_thumbnail(row, spec) else None,
        "settings": {"temperature": temperature, "num_predict": num_predict, "prompt": prompt.strip() or captions.DEFAULT_PROMPT},
    })


@router.post("/api/captions/reset-breaker")
def api_captions_reset_breaker():
    """#454: Reset the circuit breaker and consecutive-failure counter,
    resuming captioning if it was paused. For manual intervention via the
    admin page when the breaker has opened."""
    captions.reset_breaker()
    return JSONResponse({"ok": True, "breaker": captions.breaker_status()})


@router.get("/api/account/desktop-app-build")
def api_get_desktop_app_build(request: Request):
    if not DESKTOP_APP_BUILD_PATH.exists():
        return JSONResponse({"exists": False})
    stat = DESKTOP_APP_BUILD_PATH.stat()
    return JSONResponse({"exists": True, "size": stat.st_size, "uploaded_at": stat.st_mtime})


@router.post("/api/account/desktop-app-build")
async def api_upload_desktop_app_build(request: Request, file: UploadFile = File(...)):
    """A tech who's built the app locally (py2app has to run on an actual
    Mac — this server can't build one itself) uploads the resulting zip
    here so everyone else can just download a working binary instead of
    building their own. No versioning: whoever uploads last is what
    everyone gets next."""
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Expected a .zip file (zip the built .app, don't upload it unzipped)")
    content = await file.read()
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise HTTPException(status_code=400, detail="That file isn't a valid zip archive")
    DESKTOP_APP_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_APP_BUILD_PATH.write_bytes(content)
    stat = DESKTOP_APP_BUILD_PATH.stat()
    return JSONResponse({"exists": True, "size": stat.st_size, "uploaded_at": stat.st_mtime})


def _provenance_list_response(scope):
    return JSONResponse({"scope": scope, "options": provenance_options.list_options(scope, include_retired=True)})


@router.get("/api/provenance-options")
def api_list_provenance_options(scope: str = "card", include_retired: int = 0):
    """#529: one editable provenance list ('card' or 'file'), in picker order."""
    return JSONResponse({"scope": scope,
                         "options": provenance_options.list_options(scope, include_retired=bool(include_retired))})


@router.post("/api/provenance-options/{scope}")
def api_add_provenance_option(scope: str, key: str = Form(""), label: str = Form("")):
    """#529: add an option (lowercase slug key, non-empty label). Errors are CardErrors
    (422 bad_provenance_key / bad_provenance_label, 409 provenance_conflict)."""
    provenance_options.add(scope, key, label)
    return _provenance_list_response(scope)


@router.post("/api/provenance-options/{scope}/{key}/rename")
def api_rename_provenance_option(scope: str, key: str, label: str = Form("")):
    """#529: change only the label; the key and every record using it are untouched."""
    provenance_options.rename(scope, key, label)
    return _provenance_list_response(scope)


@router.post("/api/provenance-options/{scope}/{key}/retire")
def api_retire_provenance_option(scope: str, key: str):
    """#529: retire (kept on existing records, refused for new writes)."""
    provenance_options.retire(scope, key)
    return _provenance_list_response(scope)


@router.post("/api/provenance-options/{scope}/{key}/unretire")
def api_unretire_provenance_option(scope: str, key: str):
    provenance_options.unretire(scope, key)
    return _provenance_list_response(scope)


@router.post("/api/provenance-options/{scope}/{key}/move")
def api_move_provenance_option(scope: str, key: str, direction: str = Form("")):
    """#529: move one place 'up' or 'down'."""
    provenance_options.move(scope, key, direction)
    return _provenance_list_response(scope)
