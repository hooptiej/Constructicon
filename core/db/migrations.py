"""The run-once data migrations (#549): the _mig_* functions, MIGRATIONS and the runner.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import json
import logging
import re
import time

from .. import actor as actor_ctx
from .conn import get_conn, transaction
from .labels import migration_source, SOURCE_AUTHORED, SOURCE_CLAUDE, source_groups
from .changelog import insert_change_log
from .tags import _ensure_chip_triggers, MIGRATION_555, _refresh_chip_cache
from .search_index import rebuild_search_index
from .items import _update_content_metadata
from .pipeline import enqueue_caption, _ocr_note_sql, OCR_UNSUPPORTED

_log = logging.getLogger("constructicon.db")


# --- One-time data migrations (#549) ----------------------------------------------------
# Each step is a named function that runs at most once ever per DB: check-and-apply-and-record
# happen inside ONE `transaction()` (BEGIN IMMEDIATE), so a concurrent boot blocks on the write
# lock, then sees the row and skips. Every step is ALSO still idempotent on its own (they all
# were before this table existed), which is what makes an existing DB safe: on prod/test the
# first run of each step finds its effect already present, changes nothing (an owner decision
# already resolved is never re-queued -- queue_decision_once skips open OR resolved rows), and
# just records itself. Only the web process calls run_pending_migrations.

def _mig_drop_legacy_uploads():
    # Pre-generic-schema table from before the capture_events rework -- sample data only, safe
    # to drop rather than migrate. Confirmed with Jason 2026-08-27.
    get_conn().execute("DROP TABLE IF EXISTS uploads")


def _mig_curator_snooze_519():
    # #519: the timed snooze is gone, replaced by Defer (no wait time). Snoozes still in
    # effect become deferrals; expired ones are dropped (they had already come back).
    conn = get_conn()
    conn.execute("UPDATE curator_dismissals SET action = 'defer', snooze_until = NULL "
                 "WHERE action = 'snooze' AND snooze_until IS NOT NULL AND snooze_until > ?", (time.time(),))
    conn.execute("DELETE FROM curator_dismissals WHERE action = 'snooze'")


def _mig_audit_scrub_559():
    # #559: scrub saved setting values out of old audit rows. Before the route-driven
    # redaction, POST /api/settings logged {"key": ..., "value": <the secret>}. New rows are
    # redacted at write time, so this only ever needs to run once.
    conn = get_conn()
    for row in conn.execute("SELECT id, form_body FROM audit_log WHERE path = '/api/settings'").fetchall():
        try:
            body = json.loads(row["form_body"]) if row["form_body"] else {}
        except (ValueError, TypeError):  # silent-ok: an unreadable row is scrubbed whole by the else below
            body = None
        if isinstance(body, dict):
            if "value" not in body or body["value"] == "[REDACTED]":
                continue
            body["value"] = "[REDACTED]"
        elif body is not None and not body:
            continue
        else:
            body = {"_body": "[REDACTED]"}
        conn.execute("UPDATE audit_log SET form_body = ? WHERE id = ?", (json.dumps(body), row["id"]))


# #559: the paths whose audit rows may hold a secret (see web/request_guard.py AUDIT_ROUTE_RULES;
# core can't import web, so the list is repeated here and scripts/test_audit_secrets_559.py checks
# the two stay in step). Matched after stripping trailing slashes.
_AUDIT_SECRET_NAME_ONLY_559 = {"/api/settings"}
_AUDIT_SECRET_NO_BODY_559 = {"/api/auth/login", "/api/auth/setup", "/api/account/password", "/api/users"}
_AUDIT_SECRET_NO_BODY_RE_559 = re.compile(r"^/api/users/[^/]+/password$")
_AUDIT_SETTING_NAME_559 = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_AUDIT_MARKER_FIELDS_559 = {"_unparsed", "content_type", "bytes", "_body"}
_AUDIT_NOT_LOGGED_559 = "not logged: this route carries secrets or file bodies"
_AUDIT_CODE_559 = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _scrubbed_audit_body_559(path, raw):
    """The form_body to store for an old audit row on a secret-bearing path, or None to leave the
    row alone. Idempotent: scrubbing a scrubbed body returns None."""
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, TypeError):  # silent-ok: an unreadable body on a secret route is replaced whole below
        body = None
    if isinstance(body, dict) and not body:
        return None  # nothing was sent
    if isinstance(body, dict) and set(body) <= _AUDIT_MARKER_FIELDS_559:
        return None  # already a marker (unparsed / not logged / redacted): holds no field values
    if path in _AUDIT_SECRET_NAME_ONLY_559 and isinstance(body, dict):
        new = {k: (v if k == "key" and isinstance(v, str) and _AUDIT_SETTING_NAME_559.match(v) else "[REDACTED]")
               for k, v in body.items()}
    elif path in _AUDIT_SECRET_NAME_ONLY_559:
        new = {"_body": "[REDACTED]"}
    else:
        new = {"_body": _AUDIT_NOT_LOGGED_559}
    return None if new == body else json.dumps(new)


def _scrubbed_audit_error_559(raw):
    """A refused request's reason on a secret-bearing path keeps only its code (what the live
    middleware does since #583); None = leave it. A bare code-like word is already just a code."""
    if not raw or raw == "[REDACTED]":
        return None
    code, sep, _ = raw.partition(": ")
    if sep:
        return code
    return None if _AUDIT_CODE_559.match(raw) else "[REDACTED]"


def _mig_audit_scrub_secret_routes_559():
    # #559: the first scrub (audit_scrub_settings_559) only looked at the exact path
    # '/api/settings' and only rewrote the `value` field. This one covers every secret-bearing
    # route and its trailing-slash spelling, and every field. Rewrites ONLY audit_log.form_body
    # and audit_log.error_detail of rows on those paths. Never prints a value, only a count.
    conn = get_conn()
    scrubbed = 0
    rows = conn.execute(
        "SELECT id, path, form_body, error_detail FROM audit_log "
        "WHERE path LIKE '/api/settings%' OR path LIKE '/api/auth/%' OR path LIKE '/api/account/password%' "
        "OR path LIKE '/api/users%'").fetchall()
    for row in rows:
        path = (row["path"] or "").rstrip("/")
        if path not in _AUDIT_SECRET_NAME_ONLY_559 and path not in _AUDIT_SECRET_NO_BODY_559 \
                and not _AUDIT_SECRET_NO_BODY_RE_559.match(path):
            continue
        new_body = _scrubbed_audit_body_559(path, row["form_body"])
        new_error = _scrubbed_audit_error_559(row["error_detail"])
        if new_body is None and new_error is None:
            continue
        conn.execute("UPDATE audit_log SET form_body = ?, error_detail = ? WHERE id = ?",
                     (row["form_body"] if new_body is None else new_body,
                      row["error_detail"] if new_error is None else new_error, row["id"]))
        scrubbed += 1
    print(f"schema_migrations: audit_scrub_secret_routes_559 scrubbed {scrubbed} of {len(rows)} audit rows on secret-bearing paths", flush=True)


def _mig_provenance_options_seed_529():
    # INSERT OR IGNORE only: never renames, un-retires or duplicates anything the owner changed.
    from .. import provenance_options
    provenance_options.ensure_table_and_seed(get_conn())


def _mig_vocab_seed_556():
    # INSERT OR IGNORE only: the old db.BRAND_ROLES / WALLPAPER_TAG_NAMES constants become the lists.
    from .. import vocab
    vocab.ensure_seed(get_conn())


def _mig_install_config_seed_562():
    # #562: an install that already has content gets today's hard-wired owner values, so it sees
    # no change; a fresh (empty) install gets nothing owner-specific. See core/install_config.py.
    from .. import install_config
    install_config.seed_existing_install(get_conn())


def _mig_hobby_physical_piece_562():
    # #562: the PHYSICAL PIECE fields used to show for the hobby NAMED "Traditional Media". It's a
    # per-hobby flag now; the hobby of that name (if this install has one) keeps it switched on.
    from .. import changes, physical_piece
    conn = get_conn()
    rows = conn.execute("SELECT id, slug FROM blog_tags WHERE is_hobby = 1 AND lower(trim(name)) = ?",
                        (physical_piece.LEGACY_HOBBY_NAME,)).fetchall()
    for r in rows:
        if conn.execute("INSERT OR IGNORE INTO hobby_settings (hobby_tag_id, shows_physical_piece) VALUES (?, 1)",
                        (r["id"],)).rowcount:
            insert_change_log(conn, "migration_hobby_physical_piece_562", changes.ACTOR_MIGRATION,
                              [changes.row_image("hobby_settings", {"hobby_tag_id": r["id"]}, None,
                                                 {"hobby_tag_id": r["id"], "shows_physical_piece": 1})],
                              batch_id=changes.new_batch_id(), affected_slugs=[r["slug"]])


def _mig_empty_file_ocr_done_587():
    # #587 item 1: a zero-byte file has nothing to read, so OCR settles it as done with empty text
    # (core/ocr.py does that for new uploads). Re-settle the rows that an earlier version marked
    # failed: only rows whose stored file is really zero bytes on disk. Idempotent.
    from .. import storage
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, stored_filename FROM capture_events "
        "WHERE ocr_status = 'failed' AND stored_filename IS NOT NULL AND file_size = 0").fetchall()
    for r in rows:
        try:
            empty = storage.path_for(r["stored_filename"]).stat().st_size == 0
        except OSError:  # silent-ok: file missing or unreadable: not provably empty, leave the row failed
            empty = False
        if empty:
            conn.execute("UPDATE capture_events SET ocr_status = 'done', extracted_text = '' WHERE slug = ?",
                         (r["slug"],))


def _mig_ocr_unsupported_724():
    # #724: an item with nothing OCR can read (no text source and no picture: a web link today)
    # used to settle as 'failed', a red lamp for something that never went wrong. core/ocr.py now
    # settles those as 'unsupported' with a reason; re-settle the old failed rows the same way.
    # A row qualifies only by object_types.nothing_to_read(row, spec), the exact test new OCR runs use: its
    # type has no text_extract_fn AND no picture is expected for it. So an image (its own file is
    # the picture), a YouTube video (a fetched thumbnail), a PDF or any type with a text source
    # is never touched: their 'failed' is a real error and stays. Redacted rows are left alone.
    # Idempotent: a second run finds no failed row that qualifies.
    from .. import object_types
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, media_type, filename, stored_filename, external_url, type_metadata FROM capture_events "
        "WHERE ocr_status = 'failed' AND COALESCE(redacted, 0) = 0").fetchall()
    moved = {}
    for r in rows:
        row = dict(r)
        try:
            row["type_metadata"] = json.loads(row["type_metadata"] or "{}")
        except ValueError:  # silent-ok: unreadable JSON: not provably "nothing to read", leave the row failed
            continue
        spec = object_types.get_object_type(row.get("media_type"))
        if spec is None or not spec.ocr_capable or not object_types.nothing_to_read(row, spec):
            continue
        note_sql, note_args = _ocr_note_sql(object_types.ocr_unsupported_note(row, spec))
        conn.execute(f"UPDATE capture_events SET ocr_status = ?, {note_sql} WHERE slug = ? AND ocr_status = 'failed'",
                     (OCR_UNSUPPORTED, *note_args, row["slug"]))
        moved[row["media_type"]] = moved.get(row["media_type"], 0) + 1
    print(f"schema_migrations: ocr_unsupported_724 checked {len(rows)} failed row(s), re-settled "
          f"{sum(moved.values())} as unsupported {moved or ''}".rstrip(), flush=True)


def _mig_reread_legacy_office_724b():
    # #724 part b (owner decision 2026-10-17): legacy .doc / .ppt have text readers now, so the rows
    # that settled before them are re-read once: 'failed' (part a and earlier had no reader, or
    # one that couldn't open them) and 'unsupported' (part a's "no readable text yet"). They go back
    # to 'pending' (fresh ocr_started_at, stale ocr_note removed); web's startup self-heal, which runs
    # right after init_db, re-runs OCR on every pending row, and the readers decide afresh (text,
    # "not compatible" with a reason, or "corrupt" with a reason). Matched by the stored file's
    # extension, which is what the readers dispatch on. Redacted rows are left alone. Idempotent:
    # recorded once in schema_migrations, and a second run would only re-queue rows that a re-read
    # settled the same way again.
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug FROM capture_events WHERE ocr_status IN ('failed', ?) AND COALESCE(redacted, 0) = 0 "
        "AND media_type IN ('word', 'powerpoint') "
        "AND (lower(stored_filename) LIKE '%.doc' OR lower(stored_filename) LIKE '%.ppt')",
        (OCR_UNSUPPORTED,)).fetchall()
    note_sql, _ = _ocr_note_sql(None)
    now = time.time()
    for r in rows:
        conn.execute(f"UPDATE capture_events SET ocr_status = 'pending', ocr_started_at = ?, {note_sql} WHERE slug = ?",
                     (now, r["slug"]))
    print(f"schema_migrations: reread_legacy_office_724b re-queued {len(rows)} legacy .doc/.ppt row(s) for OCR",
          flush=True)


def _mig_text_sources_724c():
    # #724 part c (owner decision 2026-10-17: descriptions first, free, no network): a YouTube
    # video's stored description (type_metadata.description) becomes searchable extracted text,
    # concatenated with the thumbnail's OCR text under headed sections, text_source 'description'.
    # New rows get it from core/ocr.py; this re-settles the existing ones once, by the same rule
    # (core/text_sources.resettle), for every type with a text_sources_fn (YouTube today):
    # - a settled row ('done' / 'unsupported') whose sources hold text: extracted_text rebuilt, the
    #   OCR text already stored kept as its "Thumbnail text:" section (never lost);
    # - a settled row with no source text and no thumbnail text: 'unsupported' with the type's note
    #   ("video: no description or captions yet", grey), part a's rule;
    # - pending rows are left to their OCR run, 'failed' rows keep their real error until Retry,
    #   redacted rows and rows with unreadable type_metadata JSON are left alone.
    # No network, no OCR. Idempotent: resettle returns None for a row already settled this way, so a
    # second run changes nothing. The FTS index follows through the UPDATE OF extracted_text trigger.
    from .. import object_types, text_sources
    specs = {k: s for k, s in object_types.OBJECT_TYPES.items() if s.ocr_capable and s.text_sources_fn is not None}
    if not specs:
        return
    conn = get_conn()
    marks = ", ".join("?" for _ in specs)
    rows = conn.execute(
        "SELECT slug, media_type, filename, stored_filename, external_url, extracted_text, ocr_status, type_metadata "
        f"FROM capture_events WHERE media_type IN ({marks}) AND ocr_status IN ('done', ?) "
        "AND COALESCE(redacted, 0) = 0", (*specs, OCR_UNSUPPORTED)).fetchall()
    combined = unsupported = 0
    for r in rows:
        row = dict(r)
        try:
            row["type_metadata"] = json.loads(row["type_metadata"] or "{}")
        except ValueError:  # silent-ok: unreadable JSON: leave the row exactly as it is
            continue
        spec = specs[row["media_type"]]
        change = text_sources.resettle(row, spec, object_types.ocr_unsupported_note(row, spec))
        if change is None:
            continue
        meta = text_sources.resettled_meta(row["type_metadata"], change)
        conn.execute("UPDATE capture_events SET extracted_text = ?, ocr_status = ?, type_metadata = ? WHERE slug = ?",
                     (change["text"], change["status"], json.dumps(meta), row["slug"]))
        if change["status"] == OCR_UNSUPPORTED:
            unsupported += 1
        else:
            combined += 1
    print(f"schema_migrations: text_sources_724c checked {len(rows)} settled row(s) of {sorted(specs)}: "
          f"{combined} given their description text, {unsupported} re-settled as unsupported (no text at all)",
          flush=True)


def _mig_link_page_text_724d():
    # #724 part d (owner decision 2026-10-17, "Title + summary, safe fetch"): web links read their
    # page's title and summary now (core/object_types/url.py page_text, through core/safe_fetch.py).
    # The url rows that settled before it ('unsupported' "web link: no readable text yet" from part
    # a, or 'failed' on an install where part a found them first) go back to 'pending' once (fresh
    # ocr_started_at, stale ocr_note removed); web's startup self-heal, right after init_db, re-runs
    # OCR on every pending row. The fetches themselves are polite: core/safe_fetch.py lets one page
    # fetch run at a time per container with a pause between them (CONSTRUCTICON_LINK_FETCH_DELAY_
    # SECONDS, default 2), so a backfill of many links to one site never bursts. No network here.
    # Rows with no external_url, redacted rows and every other type are left alone. Idempotent:
    # recorded once in schema_migrations; a second run would only re-queue rows a fetch settled the
    # same way again.
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug FROM capture_events WHERE media_type = 'url' AND ocr_status IN ('failed', ?) "
        "AND COALESCE(redacted, 0) = 0 AND COALESCE(TRIM(external_url), '') != ''",
        (OCR_UNSUPPORTED,)).fetchall()
    note_sql, _ = _ocr_note_sql(None)
    now = time.time()
    for r in rows:
        conn.execute(f"UPDATE capture_events SET ocr_status = 'pending', ocr_started_at = ?, {note_sql} WHERE slug = ?",
                     (now, r["slug"]))
    print(f"schema_migrations: link_page_text_724d re-queued {len(rows)} web link row(s) to read their page title "
          f"and summary", flush=True)


def _mig_reread_xls_790():
    # #790 (owner decision 2026-10-28): an .xls that holds no Excel workbook (the #720 sampler's stub,
    # an OLE file of something else) now settles grey "not compatible" (spreadsheet.xls_check), like
    # .doc / .ppt, instead of red "corrupt or unreadable .xls". The .xls rows that settled 'failed'
    # before that go back to 'pending' once (fresh ocr_started_at, stale ocr_note removed); web's
    # startup self-heal, right after init_db, re-runs OCR on every pending row and the reader decides
    # afresh: grey for a non-workbook, still red for a real workbook that is damaged (owner: keep the
    # split), text for one that reads. Matched by the stored file's extension, which the reader
    # dispatches on. Redacted rows, 'done' / 'unsupported' rows and every other type are left alone.
    # No file is read here. Idempotent: recorded once in schema_migrations; a second run would only
    # re-queue rows a re-read settled 'failed' again.
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug FROM capture_events WHERE media_type = 'spreadsheet' AND ocr_status = 'failed' "
        "AND COALESCE(redacted, 0) = 0 AND lower(COALESCE(stored_filename, '')) LIKE '%.xls'").fetchall()
    note_sql, _ = _ocr_note_sql(None)
    now = time.time()
    for r in rows:
        conn.execute(f"UPDATE capture_events SET ocr_status = 'pending', ocr_started_at = ?, {note_sql} WHERE slug = ?",
                     (now, r["slug"]))
    print(f"schema_migrations: reread_xls_790 re-queued {len(rows)} failed legacy .xls row(s) for OCR "
          f"({', '.join(r['slug'] for r in rows[:20])}{' ...' if len(rows) > 20 else ''})", flush=True)


CAPTION_BACKFILL_TYPES_556 = ("pdf", "ai")


def _mig_caption_pdf_ai_556():
    # #556 (owner decision 2026-10-28): PDF and Illustrator files are caption-capable now. Their
    # existing rows with no caption go into the normal caption pipeline once, exactly what Admin's
    # "Caption skipped items" (core/captions.queue_skipped) would pick: not redacted, no description
    # (content_description) yet, caption status absent / '' / 'failed' / 'skipped'. Deleted items are
    # not rows any more (they live in the trash), so they are never picked. Each is marked
    # auto_caption_status 'pending' and given a caption_queue row (db.enqueue_caption); web's caption
    # worker drains the queue one at a time behind the lock, the cooldown and the breaker (an open
    # breaker leaves the queue alone until it is reset), like any upload. With captioning off on this
    # install (CAPTION_DISABLED) nothing is queued and the log says how many were left and how to
    # queue them later (Admin > Caption skipped items). No model call here. Idempotent: recorded once
    # in schema_migrations; a second run finds them pending / done and picks nothing.
    from .. import captions  # lazy: core.captions imports this module
    types = CAPTION_BACKFILL_TYPES_556
    status = "json_extract(type_metadata, '$.auto_caption_status')"
    conn = get_conn()
    rows = conn.execute(
        f"SELECT slug FROM capture_events WHERE media_type IN ({', '.join('?' * len(types))}) "
        "AND COALESCE(redacted, 0) = 0 AND COALESCE(TRIM(content_description), '') = '' "
        "AND (type_metadata IS NULL OR json_valid(type_metadata)) "
        f"AND ({status} IS NULL OR {status} = '' OR {status} IN ('failed', 'skipped'))", types).fetchall()
    garbled = conn.execute(
        f"SELECT slug FROM capture_events WHERE media_type IN ({', '.join('?' * len(types))}) "
        "AND type_metadata IS NOT NULL AND NOT json_valid(type_metadata)", types).fetchall()
    if garbled:  # never silent: these can't be marked pending without losing what the column holds
        print(f"schema_migrations: caption_pdf_ai_556 left out {len(garbled)} PDF / Illustrator row(s) whose "
              f"type_metadata isn't valid JSON: {', '.join(r['slug'] for r in garbled[:20])}; caption them "
              "from their page (Generate) once fixed", flush=True)
    if captions.DISABLED:
        print(f"schema_migrations: caption_pdf_ai_556 queued NOTHING: captions are off on this install "
              f"(CAPTION_DISABLED), so {len(rows)} PDF / Illustrator item(s) wait uncaptioned. Turn captioning "
              "on, then use Admin > Caption skipped items to queue them.", flush=True)
        return
    for r in rows:
        conn.execute("UPDATE capture_events SET type_metadata = json_set(COALESCE(type_metadata, '{}'), "
                     "'$.auto_caption_status', 'pending') WHERE slug = ?", (r["slug"],))
        enqueue_caption(r["slug"])
    print(f"schema_migrations: caption_pdf_ai_556 queued {len(rows)} PDF / Illustrator item(s) for a caption "
          "(the web worker runs them one at a time)", flush=True)


def _mig_transcripts_724e():
    # #724 part e (owner decision 2026-10-17: transcripts through youtube-transcript-api): every
    # existing row of a type with a network text source (YouTube today, ObjectTypeSpec.text_fetch_fn)
    # that has never had a transcript attempt is marked type_metadata.transcript_status = 'queued'.
    # No network here and no OCR re-run: web's _transcript_loop (web/app.py) works the queued rows a
    # few per pass, one fetch at a time (core/transcripts.py), and folds each result into the row's
    # stored text (core/text_sources.resettle). Rows that already have a transcript_status (an OCR
    # run since part e landed), redacted rows and rows with unreadable type_metadata are left alone.
    # Idempotent: recorded once in schema_migrations, and a second run finds no row without a status.
    from .. import object_types
    types = [k for k, s in object_types.OBJECT_TYPES.items() if getattr(s, "text_fetch_fn", None) is not None]
    if not types:
        return
    conn = get_conn()
    marks = ", ".join("?" for _ in types)
    cur = conn.execute(
        "UPDATE capture_events SET type_metadata = json_set(CASE WHEN COALESCE(type_metadata, '') = '' THEN '{}' "
        "ELSE type_metadata END, '$.transcript_status', 'queued') "
        f"WHERE media_type IN ({marks}) AND COALESCE(redacted, 0) = 0 "
        "AND (COALESCE(type_metadata, '') = '' OR (json_valid(type_metadata) AND json_type(type_metadata) = 'object' "
        "AND json_extract(type_metadata, '$.transcript_status') IS NULL))", types)
    print(f"schema_migrations: transcripts_724e queued {cur.rowcount} row(s) of {sorted(types)} for a transcript "
          f"attempt (fetched in the background, one at a time)", flush=True)


def _mig_writeup_lead_596():
    # #596: fill the projects.writeup_lead cache (the card face's write-up excerpt) once for every
    # card that has a write-up. From then on the cards service keeps it in step with every write-up
    # save. Derived data, recomputable at any time, so it isn't change-logged. Idempotent.
    from .. import cards
    conn = get_conn()
    for r in conn.execute("SELECT id, writeup_slug FROM projects WHERE writeup_slug IS NOT NULL").fetchall():
        conn.execute("UPDATE projects SET writeup_lead = ? WHERE id = ?",
                     (cards.writeup_lead(r["writeup_slug"]), r["id"]))


def _mig_reextract_utf16_text_607():
    # #607 part 3: text files in UTF-16 / UTF-32 (what Windows writes for `gpresult /h`, `powercfg
    # /batteryreport`, `msinfo32`) were read as UTF-8 and stopped almost at once, so their extracted
    # text was a few characters (150 KB report -> 3 characters) and they were unsearchable. The readers
    # now detect the encoding; this re-extracts the rows already stored wrong, once. A row qualifies
    # when its text is far shorter than its file (under 1/8 of the bytes), the file itself reads as
    # UTF-16/32 (BOM, or the NUL heuristic) and its type has a text reader; the new text replaces the
    # old when that is NUL-ridden (the signature of UTF-16 read as UTF-8) or shorter. Note SQLite's
    # length() stops at the first NUL, which is why a 233 KB report reads as "3 characters" in SQL.
    # Idempotent (a second run finds nothing to fix).
    from .. import object_types, storage
    from ..object_types import _textstats
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, media_type, stored_filename, file_size, extracted_text FROM capture_events "
        "WHERE stored_filename IS NOT NULL AND file_size >= 64 "
        "AND length(COALESCE(extracted_text, '')) * 8 < file_size").fetchall()
    checked = fixed = 0
    for r in rows:
        spec = object_types.get_object_type(r["media_type"])
        if spec is None or spec.text_extract_fn is None:
            continue
        checked += 1
        try:
            path = storage.path_for(r["stored_filename"])
            codec, _label = _textstats.sniff_encoding(path)
        except OSError:  # silent-ok: file missing or unreadable: nothing to re-read, leave the row as it is
            continue
        if not codec.startswith(("utf-16", "utf-32")):
            continue
        new_text = (spec.text_extract_fn(dict(r)) or "").strip()
        old_text = r["extracted_text"] or ""
        # The old reader's output for a UTF-16 file is full of NUL characters (SQLite's length() stops at the
        # first one, which is how these rows look "short"); that, or simply being shorter, means replace.
        if "\x00" in old_text or len(new_text) > len(old_text):
            conn.execute("UPDATE capture_events SET extracted_text = ? WHERE slug = ?", (new_text, r["slug"]))
            fixed += 1
    print(f"reextract_utf16_text_607: {len(rows)} short-text rows, {checked} with a text reader, {fixed} re-extracted", flush=True)

_UPLOAD_FILE_FIELD = re.compile(r"^<file: (.*)>$", re.S)
OWNERSHIP_BACKFILL_WINDOW = 600  # seconds between an item row's timestamp and its request-log row


def _ownership_backfill(conn):
    """#604 step 1: fill the new ownership columns once from the audit log, for what a SIGNED-IN user
    made before ownership was recorded at write time. Only `actor = user:<name>` rows count, and only
    when that user still exists; everything else stays NULL (= admin-owned, the safe default).
      * items: the request-log row of a successful `POST /api/upload` (its logged `file` field, the
        filename) or `POST /api/content` (its `external_url`), matched to the not-yet-owned item
        with that filename / URL created within OWNERSHIP_BACKFILL_WINDOW before the request was
        logged (the nearest one);
      * cards: `create_card` change-log rows (the projects row they inserted), plus the card's own
        write-up item made in the same create;
      * hobbies: `hobby_create` rows that turned a tag INTO a hobby (hobby_settings.created_by_user_id);
      * blog entries: `blog_entry_create` rows (the blog_entries row they inserted).
    Never overwrites a value already set. Idempotent. Returns the counts per kind."""
    users_by_name = {r["username"].lower(): r["id"] for r in conn.execute("SELECT id, username FROM users")}

    def uid(actor):
        if not (actor or "").startswith("user:"):
            return None
        return users_by_name.get(actor[5:].lower())

    def body(text):
        try:
            data = json.loads(text) if text else {}
        except ValueError:  # silent-ok: an unreadable request body can't name a file; that row is skipped
            return {}
        return data if isinstance(data, dict) else {}

    counts = {"items": 0, "cards": 0, "card_writeups": 0, "hobbies": 0, "blog_entries": 0}
    for r in conn.execute("SELECT timestamp, actor, path, form_body FROM audit_log WHERE op IS NULL AND method = 'POST' "
                          "AND path IN ('/api/upload', '/api/content') AND status_code = 200 AND actor LIKE 'user:%' "
                          "ORDER BY id").fetchall():
        owner = uid(r["actor"])
        if owner is None:
            continue
        fields = body(r["form_body"])
        if r["path"] == "/api/upload":
            m = _UPLOAD_FILE_FIELD.match(str(fields.get("file") or ""))
            column, value = "filename", (m.group(1) if m else None)
        else:
            column, value = "external_url", fields.get("external_url")
        if not value:
            continue
        hit = conn.execute(
            f"SELECT slug FROM capture_events WHERE uploaded_by_user_id IS NULL AND {column} = ? "
            "AND timestamp BETWEEN ? AND ? ORDER BY ABS(timestamp - ?) LIMIT 1",
            (value, r["timestamp"] - OWNERSHIP_BACKFILL_WINDOW, r["timestamp"] + 1, r["timestamp"])).fetchone()
        if hit:
            conn.execute("UPDATE capture_events SET uploaded_by_user_id = ? WHERE slug = ?", (owner, hit["slug"]))
            counts["items"] += 1

    def changes_by_user(op):
        for r in conn.execute("SELECT actor, mutations FROM audit_log WHERE op = ? AND actor LIKE 'user:%' "
                              "AND mutations IS NOT NULL ORDER BY id", (op,)).fetchall():
            owner = uid(r["actor"])
            if owner is None:
                continue
            try:
                muts = json.loads(r["mutations"]) or []
            except ValueError:  # silent-ok: a change-log row with unreadable images can't name what it made
                continue
            yield owner, [m for m in muts if isinstance(m, dict)]

    for owner, muts in changes_by_user("create_card"):
        for m in muts:
            if m.get("table") == "projects" and m.get("before") is None and (m.get("key") or {}).get("id"):
                card_id = m["key"]["id"]
                cur = conn.execute("UPDATE projects SET created_by_user_id = ? WHERE id = ? AND created_by_user_id IS NULL",
                                   (owner, card_id))
                if cur.rowcount:
                    counts["cards"] += 1
                    card = conn.execute("SELECT writeup_slug, created_at FROM projects WHERE id = ?", (card_id,)).fetchone()
                    if card and card["writeup_slug"]:
                        counts["card_writeups"] += conn.execute(
                            "UPDATE capture_events SET uploaded_by_user_id = ? WHERE slug = ? AND uploaded_by_user_id IS NULL "
                            "AND ABS(timestamp - ?) < 60", (owner, card["writeup_slug"], card["created_at"])).rowcount
    for owner, muts in changes_by_user("hobby_create"):
        for m in muts:
            after, before = m.get("after") or {}, m.get("before")
            if m.get("table") != "blog_tags" or after.get("is_hobby") != 1:
                continue
            if before is not None and before.get("is_hobby") == 1:
                continue  # re-marked an existing hobby: not its creation
            tag_id = (m.get("key") or {}).get("id")
            if not tag_id or not conn.execute("SELECT 1 FROM blog_tags WHERE id = ? AND is_hobby = 1", (tag_id,)).fetchone():
                continue
            conn.execute("INSERT OR IGNORE INTO hobby_settings (hobby_tag_id) VALUES (?)", (tag_id,))
            counts["hobbies"] += conn.execute(
                "UPDATE hobby_settings SET created_by_user_id = ? WHERE hobby_tag_id = ? AND created_by_user_id IS NULL",
                (owner, tag_id)).rowcount
    for owner, muts in changes_by_user("blog_entry_create"):
        for m in muts:
            if m.get("table") == "blog_entries" and m.get("before") is None and (m.get("key") or {}).get("id"):
                counts["blog_entries"] += conn.execute(
                    "UPDATE blog_entries SET created_by_user_id = ? WHERE id = ? AND created_by_user_id IS NULL",
                    (owner, m["key"]["id"])).rowcount
    return counts


def _mig_ownership_backfill_604():
    # #604 step 1: who made what, from the audit log (see _ownership_backfill). One change-log row
    # carries the counts (no row images: derived data, recomputable from the log itself).
    from .. import changes
    conn = get_conn()
    counts = _ownership_backfill(conn)
    insert_change_log(conn, "migration_ownership_backfill_604", changes.ACTOR_MIGRATION, [],
                      batch_id=changes.new_batch_id(), details={"counts": counts})
    print(f"schema_migrations: ownership_backfill_604 counts {counts}", flush=True)


class MigrationDeferred(Exception):
    """Raised by a migration that must NOT run yet (its precondition isn't met, e.g. the owner's account
    can't be told apart). run_pending_migrations rolls its transaction back (nothing written), prints the
    reason loudly, leaves it unrecorded (so it tries again at the next start) and carries on booting."""


def _request_log_items(conn):
    """{slug: request actor} for items matched to the request-log row of the successful `POST
    /api/upload` (logged `file` field = the filename) or `POST /api/content` (its `external_url`) that
    made them: the nearest item with that name / URL created up to OWNERSHIP_BACKFILL_WINDOW before the
    row was logged, each item matched once. The actor may be None (rows from before actors were logged:
    the single-owner, no-login era), "owner-ui", "token" (the install token), "user:<name>", ..."""
    def body(text):
        try:
            data = json.loads(text) if text else {}
        except ValueError:  # silent-ok: an unreadable request body can't name a file; that row is skipped
            return {}
        return data if isinstance(data, dict) else {}

    out = {}
    for r in conn.execute("SELECT timestamp, actor, path, form_body FROM audit_log WHERE op IS NULL AND method = 'POST' "
                          "AND path IN ('/api/upload', '/api/content') AND status_code = 200 ORDER BY id").fetchall():
        fields = body(r["form_body"])
        if r["path"] == "/api/upload":
            m = _UPLOAD_FILE_FIELD.match(str(fields.get("file") or ""))
            column, value = "filename", (m.group(1) if m else None)
        else:
            column, value = "external_url", fields.get("external_url")
        if not value:
            continue
        for hit in conn.execute(
                f"SELECT slug FROM capture_events WHERE {column} = ? AND timestamp BETWEEN ? AND ? "
                "ORDER BY ABS(timestamp - ?)",
                (value, r["timestamp"] - OWNERSHIP_BACKFILL_WINDOW, r["timestamp"] + 1, r["timestamp"])).fetchall():
            if hit["slug"] not in out:
                out[hit["slug"]] = r["actor"]
                break
    return out


def _owner_manual_label(conn):
    """The owner's manual-upload Source label as `source_manual_upload()` builds it, read on `conn`
    (install_config: owner_label, else owner_name, else "Owner") + " — manual upload"."""
    from .. import install_config  # lazy: install_config imports this module
    vals = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM install_config WHERE key IN ('owner_label', 'owner_name')")}
    who = (vals.get("owner_label") or "").strip() or (vals.get("owner_name") or "").strip() or install_config.FALLBACK_OWNER
    return f"{who} — manual upload", (vals.get("owner_name") or "").strip()


def owner_account(conn=None):
    """#560: the owner's user account, for attributing the owner's own old uploads. Returns
    (user row, how) or raises MigrationDeferred saying why it can't be told:
      * install_config.owner_name set -> the ONE enabled admin whose display name or username is that
        name (any case); none or several -> refused (a name that matches nobody is ambiguous, never
        guessed);
      * owner_name unset -> the install's only enabled admin; none or several -> refused."""
    own = conn is None
    conn = conn or get_conn()
    try:
        _label, owner_name = _owner_manual_label(conn)
        admins = [dict(r) for r in conn.execute(
            "SELECT id, username, display_name FROM users WHERE role = 'admin' AND COALESCE(disabled, 0) = 0 "
            "ORDER BY id")]
    finally:
        if own:
            conn.close()
    if owner_name:
        want = owner_name.lower()
        hits = [a for a in admins if want in ((a["display_name"] or "").strip().lower(), a["username"].lower())]
        if len(hits) == 1:
            return hits[0], f"the admin named {owner_name!r} (install config owner_name)"
        if not hits:
            raise MigrationDeferred(
                f"the install's owner is {owner_name!r} (Admin > Install, owner name), but no enabled admin has "
                f"that display name or username ({len(admins)} admin(s): "
                f"{', '.join(a['username'] for a in admins) or 'none'}). Set the owner name to the owner's "
                "display name, or give the owner's account that display name, then restart the web app.")
        raise MigrationDeferred(
            f"{len(hits)} enabled admins match the owner name {owner_name!r} "
            f"({', '.join(a['username'] for a in hits)}); give them different display names, then restart.")
    if len(admins) == 1:
        return admins[0], "the install's only admin (no owner name is set)"
    raise MigrationDeferred(
        f"no owner name is set (Admin > Install) and the install has {len(admins)} enabled admins, so the "
        "owner's account can't be told; set the owner name, then restart the web app.")


def _mig_capture_channel_560():
    # #560: capture_events.channel (how an item arrived) for every row made before it was recorded at
    # write time. Fill-only (channel IS NULL), idempotent, one change-log row with the count per rule.
    # Rules, first that applies wins (see CLAUDE.md "Who and how"):
    #   1. a Source label "Claude — migrated from <X>"            -> migration:<X>
    #   2. its upload_tags / automatch_tags change-log row (made with the item): that row's actor
    #      (mcp -> mcp; script -> script; the web's actors -> web)
    #   3. matched to a successful POST /api/upload or /api/content request-log row -> web
    #   4. a card's write-up: the actor of the create_card row that made the card -> its channel
    #   5. the label alone: the owner's "— manual upload" (only the web routes write it) -> web,
    #      "— automated upload" -> script, "Claude — authored" / "Claude" (the MCP's default) -> mcp
    #   6. otherwise -> unknown (said so, never guessed)
    from .. import changes
    conn = get_conn()
    rows = conn.execute("SELECT slug, tech, timestamp FROM capture_events WHERE channel IS NULL").fetchall()
    counts = {"rows": len(rows), "migration_label": 0, "change_log": 0, "request_log": 0, "writeup": 0,
              "label": 0, "unknown": 0}
    if not rows:
        insert_change_log(conn, "migration_capture_channel_560", changes.ACTOR_MIGRATION, [],
                          batch_id=changes.new_batch_id(), details={"counts": counts})
        print(f"schema_migrations: capture_channel_560 nothing to fill {counts}", flush=True)
        return
    when = {r["slug"]: r["timestamp"] for r in rows}
    by_log = {}
    for r in conn.execute("SELECT actor, affected_slugs, timestamp FROM audit_log WHERE op IN "
                          "('upload_tags', 'automatch_tags') ORDER BY id").fetchall():
        try:
            slugs = json.loads(r["affected_slugs"] or "[]")
        except ValueError:  # silent-ok: an unreadable slug list names no item; the row is skipped
            continue
        for s in slugs if isinstance(slugs, list) else []:
            if s in when and s not in by_log and abs((r["timestamp"] or 0) - when[s]) <= 120 and r["actor"]:
                by_log[s] = actor_ctx.channel_for_actor(r["actor"])
    by_request = _request_log_items(conn)
    by_writeup = {}
    for r in conn.execute("SELECT actor, mutations FROM audit_log WHERE op = 'create_card' AND mutations IS NOT NULL "
                          "ORDER BY id").fetchall():
        try:
            muts = json.loads(r["mutations"]) or []
        except ValueError:  # silent-ok: unreadable images can't name the card; its write-up falls to the label rules
            continue
        for m in muts if isinstance(muts, list) else []:
            if isinstance(m, dict) and m.get("table") == "projects" and m.get("before") is None:
                card = conn.execute("SELECT writeup_slug FROM projects WHERE id = ?",
                                    ((m.get("key") or {}).get("id"),)).fetchone()
                if card and card["writeup_slug"] and r["actor"]:
                    by_writeup.setdefault(card["writeup_slug"], actor_ctx.channel_for_actor(r["actor"]))
    from .. import install_config
    groups = source_groups()
    manual = {f"{g} — manual upload" for g in groups if g != SOURCE_CLAUDE} | {f"{install_config.FALLBACK_OWNER} — manual upload"}
    automated = {f"{g} — automated upload" for g in groups if g != SOURCE_CLAUDE}
    for r in rows:
        slug, tech = r["slug"], (r["tech"] or "").strip()
        src = migration_source(tech)
        if src:
            channel, rule = f"migration:{src}", "migration_label"
        elif slug in by_log:
            channel, rule = by_log[slug], "change_log"
        elif slug in by_request:
            channel, rule = actor_ctx.CHANNEL_WEB, "request_log"
        elif slug in by_writeup:
            channel, rule = by_writeup[slug], "writeup"
        elif tech in manual:
            channel, rule = actor_ctx.CHANNEL_WEB, "label"
        elif tech in automated:
            channel, rule = actor_ctx.CHANNEL_SCRIPT, "label"
        elif tech in (SOURCE_AUTHORED, SOURCE_CLAUDE):
            channel, rule = actor_ctx.CHANNEL_MCP, "label"
        else:
            channel, rule = "unknown", "unknown"
        counts[rule] += 1
        conn.execute("UPDATE capture_events SET channel = ? WHERE slug = ? AND channel IS NULL", (channel, slug))
    per_channel = {c: n for c, n in conn.execute(
        "SELECT channel, COUNT(*) FROM capture_events GROUP BY channel ORDER BY 2 DESC").fetchall()}
    insert_change_log(conn, "migration_capture_channel_560", changes.ACTOR_MIGRATION, [],
                      batch_id=changes.new_batch_id(), details={"counts": counts, "per_channel": per_channel})
    print(f"schema_migrations: capture_channel_560 filled {len(rows)} row(s) by rule {counts}; "
          f"now per channel {per_channel}", flush=True)


def _owner_upload_backfill(conn):
    """#560 (owner decision 2026-10-25, answer (a)): the owner's own old uploads become the owner's.
    An item counts when ALL hold: no uploader recorded yet (fill-only), its Source label is exactly the
    owner's manual-upload label (`source_manual_upload()`, e.g. "Hooptie J (me) — manual upload"), it
    arrived through the web (channel 'web', from _mig_capture_channel_560), and its upload request (when
    the request log has it) was not made by ANOTHER signed-in user. Uploads made with the install token
    under that label count too (owner, 2026-10-30: "Give them to me too"; they were the owner's scripts),
    counted in `of_which_install_token`. Returns (counts, assigned slugs); raises MigrationDeferred when
    there is something to assign but the owner's account can't be told (owner_account)."""
    label, _owner_name = _owner_manual_label(conn)
    rows = conn.execute("SELECT slug, channel FROM capture_events WHERE uploaded_by_user_id IS NULL AND tech = ?",
                        (label,)).fetchall()
    counts = {"label": label, "candidates": len(rows), "assigned": 0, "of_which_install_token": 0,
              "left_other_user": 0, "left_not_web": 0, "owner": None}
    if not rows:
        return counts, []
    owner, how = owner_account(conn)
    counts["owner"] = {"id": owner["id"], "username": owner["username"], "how": how}
    by_request = _request_log_items(conn)
    picked = []
    for r in rows:
        req = by_request.get(r["slug"])
        if r["channel"] != actor_ctx.CHANNEL_WEB:
            counts["left_not_web"] += 1
        elif isinstance(req, str) and req.startswith("user:") and req[5:].lower() != owner["username"].lower():
            counts["left_other_user"] += 1
        else:
            picked.append(r["slug"])
            if req == actor_ctx.ACTOR_TOKEN:
                counts["of_which_install_token"] += 1
    for i in range(0, len(picked), 500):
        part = picked[i:i + 500]
        conn.execute(f"UPDATE capture_events SET uploaded_by_user_id = ? WHERE uploaded_by_user_id IS NULL "
                     f"AND slug IN ({', '.join('?' * len(part))})", [owner["id"], *part])
    counts["assigned"] = len(picked)
    return counts, picked


def _mig_owner_upload_backfill_560():
    # #560: see _owner_upload_backfill. One change-log row with the counts and the slugs it set (no row
    # images: the slugs are the way back, `UPDATE ... SET uploaded_by_user_id = NULL WHERE slug IN (...)`).
    # Idempotent: fill-only, a second run finds nothing. Deferred (not recorded, retried every start)
    # while the owner's account is ambiguous.
    from .. import changes
    conn = get_conn()
    counts, slugs = _owner_upload_backfill(conn)
    insert_change_log(conn, "migration_owner_uploads_560", changes.ACTOR_MIGRATION, [],
                      batch_id=changes.new_batch_id(), details={"counts": counts, "slugs": slugs})
    print(f"schema_migrations: owner_upload_backfill_560 counts {counts}", flush=True)


# #760: the one-time v1 -> v2 card migrations (v2c_1_kind_status, v2c_2_hobbies,
# v2c_5_referenced_provenance; core/card_migration.py) were removed: both installs are V2 and every
# new card is created with a stage, so they had nothing left to do. Their schema_migrations rows stay
# on existing DBs (harmless); a fresh install never runs them. (v2c_3 / v2c_4 went in #562.)
def _mig_date_guess_backfill_725():
    # #725: undated items (no real date: the upload time and a post-install file mtime don't count)
    # get the high-confidence date guesses (core/date_guess.py: a date in the file name, the file's
    # own embedded date such as a PDF CreationDate or a container date, a stored YouTube publish
    # date) as content_date, with the source recorded, as ONE undoable batch (core/items.py
    # apply_dates, op item_date_guess, actor migration). No network: nothing is fetched. Idempotent:
    # fill-only, so a second run finds those items dated and skips them. The rest stay undated:
    # they inherit their card's range, or the Curator shows them as needs_date.
    from .. import actor as actor_ctx, item_dates, items
    with actor_ctx.acting_as(actor_ctx.ACTOR_MIGRATION):
        plan = item_dates.auto_plan(read_files=True)
        by_source = {}
        for _row, g in plan:
            by_source[g.source] = by_source.get(g.source, 0) + 1
        if plan:
            res = items.apply_dates([(r["slug"], g.epoch, item_dates.source_record(g)) for r, g in plan])
            applied, batch = len(res.data["applied"]), res.batch_id
        else:
            applied, batch = 0, None
    print(f"schema_migrations: date_guess_backfill_725 dated {applied} undated item(s) from in-file evidence "
          f"{by_source} (batch {batch}; undo it with POST /api/changes/<batch>/undo)", flush=True)


def _mig_search_index_543():
    # #543: fills the FTS5 search index for every item already in the DB (new/changed items are
    # kept current by the triggers from here on). Idempotent: 'rebuild' discards and re-creates the
    # index, so a second run changes nothing. Reports the item count and the time it took.
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_index_543 indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_short_title_seed_542():
    # #542 (owner decision 2026-10-18): the long caption is no longer the title. Every item whose
    # content_description already reads as a name (one line; at most item_title.SHORT_TITLE_MAX
    # characters for a content's own title, item_title.CAPTION_AS_TITLE_MAX for an accepted caption)
    # gets it as its short_title, so its name doesn't change. A longer accepted caption is left for
    # the short-title proposals (captions.propose_short_titles). ONE imaged change-log row as actor
    # `migration` (the generic undo refuses migration rows; the pre-deploy snapshot is the way back).
    # Idempotent: fill-only, a second run finds nothing.
    from .. import actor as actor_ctx, items
    with actor_ctx.acting_as(actor_ctx.ACTOR_MIGRATION):
        res = items.seed_short_titles()
    print(f"schema_migrations: short_title_seed_542 gave {len(res.data['seeded'])} item(s) a short title from "
          f"their own title-shaped description; {res.data['left_long']} long caption(s) left for proposals "
          f"(batch {res.batch_id})", flush=True)


def _mig_tags_one_store_555():
    # #555 (owner decisions 2026-10-18): one tag store, the tree. Every free-text chip an item carries
    # that its tree tags don't is merged into the tree (core/tags.py merge_free_text_into_tree: the
    # rule is there), as ONE imaged change-log row, actor `migration`, counts in `details`. Then the
    # chip-cache triggers go on and every row's `tags` is rewritten from the tree. Idempotent: a
    # second run finds the column equal to the tree and merges nothing.
    from .. import actor as actor_ctx, tags
    with actor_ctx.acting_as(actor_ctx.ACTOR_MIGRATION):
        res = tags.merge_free_text_into_tree()
    conn = get_conn()
    _ensure_chip_triggers(conn, force=True)
    rewritten = _refresh_chip_cache(conn)
    d = res.data
    print(f"schema_migrations: tags_one_store_555 examined {d['items']} item(s), {d['disagreeing']} disagreed; "
          f"{d['links_added']} tag link(s) added to {d['items_changed']} item(s), {d['tags_created']} tag(s) created, "
          f"{d['skipped_card_names']} card-name chip(s) left to the card; {rewritten} chip cache row(s) rewritten "
          f"(batch {res.batch_id})", flush=True)


def _mig_search_tags_tree_555():
    # #555: the search view's tags column reads the tree only (the free-text column is a cache now).
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_tags_tree_555 re-indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_search_filename_parts_762():
    # #762: the search view's filename column now also holds the name split at its CamelCase and
    # letter/digit boundaries ("ElCamino.jpeg" -> "El Camino jpeg"); re-read every item once.
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_filename_parts_762 re-indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_search_short_title_542():
    # #542: the search view's name column now carries short_title too; re-read every item once.
    rows, secs = rebuild_search_index()
    print(f"schema_migrations: search_short_title_542 re-indexed {rows} item(s) in {secs:.2f}s", flush=True)


def _mig_archive_stats_605():
    # #605: archive items stored no member counts unless they were tars (#485): zip and 7z returned {}
    # from the type's embedded_metadata_fn, which is why the work install's Venza manual showed an
    # empty type_metadata. Fill the stats (core/object_types/archive.py archive_stats: counts, sizes,
    # by_type, max_depth) once for every archive row that has none, fill-only (the same merge the
    # upload path uses; nothing is overwritten). Read through a parser child (#644), so a hostile
    # file costs one bounded child. A file that is gone or can't be read is counted and named in the
    # log, never skipped silently. Idempotent: a second run finds every row filled (or unreadable).
    from .. import parse_limits, paths
    from ..object_types import archive
    conn = get_conn()
    rows = conn.execute(
        "SELECT slug, stored_filename, type_metadata FROM capture_events WHERE media_type = 'archive' "
        "AND stored_filename IS NOT NULL AND stored_filename != '' AND COALESCE(redacted, 0) = 0").fetchall()
    filled, missing, failed = 0, [], []
    for r in rows:
        try:
            meta = json.loads(r["type_metadata"] or "{}") or {}
        except (TypeError, ValueError):
            meta = {}  # silent-ok: a garbled column is treated as empty; the merge below keeps nothing it can't read
        if archive.STATS_KEY in meta:
            continue
        path = paths.storage_dir() / r["stored_filename"]
        if not path.is_file():
            missing.append(r["slug"])
            continue
        try:
            stats = parse_limits.call(archive.archive_stats, path, kind=parse_limits.BACKGROUND,
                                      what="archive stats", key=r["stored_filename"])
        except Exception as e:
            failed.append(f"{r['slug']} ({e})")
            continue
        if stats:
            _update_content_metadata(r["slug"], type_metadata={archive.STATS_KEY: stats})
            filled += 1
    print(f"schema_migrations: archive_stats_605 filled {filled} archive item(s); "
          f"{len(missing)} with no stored file{(': ' + ', '.join(missing[:20])) if missing else ''}; "
          f"{len(failed)} unreadable{(': ' + '; '.join(failed[:20])) if failed else ''}", flush=True)


MIGRATIONS = [
    ("drop_legacy_uploads", _mig_drop_legacy_uploads),
    ("curator_snooze_519", _mig_curator_snooze_519),
    ("audit_scrub_settings_559", _mig_audit_scrub_559),
    ("provenance_options_seed_529", _mig_provenance_options_seed_529),
    ("install_config_seed_562", _mig_install_config_seed_562),
    ("hobby_physical_piece_562", _mig_hobby_physical_piece_562),
    ("empty_file_ocr_done_587", _mig_empty_file_ocr_done_587),
    ("writeup_lead_596", _mig_writeup_lead_596),
    ("reextract_utf16_text_607", _mig_reextract_utf16_text_607),

    ("ownership_backfill_604", _mig_ownership_backfill_604),
    ("audit_scrub_secret_routes_559", _mig_audit_scrub_secret_routes_559),
    ("search_index_543", _mig_search_index_543),
    ("ocr_unsupported_724", _mig_ocr_unsupported_724),
    ("reread_legacy_office_724b", _mig_reread_legacy_office_724b),
    ("text_sources_724c", _mig_text_sources_724c),
    ("link_page_text_724d", _mig_link_page_text_724d),
    ("transcripts_724e", _mig_transcripts_724e),
    ("date_guess_backfill_725", _mig_date_guess_backfill_725),
    ("short_title_seed_542", _mig_short_title_seed_542),
    ("search_short_title_542", _mig_search_short_title_542),
    (MIGRATION_555, _mig_tags_one_store_555),
    ("search_tags_tree_555", _mig_search_tags_tree_555),
    ("search_filename_parts_762", _mig_search_filename_parts_762),
    ("reread_xls_790", _mig_reread_xls_790),
    ("caption_pdf_ai_556", _mig_caption_pdf_ai_556),
    ("vocab_seed_556", _mig_vocab_seed_556),
    ("capture_channel_560", _mig_capture_channel_560),
    ("owner_upload_backfill_560", _mig_owner_upload_backfill_560),  # after the channel fill: it reads it
    ("archive_stats_605", _mig_archive_stats_605),
]


def applied_migrations():
    conn = get_conn()
    try:
        return {r["name"]: r["applied_at"] for r in conn.execute("SELECT name, applied_at FROM schema_migrations")}
    finally:
        conn.close()


def pending_migrations():
    done = applied_migrations()
    return [name for name, _fn in MIGRATIONS if name not in done]


def run_pending_migrations():
    """Runs every migration whose schema_migrations row is absent, in order, recording each.
    Returns the list of names applied by THIS call."""
    ran = []
    for name, fn in MIGRATIONS:
        if name in applied_migrations():  # cheap pre-check outside the lock
            continue
        try:
            with transaction() as tx:
                if tx.conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)).fetchone():
                    continue  # another process applied it while we waited for the lock
                t0 = time.monotonic()
                fn()
                tx.conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)", (name, time.time()))
        except MigrationDeferred as exc:
            # #560: refused on purpose; the transaction rolled back, so nothing it did is kept. Loud, not fatal.
            msg = (f"schema_migrations: DEFERRED {name}: nothing was changed, because {exc} "
                   "It stays pending and runs again at the next start.")
            print(msg, flush=True)
            _log.warning(msg)
            continue
        ran.append(name)
        print(f"schema_migrations: applied {name} ({time.monotonic() - t0:.2f}s)", flush=True)
    return ran
