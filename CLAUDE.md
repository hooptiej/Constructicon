# Constructicon

## What this is

A personal media/upload gallery app — "Constructicon" (named after the
Transformers Decepticon that assembles itself out of smaller robots) was
built by forking and repurposing
[ComputerCats-Jason/imagerepo](https://github.com/ComputerCats-Jason/imagerepo)
(a multi-tech IT screenshot repo) into a single-owner personal content site.
That fork is kept as a local-only reference elsewhere — it is not part of
this repo, and its old vocabulary (`tech`, `client`, `ticket_id`) still
echoes through the schema and code as repurposed/vestigial fields (see
below).

It's a single-owner tool on a LAN-only server with no port forward — there
is **no auth/login anywhere in the app**. The network perimeter is the
security boundary, not a login gate. Don't add one without checking with
the owner first.

Long-term goal (see `README.md` for the full writeup): this is the dynamic
backend for a future blog-driven personal site. A future static-export step
will freeze content out of here and publish it to
`hooptiej/hooptiej.github.io` (GitHub Pages). That export step doesn't
exist yet.

## Architecture at a glance

- **`web/`** — FastAPI/Starlette app, split into routers (#547). The ASGI
  entry point is still `web.app:app`.
  - `web/app.py` is only the assembly point: `app = FastAPI()`, the error
    handlers (one shape, see "Actor and errors" below), the static mounts
    (`/static`, `/brand`, `/preview`), middleware (request guard outermost,
    then the actor context, then audit logging), the startup hook (`init_db`
    migrations, OCR self-heal + watchdog, caption queue worker; runs as the
    `system` actor) and the `include_router` calls.
  - `web/routes/` holds one plain `APIRouter()` per area, **no prefix** (each
    route writes its full path): `pages.py` (HTML pages + legacy redirects),
    `items.py` (`/api/upload`, `/api/content`, `/api/image/*`, per-item
    captions, gallery, bulk, tags, search, multi-delete), `cards.py`
    (`/api/projects*`, `/api/project/*`, `/api/cards/*`, `/api/links*`,
    `/api/families/*`, `/api/changes/*`), `hobbies.py`, `curator.py`
    (`/api/curator/*`, `/api/pending-decisions*`), `blog_export.py`
    (`/api/blog-entries*`, `/api/export/*`), `admin.py` (settings, backup,
    delete-all, audit log, redacted/restricted, storage stats, provenance
    options, caption tuning/breaker, desktop-app build), `files.py` (`/f/*`,
    `/downloads/*`, brand-asset and wallpaper listings) and `meta.py`
    (`/healthz`, `/api/version`).
  - `web/shapes.py`: the `_to_*` response shapers and the pure helpers they
    share. `web/common.py`: the `templates` object and its Jinja globals,
    desktop-uploader constants, breadcrumbs / `?rev=` note helpers.
    `web/middleware.py`: the actor middleware and audit logging. Routers import from `common` and
    `shapes`, never from `web.app` or from each other.
  - **Adding a route:** put it in the router for its area, decorated
    `@router.get(...)`/`@router.post(...)` with the full path. Registration
    order matters only when two paths can match the same URL (first one
    wins, e.g. `/api/projects/from-selection` must stay above
    `/api/projects/{project_id}`): keep such routes in the same router, in
    that order. A new area gets a new module plus one `include_router` line
    in `app.py`. A router file's `Path(__file__)` is `web/routes/`, one level
    deeper than the old `web/app.py`. After a routing refactor, run
    `scripts/golden_master.py` before/after (route table, path resolution,
    OpenAPI, ~160 GET snapshots, write round-trips) on the same DB.
  - Page routes render Jinja2 templates from `web/templates/`; `/api/*` is
    the JSON/form API the templates' JS calls; `/f/{slug}` and
    `/f/{slug}/thumb` are the public hotlink + thumbnail routes (stable URLs
    meant to be embedded elsewhere).
- **`core/db.py`** — all SQLite access. No ORM; raw SQL via `sqlite3`, one
  `get_conn()`/`conn.close()` pair per call. This is the schema source of
  truth — read it directly rather than trusting any description here or in
  README.md, which can drift.
- **`core/object_types.py`** — registry of object/media types
  (`ObjectTypeSpec` per `media_type`: thumbnail strategy, OCR eligibility,
  per-type metadata fields). `core/thumbnails.py` and `core/ocr.py`
  dispatch purely off this registry — neither should ever grow a literal
  `if media_type == "..."` branch. Adding a new type means adding one spec
  here, not touching call sites elsewhere.
- **`core/storage.py`** — file storage on local disk under `storage/`
  (gitignored; not baked into the Docker image). Random unguessable slugs
  (`secrets.token_urlsafe`), not sequential IDs.
- **`core/ocr.py`, `core/similarity.py`** — text extraction
  (tesseract via `pytesseract`) and "related items" (perceptual hash +
  sentence-transformers embedding similarity, `all-MiniLM-L6-v2`, baked
  into the Docker image at build time — see Dockerfile).
- **`core/pdf.py`, `core/stl.py`, `core/psd.py`, `core/svg.py`,
  `core/eps.py`** — per-format thumbnail/preview rendering, one module per
  exotic upload type, plugged into `object_types.py`'s registry.
- **`core/captions.py`** (#239) — auto-caption *suggestions* from a local
  Ollama vision model (`moondream`) on the TrueNAS box's GPU, written to
  `type_metadata.auto_caption` (never into `description` on its own).
  Gated per type by `ObjectTypeSpec.caption_capable` (distinct from
  `ocr_capable`: video is captioned but not OCR'd; STL is explicitly
  excluded — wireframe renders produce garbage). Strictly one image at a
  time under `CAPTION_LOCK`. #454: **No routine restarts; restart only on
  failure or if Ollama RAM exceeds ceiling, guarded by cooldown + circuit
  breaker.** Keep-alive model unload is now the primary memory release; restarts
  (via Docker Engine API) are a safety valve for failures or runaway memory.
  **Deploy prerequisites for the app container** — join Ollama's compose
  network (`ollama_default`, external — the ipvlan `questlog-lan` network can't
  reach the host's published `:11434`) so `http://ollama:11434` resolves,
  and bind-mount `/var/run/docker.sock` for safety-valve restarts + RAM reading.
  Without the socket it relies on Ollama's own `keep_alive: 0` model unload;
  set `CAPTION_DISABLED=1` to skip captioning entirely. Tune with the admin
  page's (`/admin`) "Caption tuning" panel (`POST /api/captions/test`) before
  changing the defaults in that module.
- **`core/embedded_metadata.py`** (#255, #265) — metadata the uploaded
  file itself carries, read *synchronously* at upload time via
  `ObjectTypeSpec.embedded_metadata_fn`: an audio file's ID3/Vorbis/RIFF
  tags (via the already-present `ffprobe` — deliberately no `mutagen`
  dependency, see `core/object_types/audio.py`'s docstring for the
  real-file check behind that), seeded into `content_description` +
  `display_name` (the title — both, because the display-name fallback
  chain puts `filename` ahead of `content_description`, so a title stored
  only there would never show on a tile) and `type_metadata`
  (artist/album/track/year/genre); and, since #265, **`content_date`** —
  an image's EXIF `DateTimeOriginal` (Pillow, `core/object_types/image.py`)
  and a video's container `creation_time` (ffprobe,
  `core/object_types/video.py`), gated by a plausibility window (1980 to
  now) that drops metadata sentinels like QuickTime's 1904 epoch. Naive
  source dates go through `core/timeline.py`'s
  `source_datetime_to_epoch` (the Mountain Time convention below).
  Strictly fill-only-missing — never overwrites a value already on the
  row — so it's also safe as a backfill over rows that predate a type's
  hook: `scripts/backfill_content_dates.py` runs the same extraction over
  every undated image/video row and writes through `POST /api/image/{slug}`.
  Called from both `/api/upload` and the MCP `constructicon_upload` tool
  (which bypasses the HTTP route and calls `db.insert_upload` directly —
  any future post-insert step needs wiring in both places, not just the
  route).
- **`core/backup.py`** — standalone `POST /api/backup` backup-to-zip
  (DB snapshot + `storage/`). Deliberately **not** wired into any delete
  path (a past incident wiped storage while only the DB got backed up).
- **`desktop_app/`** — a separate desktop uploader app (own README), built
  and distributed as a downloadable zip from `/downloads/...`. Talks to the
  web app over the same `/api/upload`/`/api/content` HTTP API, identified
  server-side via the `X-Imagerepo-Client: desktop-app` header (not a
  client-supplied identity — this is still single-owner).
- **`mcp_server/server.py`** — the live `constructicon-mcp` sidecar (see
  "MCP server: `constructicon-mcp`" below for the tool surface and how it
  runs alongside `constructicon-web`). Tool names are `constructicon_*`;
  the old imagerepo vocabulary is gone from here.
- **`scripts/`** — one-off/maintenance scripts (YouTube channel sync,
  backfill from the old static site, project-grouping fixups). These talk
  to a running instance over HTTP (`--base-url`), the same discipline the
  app's own UI would use, rather than writing to the DB directly — see
  individual script docstrings for the reasoning and any exceptions.
- **The details panel (#515)** — the project and item pages edit through one
  grouped panel: `templates/_details_group.html` (the `dp_group` macro: a
  fact-sheet view plus a per-group edit form), `static/js/details.js` (edit /
  save / cancel mechanics, the More menu, the "Needs your input" strip) and
  `static/css/details.css`. A group's Save calls the same `/api/...` endpoints
  the old scattered controls called; new fields go into a group, not back onto
  the page loose. Live pages only: nothing here touches the static export.

## Actor and errors (#560, #548; phase A of the service layer #541)

**Who did it: `core/actor.py`.** The actor is a `ContextVar`, set once per entry
point, never a string literal at a call site.
- HTTP: `web/middleware.py`'s `ActorMiddleware` sets `owner-ui` per request
  (`request_actor()` is the hook #467 will point at the logged-in user). The
  request-log rows in `audit_log` record it too.
- MCP: every tool runs inside `acting_as("mcp")` (the `@mcp.tool()` wrapper, below).
- Boot, the OCR watchdog and the caption queue worker run as `system`. A script
  started from `scripts/` defaults to `script`.
- Read it with `actor.current_actor()`; run a block as someone with
  `with actor.acting_as(actor.ACTOR_UI):`. Constants (`ACTOR_UI`, `ACTOR_MCP`,
  `ACTOR_SYSTEM`, `ACTOR_SCRIPT`, `ACTOR_MIGRATION`) live there; `changes.ACTOR_*`
  re-exports them.
- Core ops keep `actor=None` for an explicit override. None means "the context's",
  resolved in `db.insert_change_log`. **Don't pass a literal.**
- ContextVars don't cross `threading.Thread`: start threads with `actor.spawn(fn, *args)`
  (or wrap with `actor.carry(fn)`). `ingest.run_in_thread` already does.
  `run_in_threadpool`, `BackgroundTasks` and asyncio tasks copy the context themselves.
- No context at all falls back to `system` and logs one warning per call site
  (`no actor context at <file>:<line>`). Treat that warning as a missed entry point.

**Refusing: `core/errors.py`.** Raise `AppError(code, message, status=...)` or a
subclass: `NotFound` (404 `not_found`), `Conflict` (409), `InvalidInput` (400
`bad_request`; also a `ValueError`). Existing families are subclasses:
`CardError` (422; 404 `not_found`; 409 for `*_conflict` / `nest_*`),
`curation_queue.QueueError` (400 `bad_request`), `decisions.DecisionNotFound`
(404 `not_found`), `DecisionAlreadyResolved` (409 `already_resolved`),
`UnknownDecisionKind` (400 `unknown_decision_kind`), `InvalidChoice` (400
`invalid_choice`) and `physical_piece` validation (400 `bad_physical_piece`).
Don't map these by hand in routes or tools: the two front ends do it.
- **Web** (`web/app.py` handlers): `{"ok": false, "error": {"code", "message"[, "details"]},
  "detail": <message>}` with the error's status. A plain `HTTPException` gets the same
  shape: its `detail` is unchanged and the code comes from the status (`bad_request`,
  `not_found`, `forbidden`, `conflict`, `unsupported_media_type`, ...). A FastAPI
  validation error keeps its `detail` list, with code `validation_error`. **`detail` stays**
  because the page JS reads it. New JS should read `(body.error && body.error.message) ||
  body.detail`.
- **MCP** (the `@mcp.tool()` wrapper in `mcp_server/server.py`, which replaces
  `MCPServer.tool`; startup refuses if a registered tool bypassed it): an `AppError`,
  `ValueError` or pydantic `ValidationError` raised in a tool becomes `{"ok": false,
  "error": {"code", "message"[, "details"]}}`. Over MCP it is an isError result whose
  text is that JSON. **Not found is an error**: a getter or setter whose target doesn't
  exist returns code `not_found`, never `None` or `False`. Successful returns are unchanged.
- Check with `scripts/test_actor_errors.py` (throwaway DB, no server).

## Service layer (#541): one core module per domain

Every write follows `core/cards.py`'s contract: validate everything first, one
`db.transaction()`, rows written through `db.ImageLog` (row images in `audit_log`), a
`Result`, `dry_run` where it makes sense. Undo is the generic `cards.undo` (POST
`/api/changes/{id or batch}/undo`, MCP `constructicon_undo`). Web routes and MCP tools are thin
adapters. Every table an op images must be in `db.IMAGE_TABLE_KEYS`.
- **Cards** (`core/cards.py`, V2 pieces 1-7), **revisions** (`core/revisions.py`),
  **provenance options** (`core/provenance_options.py`).
- **Items (phase B): item writes go through `core/items.py`; deletes are held 7 days, then
  purged; redacts are held until the owner clicks.** `items.update(slug, **fields)` covers display name/icon, description, client,
  content_description, type_metadata (merged, physical-piece keys cleaned), file provenance,
  highlight, brand asset/role, display-date override and content_date as ONE batch per call (one
  Save = one undo). `redact` / `recover_redacted` / `delete_redacted_file` / `unredact`, `retype` (the media_type change is imaged; the caller's
  runner re-runs OCR/thumbnail/caption; undoing a retype re-runs them for the old type) and
  `delete`. Don't call `db.rename_object`, `update_content_metadata`, `set_type_metadata`,
  `set_provenance`, `set_highlight`, `set_brand_asset`, `set_display_date_override`,
  `set_content_date`, `set_media_type`, `mark_redacted`, `unmark_redacted` or `delete_upload` from
  a route or tool. Deliberate raw exceptions: pipeline bookkeeping (caption status/results in
  `core/captions.py`, upload-time `core/embedded_metadata.py`, a YouTube row's fetched publish
  date, OCR state) and delete-all (phase C).
- **Trash.** `items.delete` / `items.redact` move the file and its thumbnail to
  `<storage>/.trash/<batch_id>/` (same dataset, so ZFS snapshots cover it) and record a `trash`
  row. **Deletes are held 7 days, then purged** (`expires_at` = now + 7 days). **Redacts are held
  until the owner clicks** (owner decision 2026-10-04): the row is `reason='redact'`, `expires_at`
  NULL, and neither the hourly purge nor "Empty trash now" (`POST /api/trash/empty`,
  `constructicon_empty_trash`) ever touches it. A redacted item's `/object/<slug>` page and the
  /admin "Redacted items" list show "The redacted file is still stored on the NAS. Recover it, or
  delete it permanently." with two actions: **Recover** (`items.recover_redacted`,
  `POST /api/image/{slug}/recover-redacted`, MCP `constructicon_recover_redacted`; file,
  `stored_filename` and visibility exactly as before the redact) and **Delete file permanently**
  (`items.delete_redacted_file(slug, confirm)`, `POST /api/image/{slug}/delete-redacted-file`, MCP
  `constructicon_delete_redacted_file`; confirm dialog, logged as `item_redact_erase`, not
  undoable; the item stays redacted, metadata only, and recover then refuses with
  `no_redact_hold`). `unredact` is only for file-less redactions and refuses with
  `redact_hold_exists` while a hold exists. `constructicon_list_trash` lists holds separately
  under `held`. A delete images the `capture_events` row and everything
  pointing at it: `capture_event_relations` (both directions), `item_revisions` (incl. the
  A -> C re-link when B leaves a chain), `project_items`, `post_tags`, `blog_entry_items`,
  `pending_decisions` about the item and their `curator_dismissals` (`decision:<id>`). The
  embedding (a BLOB, never imaged) rides in the trash row. Undo restores rows AND files; if the
  file was purged it refuses with `trash_expired` (410) and writes nothing. The web worker
  (`web/app.py`, `_trash_purge_loop`) purges expired entries hourly (rows kept, `purged_at` set);
  `/admin` "Trash" shows count/size/oldest with a typed-phrase "Empty trash now" (`POST
  /api/trash/empty`, confirm `EMPTY TRASH`); MCP `constructicon_list_trash` /
  `constructicon_empty_trash`. Delete and bulk delete answer with `batch_id`; the pages offer
  Undo (`web/static/js/undo-bar.js`). Check with `scripts/test_items_service.py` (throwaway DB).
- Phase C: membership, tags, relations and one delete-all; phase D: hobbies, blog, the
  stale-decision sweep, then the raw `db.*` writers go private.

## Adding an object type

To add a new object type (issue #448 contract v2):

1. **One new file in `core/object_types/`**: create a module (e.g. `core/object_types/mytype.py`) with:
   - A `preview(ctx: PreviewContext)` function returning `markupsafe.Markup` HTML or None.
   - A `properties(row: dict)` function returning a `dict[str, str]` of display properties.
   - A `register(ObjectTypeSpec(...))` call at the end with both functions, plus optional hooks below.

2. **Required fields**: Every type must declare `preview_fn` and `properties_fn` at registration (enforced, will raise `ObjectTypeContractError` otherwise).

3. **Optional hooks** (all callbacks, all in the same module file): `sniff_fn` (content-based file detection), `pre_store_fn` (accept/reject/defer uploads), `url_match_fn` (classify external URLs), `actions` (per-row UI actions), `edit_fields` (form fields for type_metadata), `preview_assets` (deferred assets), `embedded_metadata_fn` (extract metadata from the file at upload time — used once, never per-view), `writeup_body_key` (the type_metadata key holding write-up text, if this type can be a project write-up), and `sniff_priority` (for resolution when multiple sniffers claim the same extension).

4. **Preview guidance**: previews must escape all data (no raw HTML from untrusted sources; build with `_preview.py` helpers or `markupsafe.escape`) and handle `ctx.mode`: `"live"` (object page) and `"export"` (static site; most types render export markup via the helpers, some deliberately return the download link or `None` to keep the export as it was). A preview that needs its own file reads `ctx.file_path`; never guess at item-dict keys. **Anything expensive goes in `embedded_metadata_fn` (computed once at upload, stored in `type_metadata`), never in `preview_fn`/`properties_fn`, which run on every page view.** Measured lesson: per-view STL mesh parsing took 7.9 s on a real 22 MB file.

5. **Verify BEFORE deploying**: run `docker exec <container> python3 scripts/check_object_types.py` in `constructicon-test` first. It renders every type live + export against hostile synthetic data, flags unescaped output, and exits 1 on any failure. Registration enforcement means **an incomplete type file stops the app from booting at all** (verified: restart loop with `ObjectTypeContractError`), so a bad type deployed to prod = the site is down. Then snapshot real object pages + the static export before/after the change and diff them.

6. **No other changes needed**: All dispatch (thumbnails, OCR, type lists, etc.) works off the registry. No per-media-type branches in templates, no `if media_type == "..."` conditionals in app code — the registry is the single source of truth.

7. **Reference**: see docs/design/object-type-contract-v2.md for the full specification.

## Data model (verify against `core/db.py`'s `SCHEMA` before trusting this — it evolves)

- **`capture_events`** — the core item table. Despite the name (a holdover
  from imagerepo's screenshot-capture origins), this holds *every* kind of
  object: images, PDFs, STLs, PSDs, SVGs, EPS, audio, YouTube links,
  whatever else `object_types.py` registers. Key columns:
  - `slug` — unique, unguessable, used in URLs.
  - `media_type` — loose classifier (`'image' | 'video' | 'youtube' |
    'document' | 'any'`, ...). **Deliberately no CHECK constraint** — not a
    rigid enum, a new value just needs a registry entry in
    `object_types.py`.
  - `source` — upload-pipeline metadata (e.g. `'screenshot'`,
    `'external'`), distinct from `tech`.
  - `tech` — repurposed. Originally "which technician uploaded this" in
    imagerepo's multi-user days; now a free-text **Source** label ("who or
    what added this row, and how"). See `SOURCE_*` constants and
    `source_migrated_from()`/`source_group()` for the fixed vocabulary
    (manual web upload, automated desktop-uploader upload, migrated by
    Claude from a named source, or authored by Claude directly).
  - `client`, `ticket_id` — also vestigial imagerepo IT-ticketing fields;
    still present in the schema/API but not meaningful to Constructicon's
    actual use as a personal gallery.
  - `filename`/`stored_filename` — for uploaded files; `NULL` for content
    with no local file (e.g. `media_type='youtube'`, which uses
    `external_url` instead). `db.insert_upload()` handles the file path;
    `db.insert_content()` is the thin wrapper for the no-file path.
  - `description` — uploader/tagging metadata, vs. `content_description` —
    the content's *own* description/title (e.g. a YouTube video's real
    title). Similarly `timestamp` (capture/upload time) vs. `content_date`
    (the content's own real-world date). These pairs are easy to confuse —
    check which one a given piece of code actually means.
  - `display_name`, `icon` — optional per-object override of the
    filename/content_description/slug and the media type's default badge
    icon.
  - `type_metadata` — freeform JSON bag for per-type properties that don't
    fit a generic column (e.g. YouTube view/like/comment counts; an audio
    file's ID3 artist/album/track/year/genre, #255). One
    shared column so a new object type never needs a schema migration; see
    `object_types.py`'s `MetadataField` for the documented shape per type.
  - `extracted_text`, `perceptual_hash`, `embedding`, `ocr_status` —
    OCR/similarity pipeline state.
  - `redacted` — "file removed, metadata kept" (the detail page's "Remove
    file, keep info" button / `constructicon_redact`; since #541 `items.redact`
    holds the file in the trash with no expiry until the owner recovers it or
    deletes it permanently, see "Trash" above). Since #282 a
    redacted row is hidden from **every** list/browse/search query in
    `core/db.py` (`search`, `list_unfiled_items`,
    `list_recent_items_by_type`, `list_project_items`,
    `list_posts_for_tag`, `list_recent_posts`, and `list_uploaders`'
    totals) and reachable only by its direct `/object/<slug>` link, the
    admin page's (`/admin`) "Redacted items" list (`GET /api/redacted` /
    `db.list_redacted()`) or the MCP `constructicon_list_redacted` tool.
    `POST /api/image/{slug}/unredact` / `constructicon_unredact` flips it
    back — visibility only, for redactions whose file is already gone (refused while a
    file is still held: recover it or delete it permanently first).
    `db.search(include_redacted=True)` is the one escape hatch, used only
    by the two delete-all paths so a full reset doesn't orphan hidden
    rows. There is no `redacted_at` column.
- **`blog_tags`** — the tag tree: `{id, name, slug, parent_id}`, nestable
  to arbitrary depth via self-referencing `parent_id`. Not a fixed
  Section/Category/Tag split — a post can attach to any tag at any depth,
  and to more than one branch at once. `get_or_create_tag(name, parent_id)`
  dedupes per-parent (the same tag name can exist under different
  parents).
- **`post_tags`** — many-to-many join, `(post_slug, tag_id)`, between
  `capture_events.slug` and `blog_tags.id`.
- **`projects`** — hand-curated portfolio collections, deliberately
  *distinct* from the tag tree (auto-grouping by tag was the original plan
  and was rejected in favor of manual curation — "projects are how the
  other objects come together"). Has an optional `tag_id` linking the
  project to a root-level `blog_tags` row of the same name, so tagging into
  a project also surfaces it via ordinary tag browsing.
- **`project_items`** — many-to-many join, `(project_id, post_slug,
  sort_order)`, with a manual `sort_order` for deliberate (non-chronological)
  ordering within a project.
- **`clients`, `client_domains`** — vestigial imagerepo IT-client list
  (Hudu-synced company names, used for OCR auto-tagging). Not part of
  Constructicon's actual personal-gallery use case; present because it
  rode along with the fork.
- **`app_settings`** — generic key/value store for app-level secrets (e.g.
  the YouTube Data API key), so new integrations don't need a
  docker-compose env var wired in from outside. `GET /api/settings` only
  ever reports *presence* of a key, never its value.
- **`pending_decisions`** (#240) — a small generic "don't auto-decide, ask
  the owner" queue: `{id, kind, post_slug, payload JSON, created_at,
  resolved_at}`. Only `kind='project_match'` exists today — written by
  `core/automatch.py` when an upload's filename/folder name matches more
  than one project title (one match auto-adds, tag-name matches always
  auto-apply). Surfaces on the admin page (`/admin`, "Needs your input";
  the header's gear link carries the count badge on every page — it was a
  bottom-right pop-out until #295) via `GET /api/pending-decisions`;
  resolved with checkboxes
  via `POST /api/pending-decisions/{id}/resolve`. Resolved rows are kept
  (resolution stored in `payload.resolution`). A future "ask, don't guess"
  case adds a new `kind` + payload shape, not a table.
  **V2 card decisions** (`card_status`, `card_built_for`, `card_kind`; spec
  `docs/design/v2-cards.md` 4.3) use `post_slug = "card:<project slug>"`, NOT a
  `capture_events` slug — `decisions.list_open()` branches on that prefix
  (validating against `projects`), because the file-row stale check would
  otherwise resolve every card question on first page load.
- **Card kind + status (V2, piece 1)** — `projects.kind` plus
  `activity`/`stage`/`stop_reason` are the *live* status; legacy
  `projects.status` is **frozen** (the static export still filters on it).
  Rules live in `core/card_rules.py` (pure), operations in `core/cards.py`,
  every core write is logged with row images in `audit_log` via
  `core/changes.py`, and the v1 -> v2 mapping is `core/card_migration.py`
  (run from `init_db()`, idempotent: only cards with `stage IS NULL`).
- **Families + nesting (V2, piece 3)** — `family_members(family_id, member_id)`
  is many-to-many membership for `kind=family|collection` cards (not nesting;
  no files move). `projects.parent_id` now means "part of" only:
  `card_rules.validate_nest` (no self/cycle/second parent, neither end a group
  kind) guards `db.create_project`, `db.update_project` and `cards.nest`;
  `card_rules.validate_membership` guards `cards.add_to_family`. Violations are
  `CardError` codes (`nest_*` -> 409, `bad_membership` -> 422), same over HTTP
  and MCP. The AlienWhoop `card_family_members` decision is queued by
  `card_migration.run_v2c_3` (never moves anything); resolving it runs
  unnest -> set_kind family -> add_to_family in one change-log batch.

- **Provenance options (#529)** — the card list (`projects.provenance`) and the
  file list (`capture_events.provenance`) are rows of `provenance_options(scope,
  key, label, sort_order, retired_at)`, seeded idempotently by `init_db` and
  managed in `/admin`. `core/provenance_options.py` owns reads, validation
  (active keys for new writes; a record's existing retired key stays valid) and
  change-logged writes. Pickers, labels and the MCP tools read the table; the
  old constants in `card_rules` / `db.PROVENANCE_TYPES` are only the seed.
  `scripts/test_provenance_options.py` runs on a throwaway DB, no server.

- **Revision chains (#477)** — `item_revisions(old_slug PK, new_slug UNIQUE)`: "new
  supersedes old", one successor and one predecessor per item, so a chain is
  linear (A -> B -> C) and the CURRENT revision is the item with no successor.
  `core/revisions.py` owns validation (no self-link, cycle, redacted item, second
  successor/predecessor: `CardError` codes `bad_revision` / `revision_cycle` /
  `revision_conflict`), name normalization and the change-logged writes (undo via
  `cards.undo`). Browse listings (home Files, Unfiled, user gallery, project
  stacks and grid, hobby loose objects) list current revisions only; `?rev=all`
  shows older ones with a Superseded badge, `db.search()` still finds them
  (`include_superseded` param). The static export and project zip skip older
  revisions. Upload-time: `ingest.auto_match` queues an `item_supersedes`
  pending decision (post_slug = the new file's slug, options = candidate slugs +
  `none`) when the new file's normalized name matches a current item of the same
  type. It only ever asks; the link exists only if the owner answers. Not
  `capture_event_relations`: that one is symmetric and syncs tags/projects.
  `scripts/test_revisions.py` runs on a throwaway DB, no server.

- **Trash (#541 phase B)** — `trash(batch_id, slug, stored_filename, has_original, has_thumb,
  dir, size_bytes, title, reason 'delete'|'redact', created_at, expires_at (NULL = a redact hold), purged_at,
  embedding)`, PK `(batch_id, slug)`, created idempotently in `SCHEMA`. Files live at
  `<storage>/.trash/<batch_id>/<stored_filename>` (+ `<slug>_thumb.jpg`). Item writes go through
  `core/items.py`; deletes are held 7 days then purged, redacts are held until the owner clicks
  (see "Service layer" above).

### Tag hierarchy gotcha — walk the tree, don't just keyword-search

Tags are hierarchical (`blog_tags.parent_id`), e.g. `Kerbal Space Program
Builds` has children like `Walker studies`, `More builds`, etc.
`db.list_posts_for_tag(tag_id)` walks the full descendant subtree
(`_descendant_tag_ids`) to answer "everything under this topic."

**A plain keyword search over title/description/OCR text
(`db.search()`, `GET /api/search`) is a completely different, much
weaker query, and will miss real on-topic items.** Several real items
tagged under a topic's child tags have no keyword match on that topic
anywhere in their text at all. If you (human or agent) are trying to
answer "show me everything about X," walking the tag tree from X's tag
(via `list_posts_for_tag`/`list_tag_tree`) finds items that
`db.search()`/`/api/search` will silently miss. Don't assume a keyword
search over descriptions is a substitute for a tag-tree walk.

## MCP server: `constructicon-mcp` (issue #68, resolved)

Constructicon **does have a live MCP server** — `constructicon-mcp` runs
as its own container alongside `constructicon-web` (and
`constructicon-test-mcp` alongside `constructicon-test`), built from
`mcp_server/server.py`, exposing tools like `constructicon_list_projects`,
`constructicon_create_project`, `constructicon_attach_tags`,
`constructicon_get_posts_for_tag`, `constructicon_search`,
`constructicon_update_project`, `constructicon_add_to_project`, etc.
(`mcp__constructicon-mcp__*` in a session with it configured). #68 tracked
standing this up and was closed 2026-09-02 — **don't assume it's still
missing**; if a session's tools list doesn't show it, that's a
configuration gap for that session, not evidence the server itself is gone.

- #167 tracks auditing this tool surface for real gaps found in later
  work (e.g. whether `constructicon_search`'s `tags` filter shares
  `db.search()`'s row-limit-before-filter bug, whether there's a tool for
  setting a project's cover/write-up) — check it before assuming a
  capability needs building from scratch.
- `mcp_server/server.py` still carries some stale-vocabulary rough edges
  from its imagerepo origin in places; treat naming inconsistencies as
  worth fixing opportunistically, not as evidence the server isn't real.

## Web owns background work; MCP enqueues (#549)

`constructicon-web` and `constructicon-mcp` are two processes on one SQLite DB, so
anything that must happen "only once" cannot live in process-local state.

- **Captions:** only the web process captions. `mcp_server/server.py` sets
  `CONSTRUCTICON_ROLE=mcp`, and in that role `captions.run_caption` marks the item
  caption-pending and inserts into the `caption_queue` table instead of calling the GPU.
  Every caption trigger (upload, import, retype via `ingest.run_in_thread`, a future one)
  goes through `run_caption`, so no call site can slip past. Web's `captions.start_queue_worker()`
  thread drains the queue one at a time through `run_caption`, so `CAPTION_LOCK`, the
  breaker and the cooldown still apply; it leaves the queue alone while the breaker is open
  and backs off 2s -> 15s when idle. A queue row is removed only after the caption ran.
- **Migrations:** one-time data migrations are named steps in `db.MIGRATIONS`, recorded in
  `schema_migrations`, and run by `db.init_db()` in web only, each inside one
  `BEGIN IMMEDIATE` (`db.transaction()`) so check-and-apply is atomic across processes.
  The MCP calls `db.init_db(migrate=False)`: schema DDL only, plus a log line if migrations
  are pending. A new data rewrite goes in `MIGRATIONS`, never straight into `init_db`, and
  must be idempotent (so an existing DB can safely record it on first run).
- **OCR self-heal:** the startup re-fire of `ocr_status='pending'` rows and the periodic
  watchdog live in web only (`web/app.py`), and cover OCR started by MCP too.

## Build / test / run

No test suite exists in this repo today (no `tests/`, no CI config) —
verification is manual, via `scripts/*.py` hitting a running instance's
HTTP API, plus the `constructicon-test` container described below.

Local run (Python 3.14, per the Dockerfile's base image):

```
pip install -r requirements.txt
uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
```

- Needs system packages `tesseract-ocr`, `libcairo2`, `ghostscript` for
  OCR / SVG / EPS thumbnailing to work (see Dockerfile comments for why
  each is needed) — missing them degrades those features rather than
  hard-crashing the app.
- `sentence_transformers` pulls in `torch`; the Dockerfile deliberately
  installs the CPU-only build first (`--index-url
  https://download.pytorch.org/whl/cpu`) — this app container itself has no
  need for CUDA (similarity/embeddings are cheap enough on CPU), so it
  doesn't reach for the box's GPU even though one exists (an RTX 3060 Ti,
  8GB — confirmed via `nvidia-smi` 2026-09-12; passed through to the Ollama
  container via compose's `deploy.resources.reservations.devices` for
  moondream captioning, see above). Do the CPU-only install locally too if
  disk space for CUDA wheels is a concern — it's about this container's own
  footprint, not the box's actual hardware.
- `imagerepo.db` (SQLite file) and `storage/` are created at the repo root
  on first run, gitignored, not baked into the image.
- `seed_test_data.py` seeds a handful of fake-upload rows for exercising
  the gallery UI — note it still calls the old-style `db.insert_upload(...,
  ticket_id, client)` signature and describes itself as "Computer Cats"
  demo data; it's an imagerepo-era leftover, check it still matches
  `db.insert_upload`'s current signature before relying on it.
- `scripts/seed_example_projects.py` seeds a few real example Projects
  from already-backfilled content; not auto-run.

There's no linter/formatter config checked in (no `.flake8`, `pyproject.toml`
lint section, etc.) — match the surrounding file's style.

## Deployment

Runs on a shared TrueNAS box at `10.0.1.78` as a **plain `docker compose`
checkout** — this is *not* deployed via TrueNAS's "Apps"/catalog system.
There is no `docker-compose.yml` committed to this repo; the compose file
lives only on the TrueNAS box itself (outside this checkout), which is why
you won't find one here.

**`git pull`/`git fetch` work again on both checkouts** (#71, fixed
2026-09-06). Each checkout's `origin` remote points at
`git@github.com-constructicon-deploy:hooptiej/Constructicon.git` — a
dedicated SSH `Host` alias in `~/.ssh/config` on the TrueNAS box
(`IdentityFile ~/.ssh/id_ed25519_constructicon_deploy`), backed by a
**read-only deploy key** registered on the repo (`gh repo deploy-key
list --repo hooptiej/Constructicon` to see it) — not a personal token or
a token embedded in the remote URL. Both checkouts were also
`git reset --hard origin/main`'d back onto a clean, tracked `main` branch
at the same time (their prior drift turned out to be entirely
CRLF-line-ending noise from earlier Windows-sourced tar deploys plus
files origin/main had already superseded/renamed — no real divergent
work was discarded; see #71's diagnosis comment for the full file-by-file
check before that reset).

**Deploy with `scripts/deploy.sh`** (added alongside this fix): run it
from inside either checkout —

```
./scripts/deploy.sh            # fetch + reset to origin/main, docker compose restart
./scripts/deploy.sh --build    # same, but `up -d --build` -- only needed when
                                # requirements.txt or the Dockerfile changed
```

This replaces `git pull` (still stuck on the old HTTPS-with-no-creds
failure mode if anyone reverts the remote) and replaces routinely
tar-over-ssh'ing files in by hand. **Tar-over-ssh is now a fallback, not
the default** — reach for it only if `scripts/deploy.sh` itself can't run
(e.g. git credentials break again) or for the desktop app's own build
artifacts, which aren't part of this repo's git history. If you do fall
back to it: `tar czf - core web mcp_server scripts assets | ssh ... 'cd
.../constructicon && tar xzf -'`, confirmed working 2026-09-03 deploying
#103/#95/#88+#90/#92+#93 this way.

**Versions and the release step (#508, CalVer, from 2026-10-03).** Versions are
`YYYY.M.D` (no leading zeros; a second release the same day is `.1`, `.2`, ...).
The box's deploy key is read-only and can't push tags, so the release is cut on
the **dev machine** (gh/git authenticated) between merge and deploy. The
merge-and-deploy pipeline is now:

1. merge the PRs (to `main`);
2. `python scripts/release.py` (dry-run: next version + the PRs since the last
   tag + the CHANGELOG section; refuses on a dirty tree, a non-`main` or
   behind-origin checkout, or nothing new), then `python scripts/release.py
   --execute` (prepends `CHANGELOG.md`, commits `chore(release): <version>`,
   creates the annotated tag, pushes commit and tag, `gh release create`);
   `--version X` overrides the computed version in an emergency;
3. `./scripts/deploy.sh` on the box.

`deploy.sh` fetches tags and, after the reset, writes the gitignored
`core/VERSION.json` (`{version, commit, deployed_at}` from `git describe --tags
--always`; `-dev` appended when the instance has `CONSTRUCTICON_ENV=dev`).
`core/version.py` reads it (fallback `dev`); it shows in the bottom-left page
badge (`base.html`), `GET /api/version` (`{version, commit, deployed_at, env}`),
the MCP `constructicon_version` tool and server info, and the static export
footer ("Built with Constructicon <version>"). `deploy.sh --write-version-only`
runs just that step. A tar-over-ssh deploy has no version file, so the app
shows `dev` (this is expected on constructicon-test for branch work). The ZFS
snapshot name includes the version being replaced. Check with
`python scripts/test_release.py` (the next-version logic).

**Pre-deploy backup = a ZFS snapshot, taken by `scripts/deploy.sh` itself
(#442, 2026-09-30).** Prod's data (DB dir + storage) lives in its own dataset,
`Storage Pool/Media/constructicon`. deploy.sh snapshots it as
`@predeploy-<timestamp>-<commit being replaced>` before touching anything
(instant, atomic across DB/WAL/files, only stores later changes) and keeps the
newest 10. A failed snapshot aborts the deploy; `--no-snapshot` opts out.
constructicon-test's data sits inside the shared `Storage Pool/Media` dataset,
so its deploys skip the snapshot (by design, don't snapshot 2.75 TB for a test).
- **Restore data:** read-only copies are browsable at
  `/mnt/Storage Pool/Media/constructicon/.zfs/snapshot/<name>/` (e.g. copy the
  DB file back with the app stopped). `sudo zfs rollback` discards everything
  newer than the snapshot, so that's an owner call, never automatic.
- **Restore code:** git. The snapshot name records the commit that was running.
  The old `cp -r` of code dirs is retired (it only duplicated git).
- `POST /api/backup` (full DB+storage zip, retention 3) is now for manual or
  off-box exports, not a routine pre-deploy step. Its result includes
  `integrity` (#453).
- The fallback tar-over-ssh path doesn't snapshot: run
  `sudo zfs snapshot "Storage Pool/Media/constructicon@predeploy-<ts>-manual"`
  first.

**The SQLite DB lives in its own directory, never a bare file mount (#453, 2026-09-30).** Both services mount `Media/<instance>/db/` at `/app/data/` with `CONSTRUCTICON_DB_PATH=/app/data/imagerepo.db`. WAL mode keeps `-wal`/`-shm` beside the DB, and the old single-file mount gave web and mcp private copies, which corrupted constructicon-test's DB. Read DB state with a plain `sqlite3.connect(db.DB_PATH)` inside the app container, not a `?mode=ro` URI (that can miss the WAL and show stale rows). Every `/api/backup` result now includes `integrity`.

**Captions: guarded, not per-image-restarted (#454, 2026-09-30).** The old per-image Ollama restart cycled the GPU container ~180 times during a bulk seed and hard-crashed the whole NAS. Captions are back ON everywhere with the #454 safety valve (restart only on failure / RAM ceiling, 10-min cooldown, circuit breaker; status in `GET /api/captions/defaults` → `breaker`, reset via `POST /api/captions/reset-breaker`). All four services join `ollama_default`. Only the **web** services mount `/var/run/docker.sock`; the MCP services deliberately don't (#458), so only web can do safety-valve restarts. After a reboot, check `ollama` is actually running: it can fail to start before the NVIDIA driver loads (see #454).

Two containers run side by side on that box:

- **`constructicon-web`** — the real production instance.
- **`constructicon-test`** — an isolated instance with its own DB/storage,
  used to test changes (e.g. new sync scripts, schema-affecting work)
  before pointing them at production. Its browser tab reads
  **`DEV-Constructicon`** (#310, done: `CONSTRUCTICON_ENV=dev` in its compose,
  per the dev-env-tab-label rule in `~/.claude/CLAUDE.md`). That title is
  also the cheapest "am I pointed at test?" check before anything
  destructive, e.g. `seed_test_from_production.py --execute` wipes whatever
  `--base-url` points at. Check the target's `<title>` and that its IP isn't
  `constructicon-web`'s (container IPs change on recreate).
  `scripts/full_youtube_channel_sync.py`'s
  own docstring is explicit about this discipline: its issue's
  implementation work was scoped to "testing against the isolated
  constructicon-test container," with running against the real production
  instance requiring the owner's separate explicit go-ahead. Follow the
  same discipline for any script that writes data or hits a real external
  API (YouTube, etc.) — default to testing against `constructicon-test`
  first, never assume production is the right target.
  **`constructicon-test` has its own real `youtube_data_api_key` in
  `app_settings`** (confirmed 2026-09-12 via `core.db.get_setting`) — it's
  a genuinely separate key/value, not shared with `constructicon-web`'s,
  so a real (non-dry-run) sync script can be run against it and actually
  hit the live YouTube Data API without touching production data. If it's
  ever missing/rotated: `sudo docker exec constructicon-web python3 -c
  "import core.db as db; db.init_db(); print(db.get_setting('youtube_data_api_key'))"`
  to read it off production, then either the admin page's API Keys "Set"
  field on `constructicon-test`, or `db.set_setting('youtube_data_api_key',
  '<value>')` the same way via `docker exec` into `constructicon-test`, to
  copy it over.
  **Reset to clean `main` right after prod verification (standing step,
  2026-09-12+)**: once a branch has merged and its production deploy is
  verified, the *next* thing to do — as part of that same merge-deploy
  pipeline, not a separate later chore — is reset `constructicon-test`'s
  checkout to clean `origin/main` (`./scripts/deploy.sh` from inside it)
  and restart the container, **then also restart `constructicon-test-mcp`**
  so its bind mount picks up the same clean baseline rather than serving
  stale files from whatever was there before (see gotcha #7 below — this
  sidecar's mount has gone stale on its own before, independent of
  `constructicon-test` itself). The point of this whole step is to leave
  the dev MCP tools (`mcp__constructicon-test-mcp__*`) actually ready to
  exercise live edits the moment the next round of work starts, not just
  the HTTP container. This replaced the older habit of leaving the
  checkout parked on whatever branch was just tested, on the theory that
  it'd carry into the next piece of work: in practice that just meant
  every new session started by wading through a dead branch and stale
  served files instead of a clean baseline. Land on `main` unless the
  *very next* task is already scoped and its branch already exists — in
  that case say so explicitly and park it there instead of resetting,
  rather than resetting reflexively.
  Check `git -C "/mnt/Storage Pool/home/hoop/hoop/constructicon-test"
  log -1` (or just read its `core/`/`web/` files) before assuming this
  container reflects `main` if you're picking up a session that predates
  this rule or one where it wasn't followed.
  **`git` inside that checkout works now** (fixed alongside #71,
  2026-09-06 — same deploy-key SSH remote as `constructicon-web`, see the
  Deployment section above). `scripts/deploy.sh` works here too. The
  live-testing recipe below (tar/scp) is still fine to use for a branch
  that isn't merged to `main` yet — `deploy.sh` only ever pulls `main`.

## Periodic architectural review

Beyond routine per-issue bug fixes, this repo has done one deep architectural
audit so far: a Fable-model deep-dive session on issue #148 (2026-09-08/09)
that surfaced #213 (a HIGH-severity tag-detachment data-integrity bug, fixed
same session) plus a batch of smaller findings (#214/#217 fixed;
#226-#229 deliberately filed-not-fixed, pending a scoping decision).
That kind of review — a fresh model given full codebase context, hunting
specifically for cross-cutting/architectural issues rather than the
single-feature-at-a-time view a normal issue gives — catches a different
class of problem than day-to-day work does, and is worth repeating on a
cadence rather than only after something already went wrong.

**Cadence: roughly every 150 merged PRs.** Baseline: 121 merged PRs as of
2026-09-10 (`gh pr list --repo hooptiej/Constructicon --state merged --limit
300 --json number | jq length`) — next review due somewhere around the
~270-merged-PR mark, then every ~150 after that. This isn't an automated
trigger (deliberately — see `~/.claude/CLAUDE.md`'s note on why standing
always-on checking hooks get killed here for burning tokens unprompted);
it's a number worth checking opportunistically (e.g. when already looking at
`gh pr list` for something else, or when starting a session that's about to
do a batch of new feature work) and flagging to the owner if it's been
crossed, not something to poll for on a schedule.

### Live-testing a branch against `constructicon-test`

**Serialize work against this container — never dispatch two background
agents whose verification touches `constructicon-test` (or the shared main
checkout) at the same time.** Confirmed 2026-09-09/10: two agents running
concurrently (one rebasing/deploying #240, another live-verifying #241)
raced on the same remote directory — one agent's git-based reset wiped the
other's tar-deployed files mid-test, producing a real-looking but entirely
phantom error (`item.caption_capable` Undefined) that cost real time to
chase before the actual cause (the collision itself, not a code bug) was
confirmed. If a subagent's task will touch `constructicon-test` or deploy
anything there, wait for it to fully finish (commit + PR opened) before
starting the next one that needs the same shared infrastructure — don't
run them in parallel just because the tasks themselves are independent.
An agent working in its own isolated git worktree for its *own* checkout
is fine to run alongside others; the shared remote box is the actual
contended resource, not the local checkout.

Real, live verification beats trusting a self-reported "py_compile passed"
or "code review looks fine" claim — see the #67 scaffold PR's actual
history: an agent's own compile/review-only check missed a real circular-
relative-import bug (`from . import eps` needed to become `from .. import
eps` once `core/object_types.py` became a package) that only surfaced when
the module was actually imported with real dependencies.

1. `git worktree add /tmp/verify-<branch> origin/<branch>` locally (or fetch
   + checkout) to get the branch's files without disturbing your main
   checkout.
2. `scp -i ~/.ssh/id_ed25519_truenas -r <changed-dir> hoop@10.0.1.78:"/mnt/Storage Pool/home/hoop/hoop/constructicon-test/<changed-dir>/"`
   — back up the target directory first (`cp -r`) if you'll need to restore
   it afterward; if this container is meant to keep running the branch for
   the *next* round of work, don't restore it, and update the breadcrumb
   above instead.
   **Seen 2026-09-03: a recursive `scp -r <dir> host:.../<dir>` can silently
   no-op** — exit 0, no error, but the remote file's content and mtime don't
   change — even though the destination path is correct (no nesting) and
   the same file `scp`'d individually (no `-r`, explicit source and dest
   file paths) lands every time. Root cause not identified (Windows
   git-bash's `scp` + a remote directory that already exists is the
   suspected trigger). If a live-test result doesn't reflect a change you
   just made, don't just re-restart the container — check the remote
   file's actual content/mtime first.
   **Reliable fix found 2026-09-03, prefer this over per-file `scp` for
   multi-directory deploys**: pipe a `tar` through `ssh` instead —
   `tar czf - core web mcp_server scripts assets | ssh -i
   ~/.ssh/id_ed25519_truenas hoop@10.0.1.78 'cd "<target-dir>" && tar xzf -'`
   — a single stream, no per-directory `scp` semantics to hit the no-op
   bug. Confirmed reliable deploying all of #103/#95/#88+#90/#92+#93's
   changes to `constructicon-web` (production) in one shot, verified by
   grepping the remote files afterward for content unique to each PR.
3. `sudo docker restart constructicon-test`, then `sudo docker logs
   constructicon-test --tail 20` — look for `Application startup complete`
   with no traceback.
   **`constructicon-test`'s actual command has no `--reload` flag** (checked
   via `docker inspect constructicon-test --format '{{.Config.Cmd}}'` and
   its compose file directly, 2026-09-10 — don't assume otherwise). A health
   check run immediately after `docker restart` can still transiently hit
   `ConnectionRefusedError` for a second or two while uvicorn rebinds the
   port (confirmed repeatedly, e.g. 2026-09-09/10) — that's just normal
   restart latency, not a failed deploy. Separately, a `--tail N` log read
   after a session with several earlier restarts will show multiple
   `Shutting down`/`Uvicorn running` pairs in the window, which can look
   like a crash-loop at a glance; it usually isn't — check specifically for
   a traceback between them, and if the last line is a clean `Uvicorn
   running on http://0.0.0.0:80` with nothing after it, just retry the
   health check rather than treating an old restart boundary as new.
4. There's no `curl` inside the app image — verify with `sudo docker exec
   constructicon-test python3 -c "import urllib.request; ..."` against
   `http://localhost:80/...` (the container's *internal* port; check
   `docker logs` for the actual `Uvicorn running on http://0.0.0.0:PORT`
   line rather than assuming it matches the externally-mapped port).
   Hit `/`, `/api/gallery`, `/api/settings`, `/api/projects`, and a real
   `/object/<slug>` + `/f/<slug>/thumb` for an existing row to confirm the
   object-type/thumbnail dispatch path actually works end to end, not just
   that the process boots.
5. Root-owned `__pycache__` dirs can appear under a bind-mounted `core/`
   (written by the container's own process) — plain `rm -rf` from the
   `hoop` user will hit `Permission denied` on those. Clean them from
   *inside* the container instead: `sudo docker exec constructicon-test
   find /app/core -name __pycache__ -exec rm -rf {} +`.
6. **Prefer real files over synthetic ones for upload/render-path tests.**
   Before hand-building a test file (a minimal STL triangle, a hand-crafted
   PSD header, etc.), check whether `constructicon-web` (production) already
   has a real upload of that `media_type` — its DB is a separate SQLite file
   from `constructicon-test`'s, reachable read-only via `sudo docker exec
   constructicon-web python3 -c "import sqlite3; ..."` (see the ticket_id
   data-check pattern used for #84 as an example query shape). If a real row
   exists, its stored file lives under production's storage bind mount
   (`/mnt/Storage Pool/Media/constructicon/storage/` — check
   `constructicon-web`'s actual mount source with `docker inspect`, don't
   assume the path) and can be `scp`'d down and re-uploaded to
   `constructicon-test` for a more representative test than a synthetic
   minimal file. As of 2026-09-02, production had at least one real upload
   for every registered type except `svg` — if a type has zero real
   production uploads when you need one, ask the owner to upload a real
   example rather than only ever testing against synthetic data for that
   type.
7. **A container's bind mount can go stale after files change underneath it
   while it's running** — seen twice (2026-09-02 on `constructicon-test-mcp`,
   2026-09-03 on `constructicon-test` itself): `docker exec <container> ls
   /app/web` (or `/app/core`) comes back empty even though the host
   directory clearly has real files, and even a plain `python3 -c
   "import os; os.listdir(...)"` agrees it's empty from inside. A restart
   (`sudo docker restart <container>`) fixes it immediately. If a
   verification step in this recipe reports an empty directory, missing
   module, or import error that makes no sense given the host-side files,
   restart the container before assuming the deploy itself is broken — don't
   spend time debugging a deploy that's actually fine underneath a stale
   mount snapshot.
- The Dockerfile comments confirm `core/`, `web/`, and `mcp_server/` are
  **bind-mounted at run time, not baked into the image** — an ordinary code
  deploy is a `git pull` + container restart, not a rebuild. Only changes
  to `requirements.txt` or system packages (the `apt-get install` line)
  require an actual `docker compose up --build`.

Typical redeploy from a checkout on the TrueNAS box:

```
git pull
sudo docker compose up -d --build
```

(`--build` is cheap/fast when only bind-mounted code changed, since the
image layers are unchanged and get cached — but always include it rather
than guessing whether this particular change needs a rebuild.)

**`scripts/deploy.sh --build` can report success without actually
restarting the containers** (confirmed 2026-09-07, deploying #197/#198):
it printed "Rebuilding and restarting" and `docker compose up -d --build`
reported both containers as `Running`, but since the image layers were
fully cached and compose saw no config change, it left the already-running
processes alone — the new bind-mounted code sat on disk, unread, while the
old code kept serving. `git log -1` inside the checkout showing the right
commit is **not** sufficient proof the fix is live. After any deploy,
verify the actual running process picked up the change — `sudo docker exec
<container> grep <fix-specific-string> <file>` against the file *inside
the container*, not the host checkout — and if it's not there yet, `sudo
docker restart <container>` explicitly rather than re-running deploy.sh.

### SSH / access to TrueNAS

- Passwordless `sudo docker` access via SSH as `hoop@10.0.1.78`, using a
  dedicated key at `~/.ssh/id_ed25519_truenas` (not the default
  `~/.ssh/id_ed25519`).
- **Nonstandard home directory** — `hoop`'s home on that box is *not*
  `/home/hoop`. It's:
  ```
  /mnt/Storage Pool/home/hoop/hoop
  ```
  (note the literal space in `Storage Pool`, and the doubled `hoop` at the
  end). Quote/escape this path in any shell command that touches it.
  Don't hardcode `/home/hoop` anywhere — it will silently resolve to the
  wrong place (or nowhere) on this box.

## Non-obvious gotchas worth knowing up front

- **`media_type` has no CHECK constraint on purpose.** It's a loose
  classifier, not an enum — don't add one. New types are registered in
  `object_types.py`, not validated at the DB layer.
- **`description` vs. `content_description`, `timestamp` vs.
  `content_date`.** Easy to grab the wrong one of each pair — they mean
  different things (uploader metadata vs. the content's own
  description/date). See the `capture_events` notes above.
- **Backfilling a `content_date` from a naive/timezone-less real-world
  date (EXIF `DateTimeOriginal`, a blog post's "Jul 31, 2017" with no
  time) — interpret it as Mountain Time (`America/Denver`, DST-aware via
  `zoneinfo`), not UTC.** `content_date`/`timestamp` are stored as UTC
  unix seconds either way; this is only about which timezone a naive
  source date gets treated as before converting. Decided 2026-09-12
  during the Desk Build project's date backfill (owner: "we should have
  written down our preference here is Mountain time") — the first
  attempt used UTC by mistake (matching `backfill_from_hooptiej_site.py`'s
  older convention, which parses a bare calendar date as UTC midnight);
  redone with Mountain Time once corrected. A future backfill script
  should follow Mountain Time, not copy the older UTC convention — and
  should do it by calling `core/timeline.py`'s `source_datetime_to_epoch`
  (the one implementation of this rule since #265; a tz-aware datetime
  passes through untouched, a naive one gets `America/Denver`), not by
  re-deriving it. `zoneinfo` needs a tz database: the Debian-based
  container has one, a Windows checkout does not — `tzdata` is in
  `requirements.txt` for that reason.
- **Projects are curated, not auto-generated.** Don't "fix" the Projects
  section to auto-derive from tags — that was the original design and was
  explicitly rejected in favor of manual curation via the `projects` /
  `project_items` tables.
- **`clients`/`client_domains`/`ticket_id` are imagerepo-era vestiges.**
  They still work (e.g. OCR auto-tagging by client domain) but aren't part
  of Constructicon's actual personal-use case — don't build new features
  assuming they're a live, meaningful concept for this app the way they
  were for imagerepo.
- **`mcp_server/server.py` IS live** (`constructicon-mcp` / `constructicon-test-mcp`
  containers — see the MCP section above). If a session can't see
  `mcp__constructicon-mcp__*` tools, that's a session configuration gap,
  not evidence the server is missing.
- **`GET /api/settings` never returns real secret values, only presence.**
  Scripts needing the actual value of a stored setting (e.g. the YouTube
  API key) must call `core.db.get_setting(...)` directly from a process
  that shares the DB (e.g. via `docker exec` into the app container), not
  through the HTTP API.
- **`seed_test_data.py`'s old call signature** — see "Build / test / run"
  above; don't assume it still runs cleanly against the current
  `db.insert_upload` without checking.
- **Two parallel local checkouts may exist on the owner's Windows
  machine** (`Constructicon/` and `constructicon-work/`, on different
  branches) — both are this same repo, not separate projects. If you're
  working across machines/sessions and something looks like uncommitted
  work-in-progress, check which checkout/branch you're actually in before
  assuming it's stale or foreign.
