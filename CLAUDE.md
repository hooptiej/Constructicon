# Constructicon

## What this is

A personal media/upload gallery app — "Constructicon" (named after the
Transformers Decepticon that assembles itself out of smaller robots) was
built by forking and repurposing
[ComputerCats-Jason/imagerepo](https://github.com/ComputerCats-Jason/imagerepo)
(a multi-tech IT screenshot repo) into a single-owner personal content site.
That fork is kept as a local-only reference elsewhere — it is not part of
this repo. Its old vocabulary mostly echoes no more: `tech` survives, repurposed as the
Source label, and the dead IT-ticketing columns and client tables were dropped in #704 (see
"Data model").

It started as a single-owner tool on a LAN-only server with no port forward, where the network
perimeter was the security boundary. Auth is on (#467; owner decisions 2026-10-07): **step 1**
added users, sign-in and first-run setup; **step 2 enforces it**: every page and API needs at least
a viewer, roles are checked, restricted items are admin-only, and scripts / the MCP send the
install token. See "Users and sessions" and "Auth enforcement" below.

Long-term goal (see `README.md` for the full writeup): this is the dynamic
backend for a blog-driven personal site. The static-export step exists: it freezes content out
of here into a self-contained static site, previews it at `/preview`, and publishes it to GitHub
Pages (the owner's `hooptiej/hooptiej.github.io`; the targets are per-install config since #562).
See "Static export" and "Install config" below.

## Architecture at a glance

**Architecture section verified against commit c6d2831 (main, after #629, plus the #552 changes
themselves) on 2026-10-08.** Every module, route file, script and command named in this section was
checked against the code at that commit (issue #552). If a claim here disagrees with the code, the code wins: fix the claim, and move
the date and commit forward when you re-verify.

- **`web/`** — FastAPI/Starlette app, split into routers (#547). The ASGI
  entry point is still `web.app:app`.
  - `web/app.py` is only the assembly point: `app = FastAPI()`, the error
    handlers (one shape, see "Actor and errors" below), the static mounts
    (`/static`, `/brand`, `/preview`), middleware (the trusted-proxy rewrite outermost (#646), then the
    security headers (#645), the request guard, the body caps (#649), the actor context, session, access
    gate, audit logging and CSRF; see "How the actor is set"), the startup hook (`init_db`
    migrations, the stale-decision sweep, OCR self-heal + watchdog, caption queue
    worker, the trash purge and decision-sweep loops; runs as the `system` actor) and the
    `include_router` calls.
  - `web/routes/` holds one `RoleRouter` (an `APIRouter` whose routes all carry a role label,
    see "Roles and policy" below) per area, **no prefix** (each
    route writes its full path): `pages.py` (HTML pages + legacy redirects),
    `items.py` (`/api/upload`, `/api/content`, `/api/image/*` incl. `replace-file`, per-item
    captions, `/api/gallery`, `/api/home/files`, bulk, tags, search, multi-delete), `cards.py`
    (`/api/projects*`, `/api/project/*`, `/api/cards/*`, `/api/links*`,
    `/api/families/*`, `/api/changes/*`), `hobbies.py`, `curator.py`
    (`/api/curator/*`, `/api/pending-decisions*`), `blog_export.py`
    (`/api/blog-entries*`, `/api/export/*`), `admin.py` (settings, backup,
    delete-all, audit log, redacted/restricted, storage stats, provenance
    options, caption tuning/breaker), `files.py` (`/f/*`,
    brand-asset and wallpaper listings), `meta.py`
    (`/healthz`, `/api/version`) and `auth.py` (sign-in, setup, users, my password; see "Users and
    sessions"). `app.py` includes them in this order: meta, pages, admin, items, curator, files,
    cards, hobbies, blog_export, auth.
  - `web/shapes.py`: the `_to_*` response shapers and the pure helpers they
    share. `web/common.py`: the `templates` object and its Jinja globals,
    breadcrumbs / `?rev=` note helpers.
    `web/middleware.py`: the actor middleware and audit logging. Routers import from `common` and
    `shapes`, never from `web.app` or from each other.
  - The rest of `web/`: `auth.py` (session, access-gate and CSRF middleware), `roles.py` (`RoleRouter`,
    `requires`, `NON_ROUTE_ROLES`), `request_guard.py` (same-origin guard and the audit-log redaction
    rules), `body_limit.py` (per-route request-body caps, #649), `proxy_trust.py` (the real scheme and client
    address behind a trusted reverse proxy, #646), `security_headers.py` (CSP, framing and the other
    browser security headers on every app response, #645), `content_security.py` (serving user files safely, #610), `files_feed.py` (the paged
    file-grid feed, #624/#553), `item_grid.py` (the shared file grid's config, #553), `guides/` (in-app markdown guides), `export_templates/` (the static site's
    Jinja templates), `templates/` and `static/` (css, js; `static/js/mountain-time.js` is the JS twin
    of `core/datefmt.py`).
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
  - **`def` or `async def` (#550):** a route is a plain `def` unless it truly awaits I/O. FastAPI runs a
    `def` route in a worker thread; an `async def` route that calls `db.*`, `items.*`, a subprocess or
    any other blocking work freezes the whole app for everyone. A route that needs the body
    (`request.form()` / `.json()` / `.body()`) stays `def` and reads it with
    `web.common.from_request_thread(request.form)`; one that must stay `async` wraps each blocking call in
    `await run_in_threadpool(fn, ...)`. The actor ContextVar is copied into the worker thread either way.
    Export build/publish share one lock (a second one is 409 `export_busy`). Check with
    `scripts/test_async_blocking_550.py` (a static scan of every `async def` under `web/`, plus a live
    `/healthz`-while-slow proof).
  - Page routes render Jinja2 templates from `web/templates/`; `/api/*` is
    the JSON/form API the templates' JS calls; `/f/{slug}` and
    `/f/{slug}/thumb` are the public hotlink + thumbnail routes (stable URLs
    meant to be embedded elsewhere).
- **`core/db.py`** (~5,000 lines) — the SQLite layer and the schema source of truth (`SCHEMA`,
  `MIGRATIONS`, `init_db`, `transaction()`, `ImageLog`, every read query, the pipeline writers). No
  ORM; raw SQL via `sqlite3`, one `get_conn()`/`conn.close()` pair per call (or one shared
  connection inside `db.read_session()` / `transaction()`). **It is not the only module with raw SQL:**
  eleven other core modules (`backup`, `blog`, `card_migration`, `curator_needs`, `hobbies`,
  `install_config`, `membership`, `physical_piece`, `provenance_options`, `revisions`, `tags`) and
  `web/routes/admin.py` also call `.execute(...)` themselves (the service-layer cleanup that would pull
  them in is tracked in the issues, not done). Read the schema in `core/db.py` rather than trusting
  any description here or in README.md, which can drift. Writes still go through the service modules
  ("Service layer" below).
- **`core/object_types/`** — a package, one module per object/media type (`image.py`, `video.py`,
  `pdf.py`, `stl.py`, `psd.py`, `svg.py`, `eps.py`, `audio.py`, `youtube.py`, `code.py`, ... plus the
  underscore helpers `_preview.py`, `_textstats.py`, `_office.py`, `_pe.py`). `__init__.py` holds the
  registry: `ObjectTypeSpec` (thumbnail strategy, OCR / caption eligibility, preview and properties
  functions, per-type metadata fields), `register()`, `get_object_type()`, `mismatch_reason()` and
  `is_restricted()`; it imports every module in the package at load (`pkgutil.iter_modules`), so a
  new file registers itself. The per-format rendering (PDF, STL, PSD, SVG, EPS) lives inside those
  modules, not in `core/pdf.py` and friends (they no longer exist). `core/thumbnails.py` and
  `core/ocr.py` dispatch purely off this registry: neither should ever grow a literal
  `if media_type == "..."` branch. Adding a new type means adding one module here, not touching call
  sites elsewhere (see "Adding an object type").
- **Module map: the rest of `core/`** (one line each; the module docstring is the detail):
  - *Writes, the service layer (#541):* `items.py`, `membership.py`, `tags.py`, `cards.py`,
    `hobbies.py`, `blog.py`, `decisions.py`, `reset.py`, `revisions.py`, `users.py`,
    `install_config.py`, `provenance_options.py`; `changes.py` (the change-log API), `actor.py`
    (who did it), `errors.py` (`AppError` family), `besteffort.py` (logged, rate-limited non-fatal
    failures). See "Service layer" and "Actor and errors".
  - *Rules and policy (pure or near-pure):* `card_rules.py` (card enums, labels, validation, no DB),
    `card_level.py` (the 0-5 curation pips, and the one definition of the checks the Curator score
    shares with them, #545), `card_payload.py` (slim item records for grids),
    `timeline.py` (an item's REAL date or None, card spans from real dates only, #725;
    `source_datetime_to_epoch`), `item_dates.py` (what date an item SHOWS: real, inherited from its
    card "≈ ... · from <card>", or Undated; the undated items' guesses, #725), `date_guess.py` (the pure
    date guesser, #725), `datefmt.py` (the one place
    a stored date becomes text, in Mountain Time, #544), `item_title.py` (`title_of`, #542),
    `markdown_render.py`, `roles.py` + `policy.py` (who may use a door / see an item; `access_log.py`
    records opens of sensitive items), `physical_piece.py` (made-by-hand item fields).
  - *The upload pipeline:* `ingest.py` (one pipeline for web and MCP uploads), `automatch.py`
    (filename-based project/tag matching, queues `project_match`), `embedded_metadata.py`,
    `thumbnails.py`, `ocr.py`, `similarity.py`, `captions.py`, `text_sources.py` (#724 c), and
    `safe_fetch.py` (#724 d: **the one way the app fetches a web address it was given**, see
    "Outbound fetches" under "Data model"; never call `httpx.get` / `urllib` on a stored URL directly).
  - *Curator:* `curator.py` (completeness scoring), `curator_needs.py` (derived nudges),
    `curation_queue.py` (the one queue). See "Decisions and the Curator".
  - *Install, files, export:* `paths.py`, `storage.py`, `backup.py`, `install_token.py`, `version.py`,
    `site_export.py` + `project_export.py` (see "Static export"), `card_migration.py` (the V2 card
    migration run from `init_db()`).
- **`core/storage.py`** — file storage on local disk under `storage/`
  (gitignored; not baked into the Docker image). Random unguessable slugs
  (`secrets.token_urlsafe`), not sequential IDs.
- **`core/paths.py`** (#578, #576) — every on-disk location (storage, `<storage>/.trash`,
  `<storage>/../backups` or `CONSTRUCTICON_BACKUP_DIR` (#442), `exports/` with its `current` and `.publish`) comes from here and is
  **resolved on each call** from `CONSTRUCTICON_STORAGE_DIR` / `CONSTRUCTICON_EXPORTS_DIR` (defaults =
  `<repo>/storage`, `<repo>/exports`). Never compute one of these at import time or from `__file__`.
  `build_site(config, out_dir=X)` writes only `X`: the `exports/current` refresh and the prune of old
  builds happen only for the default (no `out_dir`) build. **Tests:** every in-process `scripts/test_*.py`
  starts with `import _testenv; TMP = _testenv.isolate("name-")` (temp DB + storage + exports, then
  asserts the resolved paths are in a temp dir and outside the repo, exiting otherwise); a test that
  talks to a live server (`test_office.py`) says so at the top and never calls delete-all/empty-trash.
  Since #467 step 2 `isolate()` also sets a fresh per-run install token (never the container's):
  use `_testenv.client(app)` for an admin TestClient; a bare TestClient is anonymous.
- **`core/ocr.py`, `core/similarity.py`** — text extraction
  (tesseract via `pytesseract`) and "related items" (perceptual hash +
  sentence-transformers embedding similarity, `all-MiniLM-L6-v2`, baked
  into the Docker image at build time — see Dockerfile). **The model loads offline (#658):**
  `similarity._get_model` defaults `HF_HUB_OFFLINE=1` and loads with `local_files_only`, and the
  image sets `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` after the bake, so a load never calls the
  Hub (it used to: 144 s of retries with no network). An empty cache fails fast with the download
  command; a checkout outside the image downloads once with `HF_HUB_OFFLINE=0 python -c "from core
  import similarity; similarity._get_model()"`. Check with `scripts/test_hf_offline_658.py`.
- **`core/captions.py`** (#239) — auto-caption *suggestions* from a local
  Ollama vision model (`moondream`) on the TrueNAS box's GPU, written to
  `type_metadata.auto_caption` (never into `description` on its own).
  Gated per type by `ObjectTypeSpec.caption_capable` (distinct from
  `ocr_capable`: video is captioned but not OCR'd; STL is explicitly
  excluded — wireframe renders produce garbage). Strictly one image at a
  time under `CAPTION_LOCK`. **#542: every caption comes with a short-title
  suggestion** in `type_metadata.auto_caption_title`, DERIVED from the caption
  (`captions.title_from_caption`: first sentence, the "The image shows" lead-in and a
  leading article cut on phrase boundaries, the first clause, at most 8 words). No
  title prompt: measured 2026-10-25, moondream answers every constrained prompt ("in
  five words", "a short phrase") starting mid-word, on every photo tried; only "Describe
  this image." gives whole sentences. The caption queue also carries title-only
  proposals (`caption_queue.mode = 'title'`, with their `batch_id`; real captions
  drain first), which ask "Describe this image." and derive the title from the answer. **#743: a caption that starts mid-word** ("urn with...", "ids
  and...": `captions.starts_mid_word`, a lower-case first word with no capital that
  isn't an article or plain sentence start) **is garbage** like an empty answer: the
  ladder moves on, and a ladder of fragments ends `failed` (back in needs-caption),
  never `done`. There was never a prefix strip in this pipeline; the fragments are
  moondream's own output (all 37 on prod at step 0). `captions.scan_mid_word()` is the
  read-only scan (`GET /api/captions/mid-word`, admin); `requeue_mid_word` puts them back
  in the needs-caption queue as ONE undoable batch (`POST /api/captions/mid-word/requeue`,
  dry run by default); `scripts/caption_cleanup.py scan | requeue-midword` drives both. #454: **No routine restarts; restart only on
  failure, guarded by cooldown + circuit breaker.** Keep-alive model unload is
  the primary memory release; a restart is a safety valve for failures.
  **#643: the app container never holds the Docker socket.** The restart is a
  POST to the `ollama-restarter` sidecar (`ops/ollama-restarter/restarter.py`,
  stdlib only), the only container that mounts `/var/run/docker.sock`. It restarts
  only the container in ITS `RESTARTER_CONTAINER` (never from the request), needs
  the shared secret (`X-Restart-Token`, from `secrets/ollama_restart_token`), allows
  one attempt per `RESTARTER_MIN_INTERVAL_SECONDS` (default 300) and makes no other
  Docker call (so #454's RAM-ceiling check, which read container stats, is gone).
  It sits on an `internal: true` compose network shared only with the web service,
  no published port. Web side: `CAPTION_RESTART_URL` + `CAPTION_RESTART_TOKEN_FILE`;
  unset, unreachable, 429 or 401 = "no valve this time", logged, breaker untouched.
  **Where Ollama comes from (#687): the install's own compose.** `docker-compose.yml.example`
  defines an `ollama` service: image pinned by tag AND digest (`ollama/ollama:<tag>@sha256:...`;
  bump both halves together, then live-test captions on constructicon-test), behind the compose
  profile **`captions`** (`docker compose --profile captions up -d`, or `COMPOSE_PROFILES=captions`
  in `.env`; without it the app runs with captions off, so set `CAPTION_DISABLED=1`), together with
  the restart sidecar (no captions = no container holding the Docker socket). It publishes **no
  port**: web (and the test instance) reach it as `http://ollama:11434` over the `internal: true`
  network `captions`; its only other network, `ollama-egress`, is its own way out for model pulls.
  Direct testing: `docker exec ollama ollama list`, or the commented `127.0.0.1:11434` port. GPU is
  an override file, `docker-compose.gpu.yml` (NVIDIA reservation); CPU works as shipped. **One
  Ollama per GPU:** a second instance on the same box shares it. Its entrypoint,
  `ops/ollama/serve-and-pull.sh`, starts `ollama serve` and pulls `CAPTION_OLLAMA_MODEL` (the same
  setting `core/captions.py` reads; default `moondream`) when it is not on the volume
  (`OLLAMA_DATA_DIR`, default `./ollama-data`), so a fresh install needs no manual `ollama pull`
  and a restart never re-pulls; a failed pull is logged and Ollama serves anyway. The app side is
  two settings, both with the old behaviour as default: `CAPTION_OLLAMA_URL` (default
  `http://ollama:11434`) and `CAPTION_OLLAMA_MODEL`. The MCP never calls Ollama (it queues, #549),
  so it doesn't join `captions`. Check with `scripts/check_compose_ollama.py` (static: pin, no
  port, profile, names) and `scripts/test_restart_sidecar_643.py`.
  **The home install, until the owner migrates it (#687's PR has the steps):** Ollama is still a
  separate hand-made project (`~/ollama/docker-compose.yml` on the NAS, `ollama/ollama:latest`, GPU,
  port 11434 published to the LAN), and all four app containers join its network
  (`ollama_default`, external) to resolve `http://ollama:11434` (the ipvlan `questlog-lan` network
  can't reach the host's published port). Set `CAPTION_DISABLED=1` to skip captioning entirely. Tune with the admin
  page's (`/admin`) "Caption tuning" panel (`POST /api/captions/test`) before
  changing the defaults in that module.
- **`core/embedded_metadata.py`** (#255, #265) — metadata the uploaded
  file itself carries, read *synchronously* at upload time via
  `ObjectTypeSpec.embedded_metadata_fn`: an audio file's ID3/Vorbis/RIFF
  tags (via the already-present `ffprobe` — deliberately no `mutagen`
  dependency, see `core/object_types/audio.py`'s docstring for the
  real-file check behind that), seeded into `content_description` +
  `short_title` (#542) + `display_name` (the title, so it is the owner-visible name from the
  start; see `core/item_title.py`) and `type_metadata`
  (artist/album/track/year/genre); and, since #265, **`content_date`** —
  an image's EXIF `DateTimeOriginal` (Pillow, `core/object_types/image.py`)
  and a video's container `creation_time` (ffprobe,
  `core/object_types/video.py`) (#725: and a PDF's own `CreationDate`,
  `core/object_types/pdf.py`), gated by a plausibility window (1980 to
  now) that drops metadata sentinels like QuickTime's 1904 epoch. #725: a
  row that still has no real date then takes a date written in its FILE
  NAME (`core/date_guess.py` `from_filename`), recorded in
  `type_metadata.content_date_source` (see "Dates" below). Naive
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
- **`mcp_server/server.py`** — the live `constructicon-mcp` sidecar (see
  "MCP server: `constructicon-mcp`" below for the tool surface and how it
  runs alongside `constructicon-web`). Tool names are `constructicon_*`;
  the old imagerepo vocabulary is gone from here.
- **`scripts/`** — four kinds of file; the folder says which is which (#552):
  - *Tests and checks* (`test_*.py`, `test_*.js`, `check_*.py`): run them with `scripts/run_checks.py`,
    see "Build / test / run".
  - *Live tools* (not retired): `run_checks.py`, `deploy.sh`, `release.py`, `run_tool.sh`,
    `golden_master.py`, `seed_test_from_production.py`, `full_youtube_channel_sync.py`,
    `backfill_content_dates.py`, `regenerate_thumbnails.py`, `make_type_samples.py` + `drop_test.py` (#720),
    `caption_cleanup.py` (#743 / #542: the mid-word scan and re-queue, the short-title proposals),
    and the helpers `_http.py` and `_testenv.py`.
    **None of `scripts/` is mounted into the app containers (#634)**: where each kind runs is
    "Scripts: what runs where" below. `reset_password.py` and `mcp_token.py` moved into the app as
    `core/manage/` (`python3 -m core.manage.<tool>`); the files left at the old paths are stubs that
    print the new command and exit non-zero.
    The ones that talk to a running instance do it over HTTP (`--base-url`), the same discipline the
    app's own UI would use, rather than writing to the DB directly; see each docstring for the reasoning
    and any exceptions. Since #467 step 2 each calls `_http.install(<base url>)` (`scripts/_http.py`) so
    its requests carry the install token; see "Auth enforcement".
  - *Spent one-shots:* `scripts/archive/`. Nothing imports them and no doc tells anyone to run them.
    **Each one refuses to run (#633):** its first line, before any import or argument parsing, is
    `raise SystemExit("Retired script: ran on prod around <date> (#N). Kept for reference only; it will
    not run. See CLAUDE.md, Scripts.")`, so nothing else in the file executes (some of them could still
    write from there: `cleanup_delete_orphans.py --execute` deleted rows). `scripts/check_archive_guard.py`
    fails when a file under `archive/` lacks that guard (it also runs each guarded file to prove it
    exits, and checks `run_checks.py` never collects `archive/`). A new one-shot goes in `scripts/`
    while it is live and moves to `archive/` once it has run, with the guard as its first line.
  - `scripts/data/`: input files for scripts (the V2 curation suggestions JSON).
- **`core/manage/`** (#634) — the operational command-line tools that ship with the app, because
  `core/` is mounted in every install and `scripts/` is not: `reset_password` (see "Users and
  sessions") and `mcp_token` (see "Auth enforcement"). Run from the repo root or `/app`:
  `python3 -m core.manage.<tool> ...`. Both run as actor `script` (`core/actor.py` treats a process
  started from `core/manage/` like one started from `scripts/`). `mcp_token generate` must keep
  running on a bare checkout with no app dependencies (stdlib + `core/install_token.py` only;
  `core/__init__.py` and `core/manage/__init__.py` import nothing). Check with
  `scripts/test_manage_634.py`.
- **The details panel (#515)** — the project and item pages edit through one
  grouped panel: `templates/_details_group.html` (the `dp_group` macro: a
  fact-sheet view plus a per-group edit form), `static/js/details.js` (edit /
  save / cancel mechanics, the More menu, the "Needs your input" strip) and
  `static/css/details.css`. A group's Save calls the same `/api/...` endpoints
  the old scattered controls called; new fields go into a group, not back onto
  the page loose. Live pages only: nothing here touches the static export.

## Scripts: what runs where (#634)

The app containers mount app code only (`core/`, `web/`, `mcp_server/`, `assets/`), never `scripts/`
(owner decision 2026-10-15, option (c) on #634), so `docker exec <web> python3 scripts/x.py` does not
work on an install. What is in `scripts/` sorts into three kinds, and each runs somewhere else:

- **Tests and checks** (`test_*.py`, `test_*.js`, `check_*.py`, run by `run_checks.py`): unchanged.
  They run in a throwaway container with the checkout mounted read-only and no network (see "Tests and
  checks"), never inside a live install. The few that need a live instance (`check_curator_queue.py`,
  `test_office.py`) and the "run it against constructicon-test's own image" checks
  (`check_object_types.py`, `check_routes_roles.py`) use the `run_tool.sh` form below.
- **Deploy and release** (`deploy.sh`, `release.py`, `run_checks.py` itself): unchanged. They run on
  the host (the box's checkout, or the dev machine for `release.py`), not in a container.
- **Maintenance and install-cleanup tools**, which need the instance's DB, storage or token:
  `backfill_content_dates.py`, `regenerate_thumbnails.py`, `full_youtube_channel_sync.py`,
  `seed_test_from_production.py` (its docstring has its own throwaway-container recipe; it talks
  HTTP to the target), `golden_master.py`. Run them from the host, in the install's checkout:
  ```
  ./scripts/run_tool.sh <web container> scripts/<tool>.py [args...]
  # e.g. ./scripts/run_tool.sh constructicon-test scripts/backfill_content_dates.py --base-url http://localhost:80 --dry-run
  ```
  `run_tool.sh` starts a one-off `docker run --rm` twin of the running web container: the same image,
  environment and mounts (`--volumes-from`: DB dir, storage, secrets, `core/`, `web/`), its network
  namespace (`--network container:<web>`, so `http://localhost:<port>` is the running app, as it was
  from `docker exec`), the image's own user (uid 1000), plus `./scripts` mounted read-only for that run
  only. `RUN_TOOL_TTY=1` adds `-it` for a script that prompts. **Not** `sudo docker compose run --rm
  -v ./scripts:/app/scripts:ro web ...`: on the NAS that fails with "Address already in use", because
  the web and MCP services have a fixed `ipv4_address` on `questlog-lan` that a second container can't
  take (checked 2026-10-15 on constructicon-test). Test a writing tool against constructicon-test before
  production, as always.
- **The two operational tools are not in `scripts/` any more:** `python3 -m core.manage.reset_password`
  and `python3 -m core.manage.mcp_token` (see `core/manage/` above) run in place with `docker exec`, or
  (`mcp_token generate`) on the host from the checkout before any container exists.

## Actor and errors (#560, #548; phase A of the service layer #541)

**Who did it: `core/actor.py`.** The actor is a `ContextVar`, set once per entry
point, never a string literal at a call site.
- HTTP: `web/middleware.py`'s `ActorMiddleware` sets `anonymous` per request; a signed-in
  request is overridden to `user:<username>` by `web/auth.py`'s `SessionMiddleware` (#467 step 1),
  an install-token request to `token` by its `AccessMiddleware` (#467 step 2). The request-log rows
  in `audit_log` record it too. `owner-ui` is the pre-step-2 anonymous actor: old rows and
  in-process tests still carry it, no request does.
- MCP: every tool runs inside `acting_as("mcp")` (the `@mcp.tool()` wrapper, below).
- Boot, the OCR watchdog and the caption queue worker run as `system`. A script
  started from `scripts/`, or a tool started as `python3 -m core.manage.<tool>` (#634), defaults to
  `script` (`actor._process_default` reads `sys.argv[0]`; with `-m` it is the module's file).
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

**No silent excepts (#551 item 5).** An `except` must never swallow a failure with no trace.
- **Hides a real failure** (a malformed `tags` field, a garbled JSON body): raise `AppError` /
  `HTTPException` (clean 400) or let it propagate, so the shared handlers shape it.
- **Legitimately best-effort** (an optional metadata extractor, a probe of a service that may be
  down): keep it non-fatal but log it with context: `besteffort.warn(log, "what failed", exc,
  slug=...)` from `core/besteffort.py`, with `log = logging.getLogger("constructicon.<module>")`.
  It logs at warning level and is rate limited per site (one full line per minute, with a count of
  the suppressed repeats), so a hot path can't spam the log. Narrow the exception type where you can.
- **Pure control flow or input validation** (a sentinel exception that unwinds a transaction, a
  parse that returns None for a malformed value, a cleanup race): a `# silent-ok: <reason>` comment
  on the `except` line, with a real reason.
- The audit middleware (`web/middleware.py`) records a body it can't parse as
  `{"_unparsed": true, "content_type": ..., "bytes": n}`, never `{}` and never the raw body.
- `python scripts/check_no_silent_except.py` (AST, no server) enforces it: it fails on an `except`
  in `core/`, `web/` or `mcp_server/` whose body is only `pass` / `continue` / `break` / `return
  <constant>` / a constant assignment with no logging call and no `raise`, unless it carries a
  `# silent-ok:` reason or sits in the checker's commented `ALLOW` list. Check the behaviour with
  `scripts/test_swallowed_errors.py` (throwaway DB).

## Roles and policy (#557, groundwork for auth #467)

Two separate checks, and a request must pass **both**: the route's **role** (may this actor use
this door at all?) and the item **policy** (may this actor see this item?). Each had one switch;
#467 step 2 flipped both (`roles.ENFORCE = True`, `policy.RESTRICTED_VIEW_ROLE = roles.ADMIN`).

**Roles: `core/roles.py` + `web/roles.py`.** The ladder is `public < viewer < editor < admin`.
- Every router in `web/routes/` is a `RoleRouter(default_role=roles.X)`. A route without its own
  label gets the router default; a route that needs another role says so in its decorator:
  `@router.post("/api/settings", dependencies=requires(roles.ADMIN))`. The route's label
  **replaces** the default (never stacks), so every route has exactly one.
- **Adding a route:** put it in its area's router as usual. If it needs a different role than the
  router default, add `dependencies=requires(roles.X)`. Rule of thumb: pages and GET reads viewer,
  curation writes editor, anything that runs the install (settings, secrets, backup, delete-all,
  audit log, publish, binaries everyone downloads, option management, caption tuning, emptying
  the trash, permanent deletes, whole-card/hobby conversions) admin; public only for what must work
  with no login (`/f/<slug>` hotlinks, `/healthz`). A new non-APIRoute (a mount) goes in
  `web.roles.NON_ROUTE_ROLES`.
- `require_role(role)` (the dependency) records the label (`request.state.required_role`, written
  by the audit middleware into the request log's `audit_log.required_role` column) and, with
  `roles.ENFORCE` (True since #467 step 2), refuses a request below it: 401 `unauthorized` when the
  actor holds no role (anonymous), else 403 `forbidden`, shared error shape. `roles.role_of(actor)`:
  `user:<name>` -> that user's role; `token`, `mcp`, `script`, `system`, `migration` (and the legacy
  `owner-ui`) -> admin; `anonymous` and anything unknown -> public (fail closed). Before any route
  runs, `web/auth.py`'s `AccessMiddleware` resolves the request to its route or mount label
  (`web.roles.required_role_for`) and turns anonymous requests away (see "Auth enforcement").
- `python scripts/check_routes_roles.py [--list]` (run in the container: it imports the app) fails
  when a route has no label, two labels, or a mount isn't listed; it prints the count per role.

**Item policy: `core/policy.py`.** "Can this actor see this item?" is decided there and nowhere else.
- Direct doors (one item: `/object/<slug>`, `GET /api/image/<slug>` and its revisions/similar,
  `/f/<slug>` and its thumbnail, MCP `get` / `download` / `get_related` / `list_revisions`) keep
  their own "no such row" 404 and then call `policy.require_view(row)`: if `can_view` says no it
  raises `NotFound` (404 `not_found`, the same answer as a missing item, so existence isn't leaked).
- Lists of what a card/hobby/entry holds (project and hobby pages, MCP `get_project`, Related,
  similar, blog entries, brand assets, wallpapers) call `policy.filter_visible(rows)`.
- General browsing (search, gallery, home, unfiled, tag pages, uploader pages) filters in SQL with
  `policy.sql_browse_clause(prefix)` inside `core/db.py` (formerly `db._not_restricted`), plus
  `filter_visible` where the route holds the rows.
- Exports (static site, project zip) call `policy.filter_exportable(rows)`: restricted items never
  leave the install, whoever asks.
- **Since #467 step 2** `RESTRICTED_VIEW_ROLE = roles.ADMIN` ("restricted means locked, not just
  hidden"): `can_view` is False for a restricted item unless the actor is an admin (a signed-in
  admin, the install token, the MCP, in-process scripts), so every direct door answers 404 and every
  list drops it; browsing hides restricted items from everyone; exports exclude them.
  `policy.viewable_item(slug)` (row or 404, then the policy) guards every single-item web route,
  reads AND writes (an editor can't edit, redact, delete or relate a restricted item either);
  `policy.require_file(row)` guards `/f/<slug>` and its thumbnail (also 404 for a REDACTED item
  unless admin); `decisions.list_open()` leaves out questions about items the actor can't see.
  Don't decide restriction anywhere else (`object_types.is_restricted` / `restricted_types` outside
  the policy fail the check below).
- **Since #603** "restricted" = sensitive: a restricted type OR an item flagged "This is sensitive",
  visible to admins AND the item's uploader (`can_view`), see "Ownership and sensitive items" below.
- **Since #604 step 3** the policy also decides CARDS and HOBBIES: a private card / hobby (and
  everything under it) is hidden from everyone but admins, its owner and its members, and `can_view`
  hides an item on a hidden card (see "Private cards and hobbies" below). A card door asks
  `policy.can_view_card` / `filter_visible_cards` (or resolves its card through `cards.get_card`);
  `check_policy_doors.py` rule 5.
- **Writes refuse what the caller can't see (#709), in the service op itself**, so web, MCP and any
  future per-user token are covered: `policy.viewable_rows(slugs, missing_ok=...)` for a set
  (`items.delete`, `unrelate`, `membership.add_files` / `remove_files`, `tags.merge_item_tags`; bulk
  delete, add-to-card and tag), `items.get_item` / `tags._get_item` for one item (update, caption,
  relate, redact, attach / detach / set tags). **Owner decision 2026-10-17: "tell them no, and
  why."** A hidden item refuses the WHOLE request with 403 `forbidden` (`policy.refusal`): "Nothing
  was changed: 1 of the 3 selected items is marked sensitive, and only its uploader or an admin can
  change it." (or "a restricted file type", or both), `details: {refused: [slugs], reason: sensitive |
  restricted_type | hidden | mixed, reasons: {slug: reason}}`; nothing written. Same code, message and
  details over MCP. The grid's bulk bar shows that message as is. An unknown slug keeps its old
  meaning (`missing_ok` skips it, else 404). **Not this rule (owner, PR #727):** `set_sensitive` /
  `set_provenance` (bulk and single) keep their own 404 for a hidden item, and the single-item WEB
  routes still answer 404 first (`viewable_item`, the read-door rule above; saying why there is #730).
  Check: `scripts/test_bulk_visibility_709.py`.
- **Adding a door** (anything that hands out an item or a list of items, web or MCP): call the
  policy in it, and add it to `DOORS` in `scripts/check_policy_doors.py`. That script (AST, no
  server) fails when a known door stops calling `policy.`, when a GET route or MCP read tool reads
  items (`get_by_slug`, `search`, `list_project_items`, ...) without calling it (unless `EXEMPT`
  with a reason), or when other code decides restriction itself. It can't tell whether the call is
  on the right rows: `scripts/test_role_policy.py` (throwaway DB) proves the behaviour by flipping
  the switch, then by denying everything, and checks every door refuses.

## Users and sessions (#467 step 1: users, sign-in, first-run setup)

Owner decisions 2026-10-07 (#467): built-in auth, one install per customer, HTTPS later. Step 1
added accounts and attribution; step 2 enforces them (next section, "Auth enforcement").

**Model (`core/users.py`, the service module; tables in `core/db.py`).**
- `users(id, username UNIQUE COLLATE NOCASE, display_name, role viewer|editor|admin, password_hash,
  created_at, last_login_at, disabled)`. Usernames are 2-32 of `[A-Za-z0-9._-]`, kept as typed,
  unique in any case.
- Passwords: stdlib `hashlib.scrypt` (no dependency), n=2^15 r=8 p=1 (~95 ms, 32 MB on the NAS),
  16-byte random salt, stored as `scrypt$n$r$p$salt$hash` (urlsafe base64), so the cost can rise and
  old hashes still verify. `hmac.compare_digest`. Minimum 10 characters, nothing else. A NULL hash =
  no usable password.
- Ops (each validated, one `db.transaction()`, ONE change-log row with actor from context):
  `create_user`, `set_role`, `set_password` (logs `password_changed: true`, never the hash; ends the
  user's other sessions), `set_disabled` (disable ends every session), `delete_user`,
  `create_first_admin`. The last enabled admin can't be demoted, disabled or deleted (409
  `last_admin`). Other codes: `bad_username`, `bad_password`, `bad_role`, `username_taken` (409).
- **User rows are deliberately not imaged** (`users` is not in `db.IMAGE_TABLE_KEYS`): row images
  would copy the hash into `audit_log`, and the generic undo is an editor door that would bypass the
  last-admin rule. The change-log row carries a `details` summary instead; generic undo refuses it
  (`undo_refused`). The inverse op is the undo (enable, set the role back, reset the password).
- `sessions(token_hash PK, user_id, csrf_token, created_at, last_seen, expires_at)`: the cookie holds
  a random 32-byte token; only its sha256 is stored. Sliding expiry 30 days (`SESSION_DAYS`), the
  row touched at most every 10 minutes (and the cookie re-sent then when the expiry moved), **capped
  absolutely at 30 days after sign-in** (`SESSION_MAX_AGE_DAYS`, from `created_at`; #651, owner
  decision 2026-10-15, to revisit with passkeys / public exposure, #641 / #664): however often it's
  used, a session dies then and the user signs in again for a fresh window. Past the cap is refused
  exactly like past `expires_at` (anonymous: the 302 to `/login` or the 401) and purged at the next
  sign-in; the check reads `created_at` itself, so rows from before the cap (whose `expires_at` may
  reach past it) obey it too. Logout deletes the row. Check with `scripts/test_session_cap_651.py`.
- Sign-in backoff (`users.limiter`, in memory, web process: resets on restart, would split across
  workers): "lock the attacker, not the account" (#647). After 5 failures a key backs off 2^(extra
  failures) s after the last failure, else 429 `too_many_attempts` (`details.retry_after`) without
  checking the password. The source **IP** key is the heavy one (max 15 min,
  `LOGIN_MAX_DELAY_SECONDS`); the **username** key is capped at 5 s (`LOGIN_USER_MAX_DELAY_SECONDS`)
  and only applies to an address that itself has recent failures (its own, or against that
  username: the `("pair", ip, name)` key), so the right password from a clean address signs in even
  while the account is hammered from elsewhere; a wrong one from a clean address still counts. No
  address = one shared `unknown` key. When a key first starts backing off (once per key per window)
  one change-log row `login_throttled` records it (`details`: `throttled` ip|username, `client_ip`,
  the username only when it is a real account). A success clears that username's and that IP's counts
  (behind a Docker bridge or NAT every client can share one IP, so the IP count must not outlive a good
  sign-in), but not the address's failures against OTHER usernames. Check with
  `scripts/test_lockout_647.py`. The IP is `proxy_trust.client_ip`: behind a proxy
  listed in `CONSTRUCTICON_TRUSTED_PROXIES` it is the real client's (#646, "Behind a reverse proxy"),
  so each client has its own bucket; without that setting a proxy's clients all share the proxy's. A wrong user, wrong
  password and disabled account all answer the same 401 `invalid_login` (an unknown user still
  spends a hash's time). A password over `MAX_PASSWORD_LENGTH` (1,024) is that same failure, decided
  before the user lookup and without hashing it (#649).
- `delete_everything` keeps `users` and `sessions` (`reset.KEPT_TABLES`).

**How the actor is set (`web/auth.py`).** Middleware order: (`ProxyHeadersMiddleware`, #646, and
`SecurityHeadersMiddleware`, #645, outside everything) -> origin guard -> `BodyLimitMiddleware`
(#649, see "Request-body caps") -> `ActorMiddleware` (anonymous) -> `SessionMiddleware` ->
`AccessMiddleware` (install token + sign-in gate, step 2) -> audit logger -> `CsrfMiddleware` ->
routes. A request with the
`constructicon_session` cookie that resolves to a live session runs as actor `user:<username>`
with `users.current_user()` set (pages' `current_user()` Jinja global; `roles.role_of` reads it).
No cookie, or a dead one = `anonymous`. The cookie: HttpOnly, SameSite=Lax, Path=/,
Max-Age = the session's remaining life (30 days at sign-in, never past the #651 cap), `Secure` only when the request is HTTPS (`proxy_trust.is_https`: directly, or via a
trusted proxy, #646; never on the plain-HTTP home install). Change-log rows AND
request-log rows record `user:<name>`; request-log rows also record `client_ip` (#646).

Outside that whole chain sit `SecurityHeadersMiddleware` (#645; see "Browser security headers" under
"Auth enforcement") and, outermost of all, `proxy_trust.ProxyHeadersMiddleware` (#646; see "Behind a
reverse proxy"), which is why every layer sees the real scheme and client address.

**CSRF.** On top of the origin guard (#558): a POST/PUT/PATCH/DELETE **that carries a live session
cookie** must send that session's token in `X-CSRF-Token`, else 403 `csrf_failed` (in the request
log). Install-token requests (scripts; `Authorization: Bearer`) never need it:
a browser can't attach that header cross-site without a CORS preflight the app never grants, and a
token request carries no session even when a cookie rides along. Injection: `base.html` renders `<meta name="csrf-token">` plus
`static/js/csrf.js` (loaded before every other script) only on a signed-in page; csrf.js wraps
`fetch` and `XMLHttpRequest` and adds the header to same-origin state-changing calls. **There are
no plain HTML POST forms** (every form is submitted by JS, including login/setup, which use
`method="post"` only so a no-JS submit can't put a password in a URL); a new plain POST form would
have to submit through fetch, since the server reads only the header.

**Routes (`web/routes/auth.py`, JSON bodies, #558 JSON gate).** Public: `GET /login`, `GET /setup`
(404 once any user exists), `GET /logout` (a page with a Sign out button: a GET never signs out),
`POST /api/auth/login|logout|setup`, `GET /api/auth/me`. Viewer: `GET /account/password`,
`POST /api/account/password` (`current_password`, `new_password`; 401 `not_signed_in` /
`wrong_password`). Admin: `GET/POST /api/users`, `POST /api/users/{id}/role|password|disable|enable|delete`
(Admin > Users panel). The header's user chip shows the name (-> Change my password) and Sign out,
or "Sign in". **No secrets in logs (#559):** `request_guard.AUDIT_ROUTE_RULES` / `AUDIT_ROUTE_PATTERNS`
log no body values for login, setup, my password, user create and the admin reset, and only the
setting's name for `POST /api/settings`; error reasons on those keep only the code. Rules match by
route path (trailing slashes ignored), and the field-name backstop (also inside nested JSON) covers
the rest; a route whose plain field merely *looks* secret (`key`) goes in `AUDIT_PLAIN_FIELDS`.
**A new route that takes a secret must get a rule;** `scripts/test_audit_secrets_559.py` fails when
a Form field named like a secret has none. Migration `audit_scrub_secret_routes_559` scrubbed the
old rows.

**First run.** While `users` is empty, every page (and `/login`) redirects to `/setup` (step 2),
which creates the first admin, signs them in and fills `install_config.owner_name` if unset (that
fill is logged as actor `anonymous`: nobody is signed in yet). `/setup` is 404 once any user exists.
`/admin` (reachable with the install token before then) shows "Create the admin account".
- **Claiming it needs the install token (#648).** Reaching `/setup` first is not enough (a scanner
  that finds a new public hostname could otherwise win the race and own the install).
  `POST /api/auth/setup` takes `install_token` (`users.SETUP_PROOF_FIELD`) beside `username`,
  `password` and `display_name`; the page asks for it and says where it is (`secrets/constructicon_token`
  in the install's folder on the server, made at rollout step 2). `users.require_setup_proof`
  decides, in this order: 404 `not_found` once any user exists (whatever the token, so it is no
  oracle afterwards); a request already running as the token (`Authorization: Bearer`) passes; no
  token configured -> 403 `setup_unavailable` (the page shows the reset-script command instead of the
  form: fail closed); 429 `too_many_attempts` while the address backs off (the sign-in limiter's
  rules, its own `setup-ip` key, so 5 free tries); missing -> 403 `setup_proof_required`; wrong -> 403
  `setup_proof_wrong` (constant-time compare). The token is never stored or logged (the route's
  `AUDIT_ROUTE_RULES` entry logs no body values). It grants nothing new: whoever holds it is already
  admin over the API and the MCP.
- **One winner (#648).** `users.create_first_admin` checks "no users" and inserts inside one
  `db.transaction()` (`BEGIN IMMEDIATE`, SQLite's write lock taken before the read), so concurrent
  setups, even from the web and MCP processes, can't both win: the loser gets the 404.
- **Escape hatches, unchanged and needing no setup proof:** the install token over the API (`POST
  /api/users` with role admin, Admin > Users, or setup with the Bearer header) and
  `python3 -m core.manage.reset_password --create-admin` on the server (shell access is the proof).
- Check with `scripts/test_setup_race_648.py` (throwaway DBs: refusals, the backoff, five HTTP setups
  and six OS processes at once, the hatches, a no-token install, nothing secret in the audit log).

**Reset tool (on the box).** `sudo docker exec -it <web container> python3 -m core.manage.reset_password
<username>` (prompts twice; `core/manage/reset_password.py`, #634, was `scripts/reset_password.py`, which
is now a stub that prints this command), `... <username> --generate` (prints a generated password once, no -it
needed), `--enable` (re-enable), `--create-admin <username> [--display-name N]` (creates an admin, or
makes an existing user an enabled admin with a new password: recovery for an install with no usable
admin), `--list`. Runs as actor `script`; never prints a hash.

Check with `scripts/test_auth_step1.py` (throwaway DB) and `scripts/test_manage_634.py` (the module
path in a real child process, its change-log actor, the stubs).

## Auth enforcement (#467 step 2: login everywhere, roles, the restricted lock, the install token)

Owner decisions 2026-10-07. **Login scope is everything**, roles are enforced, restricted items are
admin-only, and the one install token is how every non-browser client gets in (as admin).

**What's open without login (role public):** `/healthz`, `/login`, `/logout` (the page), `/setup`
(only while there are no users, 404 after; claiming it needs the install token, #648),
`/api/auth/login|logout|setup|me`, `/static`, `/brand`,
and the `/f/<slug>` hotlinks + thumbnails **except restricted and redacted items** (an admin session
or the token gets them, an admin sees the redacted item's 410; anyone else 404, as if missing).
**Everything else needs at least viewer**, including `/preview` (the export preview) and the
OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`), which are mounts gated in middleware via
`web.roles.NON_ROUTE_ROLES`.

**The gate (`web/auth.py` `AccessMiddleware`, before any body is read).** It resolves the request to
its route or mount label (`web.roles.required_role_for`, walking `app.routes` with the lazily
included routers expanded; an unknown path counts as viewer, so a stranger can't map the API by
404s) and compares it with `roles.role_of(actor)`:
- anonymous + a page (GET/HEAD outside `/api/`, not `/openapi.json`) -> 302 to `/setup` while there
  are no users, else to `/login?next=<path?query>`; `/login` validates `next` (`_safe_next`: one
  leading `/`, no `//`, no `\`, no control characters or whitespace anywhere, at most 2000 chars,
  else `/`), so it can't be an open redirect;
- anonymous + anything else -> 401 `unauthorized` (shared shape, `WWW-Authenticate: Bearer`);
- signed in but below a MOUNT's label -> 403 `forbidden`; below a ROUTE's label -> 403 `forbidden`
  from `web.roles.require_role` (the labels from #557; a page answers the same JSON 403).
- **No trust for loopback or LAN addresses**: no session and no token = anonymous, whatever the IP
  or `X-Forwarded-For`. (A trusted proxy's `X-Forwarded-For`, #646, only changes which address the
  limiter and the request log record; it never grants anything.)

**Request-body caps (#649, `web/body_limit.py`).** Every request body has a per-route ceiling,
enforced before anything reads it (uvicorn has no limit of its own, and the audit logger buffers
mutating `/api/*` bodies before the route runs, the public sign-in routes included).
`BodyLimitMiddleware` (pure ASGI, just inside the origin guard) reads nothing: it wraps `receive`,
so a declared `Content-Length` over the limit trips before a byte is pulled, and the bytes actually
received are counted, so a chunked or lying body is cut off once it passes the limit (at most the
limit is ever held). The answer is 413 `payload_too_large` in the shared shape with `details:
{max_bytes, limit_kind}` and `Connection: close`. The audit logger refuses a declared-oversize body
itself (no read, the route never runs) and logs `{"_body": "not read: ..."}` plus the code; when a
route's own reading trips it, the middleware swaps whatever answer came back (form parsing wraps the
exception and would say 400) for the 413, and the log says 413 too.
- **auth** (`/api/auth/*`, `/api/account/*`, `/api/users*`): `CONSTRUCTICON_MAX_AUTH_BODY_KB`, default 64.
- **upload** (`/api/upload`, `/api/image/<slug>/replace-file`, the only routes that take a file):
  `CONSTRUCTICON_MAX_UPLOAD_MB` (`storage.MAX_BYTES`) + 1 MB of multipart framing. They still stream;
  the per-file checks in the routes are unchanged. An anonymous upload is 401 from the access gate
  before any of its body is read.
- **everything else**: `CONSTRUCTICON_MAX_BODY_KB`, default 1024 (the largest real JSON/form body on
  the owner's archive is a ~6 KB `type_metadata`).
- A bad or non-positive value falls back to the default. **A new route that takes a file must be
  added to `_UPLOAD_PATH`**: `scripts/test_body_cap_649.py` fails when the routes with an `UploadFile`
  parameter and the upload-limit routes differ. A reverse proxy in front (later, HTTPS) should set
  its own body limit too, as a second layer. The MCP server (port 8100) is not covered: every request
  there needs the install token first.

**User files are never served as active content on the app origin (#610).** Browsers carry the
session cookie for this origin, so an uploaded `.html` / `.svg` rendered as a page from `/f/<slug>`
would be stored XSS against whoever opens it (an admin included). `web/content_security.py` is the one
place that decides: every user-file response (`/f/<slug>`, `/f/<slug>/thumb`, the `media/` files under
`/preview`) carries `X-Content-Type-Options: nosniff`; active content (HTML, XHTML, SVG, XML, by media
type, extension or a sniff of the first bytes) also carries the sandbox CSP (`RENDERED_HTML_CSP`, shared
with the Rendered view: opaque origin, no script, no network); HTML/XHTML is also
`Content-Disposition: attachment` (look at it in the in-app Rendered view); SVG stays inline so a hotlinked
`<img>` keeps working. **#701:** with an original name, everything else is `inline` when the browser
shows it itself (PDF, JPEG/PNG/GIF/WebP/AVIF/BMP, MP4/WebM/Ogg video, the common audio types, `text/plain`:
`object_types.BROWSER_VIEWABLE_TYPES`, an exact allowlist, so PSD/TIFF stay downloads) and `attachment`
otherwise, both keeping the name (`filename*=` / `filename=`). **#723:** the allowlist holds canonical names
only; `object_types.BROWSER_TYPE_ALIASES` folds the other names Python (by version) or a host's mime.types
gives the same formats (`audio/vnd.wave` / `audio/x-wav` / `audio/wave` -> `audio/wav`, `audio/x-m4a` ->
`audio/mp4`, `audio/x-flac` -> `audio/flac`, ...) through `object_types.browser_media_type`, which both the
inline decision and `serve_file`'s `Content-Type` use (so `.wav` is served `audio/wav`); add an alias there,
never a second name to the allowlist. **#729:** `.opus` (Python 3.14: `audio/opus`; also `audio/x-opus`,
`audio/x-opus+ogg`) folds to `audio/ogg` (Ogg Opus's registered type, RFC 7845) and `.m4v` (`video/x-m4v`,
`video/m4v`) to `video/mp4`, so both play inline. `video/quicktime` is deliberately not aliased (.mov stays a download). Those inline types are sniffed too, so markup
renamed `.pdf` is still HTML (attachment + CSP). The object page's link follows the same set
(`object_types.original_link(item)`, used by `_preview.thumb_with_original_link` and `object_detail.html`):
"View original <Type>" in a new tab, or "Download original <Type>" in place. Check with
`scripts/test_view_original_701.py`. **#703, the raw view** (`/f/<slug>/raw`, viewer, the same
`policy.require_file` door and view log as `/f/<slug>`): a type with `ObjectTypeSpec.raw_text_fn` (only
`code`) shows its file as `text/plain; charset=utf-8`, `inline` under its original name, nosniff, and the
sandbox CSP on every response (`content_security.serve_raw_text`). It is never served as its own type,
so markup inside (HTML in a `.py`, a PHP template) shows as text and cannot run; UTF-16/32 is transcoded to
UTF-8. When the type's function says no (binary, over `code.RAW_MAX_BYTES` = 32 MB), or the type has no
raw view, the route answers with the download (`serve_file(..., download=True)`: `/f/<slug>`'s bytes and
type, always `attachment`; Python maps `.c`/`.h` to `text/plain`, so `/f/` alone shows those inline). Check with
`scripts/test_raw_view_703.py`. **Any new route that streams a stored file must go through
`content_security.serve_file` (or add `file_headers`)**, never a bare `FileResponse` on a user file.
Thumbnails are generated JPEGs (nosniff only). Check with `scripts/test_f_sandbox_610.py`. Longer term: a
separate user-content origin.

**Browser security headers (#645, `web/security_headers.py`).** One pure-ASGI middleware, the
**outermost** layer (it wraps the origin guard, so every refusal carries the headers too), adds
defaults to every HTTP response. A header the response already set always wins.
- **Everywhere:** `Referrer-Policy: strict-origin-when-cross-origin` (a link out sends our origin, never
  the path; not `same-origin`, because YouTube embeds refuse to play with no Referer) and
  `Strict-Transport-Security: max-age=<n>` **only when the request is HTTPS** (`proxy_trust.is_https`)
  and `CONSTRUCTICON_HSTS_SECONDS` is a positive number (default off). Never over plain HTTP: the home
  install is HTTP-only. It does NOT read `X-Forwarded-Proto` itself (a client could forge it); behind a
  TLS proxy, `CONSTRUCTICON_TRUSTED_PROXIES` (#646, "Behind a reverse proxy") is what makes the request
  count as HTTPS, and only for requests that came through that proxy.
- **App pages and API answers** (everything but `/f` and `/preview`): `X-Frame-Options: SAMEORIGIN`,
  `X-Content-Type-Options: nosniff`, `Permissions-Policy` (camera, microphone, geolocation, payment, usb
  off), `Cross-Origin-Opener-Policy: same-origin`, and `Content-Security-Policy` = `APP_CSP`:
  `default-src 'self'`; script **`'self' 'nonce-<n>'`** (#660, no `'unsafe-inline'`) with
  **`script-src-attr 'none'`** (no inline event handlers); style `'self' 'unsafe-inline'
  https://fonts.googleapis.com` (on purpose, see below); font `'self'
  https://fonts.gstatic.com`; img, media, connect, worker `'self'`; frame `'self'
  https://www.youtube.com`; `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`,
  `frame-ancestors 'self'`. Same-origin framing stays allowed: the admin drawer iframes
  `/admin?embed=1`, the HTML Rendered view iframes `/api/image/<slug>/rendered`.
- **`/f/...`** keeps #610's headers as they are (sandbox CSP on active content, nosniff); no app CSP and
  no X-Frame-Options, so a hotlinked file can still be embedded elsewhere. **`/preview`** (the export
  preview) gets only `PREVIEW_CSP` (`object-src 'none'; base-uri 'self'; form-action 'self';
  frame-ancestors 'self'`), which blocks nothing the exported site uses; its `media/` keep the sandbox.
- **Rules this puts on new UI code:** no inline event handlers (`onclick=`, `onchange=`, also not inside
  an innerHTML string: use `addEventListener` or a delegated listener), no `javascript:` links, no
  `eval` / `new Function` / string `setTimeout`, no new external origin (a CDN script, a remote image, an
  embed other than YouTube) without adding it to `APP_CSP` with a reason. Remote images are blocked
  (`img-src 'self'`): thumbnails are fetched and stored server-side.
- **Script nonces (#660).** The middleware makes one nonce per request (`secrets.token_urlsafe(16)`),
  fills `APP_CSP`'s `{nonce}` with it, and exposes it as `request.state.csp_nonce` and the Jinja global
  `csp_nonce()` (a ContextVar, `security_headers.current_nonce()`, so an imported macro sees it too).
  **Every inline `<script>` in `web/templates/` must be `<script nonce="{{ csp_nonce() }}">`**, or the
  browser refuses to run it; a `<script src="/static/...">` needs nothing (`'self'`). JS must not build
  an inline script (`createElement('script')` with text, or a `<script>` in an innerHTML string): load a
  `/static` file instead. A page whose HTML is cached and replayed would carry a stale nonce: page HTML
  is rendered per request, keep it that way. The static export (`web/export_templates/`, its own Jinja
  env) has no scripts and no `csp_nonce`; `/preview` keeps `PREVIEW_CSP`, which doesn't restrict script.
- **Styles keep `'unsafe-inline'`, deliberately (#660 decision).** There are 150+ `style="..."`
  attributes in the templates and more built inside JS strings (`cards.js`, `ingest.js`, the templates'
  innerHTML), and attributes can't take a nonce, so dropping it means moving every one into a class for
  little gain: injected CSS can restyle or hide parts of a page, and at worst leak attribute values
  through selector-plus-`url()` tricks, which `img-src 'self'` / `font-src` already starve of an
  outside destination. It can't run code. Script injection is the one that matters, and the nonce closes
  it. Revisit only if the inline styles get moved into classes for their own sake.
- **Opt-out:** a route that needs a default left off calls `security_headers.opt_out(response,
  "x-frame-options", ...)`; a route that needs a different value just sets the header itself.
- **Env:** `CONSTRUCTICON_HSTS_SECONDS` (above); `CONSTRUCTICON_CSP` replaces `APP_CSP` for an install
  that needs another source (`off` = no CSP on app pages; X-Frame-Options still stops framing; a literal
  `{nonce}` in it is filled per request, so keep `'nonce-{nonce}'` in its script-src);
  `CONSTRUCTICON_CSP_REPORT_ONLY=1` sends it as `Content-Security-Policy-Report-Only` to diagnose a page
  it breaks (frame-ancestors is ignored in that mode; X-Frame-Options still holds). A bad value falls
  back to the default. Read per request (`docker-compose.yml.example` shows them commented out).
- Not covered: a 500 from an unhandled exception (Starlette's outermost error middleware answers it), the
  MCP server (port 8100, bearer-only, no browser pages), the static export as published (GitHub Pages
  sets its own headers). Check with `scripts/test_security_headers_645.py` (throwaway DB), which also
  fails if an inline handler, `setAttribute('on...')` or `javascript:` link appears in the templates/JS,
  if a template's inline `<script>` lacks the nonce, if `script-src` regains `'unsafe-inline'`, or if a
  rendered page's inline scripts don't carry the header's nonce (#660).

**Behind a reverse proxy (#646, `web/proxy_trust.py`).** For an internet-facing install with HTTPS
terminated by Caddy or nginx. **The home install does not use this**: it stays plain HTTP with no
proxy and no setting, which is exactly the behaviour from before #646.
- **The setting:** `CONSTRUCTICON_TRUSTED_PROXIES`, comma-separated IPs and/or CIDRs, the proxy's
  address **as the app container sees it**. Unset or empty = forwarded headers are ignored everywhere.
  A bad entry is logged and skipped; there is no wildcard. Read per request (tests set it).
- **The one place:** `ProxyHeadersMiddleware`, the outermost layer. Only when the TCP peer is in the
  list: `X-Forwarded-Proto` (right-most value, `http`/`https` only) becomes `scope["scheme"]`, and
  `X-Forwarded-For` is walked from the right, skipping trusted proxies; the first other address is the
  client (`scope["client"]`). The left-most entries are what the client typed and are never believed; a
  non-address entry stops the walk (the proxy stays the client). Not read: `X-Forwarded-Host` (the proxy
  must pass the original `Host`), `X-Forwarded-Port`, the RFC 7239 `Forwarded` header.
- **Who uses it** (always through `proxy_trust.client_ip()` / `is_https()`, never the scope or a header
  directly; `scripts/test_proxy_646.py` fails otherwise): the session cookie's `Secure` flag
  (`web/auth.py`), HSTS (`web/security_headers.py`), the origin guard's scheme (`web/request_guard.py`:
  same-origin is now scheme + host, so behind an HTTPS proxy WITHOUT the setting every POST is a 403
  `cross_origin` whose message names the setting), the sign-in limiter's per-IP key
  (`web/routes/auth.py`), the request log's `client_ip` column (`web/middleware.py`, new in #646) and
  the "install token refused" log line. Starlette's own URL building (`request.base_url`, the
  trailing-slash 307) follows the rewritten scheme; `/login?next=` redirects are relative.
- **Trust the proxy's exact address, and give the app no other way in.** Remove the web service's
  published `ports:` so nothing reaches it except through the proxy. Never trust a whole Docker bridge
  subnet: a client hitting a published port shows up as that bridge's gateway address, which would
  then be able to forge the headers. A fixed proxy IP on its own network (`ipv4_address`) is the
  simple way (`docker-compose.yml.example`, the commented `proxy` service and `edge` network).
- **uvicorn's own `--proxy-headers`** is on by default but trusts only 127.0.0.1 (in-container
  loopback, so nothing outside the container). The compose example adds `--no-proxy-headers` so this
  module is the only reader; the owner's existing compose files don't have it and don't need it.
- **Caddy** (`Caddyfile`; Caddy keeps `Host`, redirects HTTP to HTTPS, and REPLACES any client-sent
  `X-Forwarded-For` / `X-Forwarded-Proto` with what it saw, so the app gets a one-entry chain):
  ```
  constructicon.example.com {
      request_body {
          max_size 30MB            # a second body cap in front of #649's
      }
      reverse_proxy constructicon-web:8000
  }
  ```
  plus `CONSTRUCTICON_TRUSTED_PROXIES=<caddy's address>` and `CONSTRUCTICON_HSTS_SECONDS=31536000` on
  the web service. `tls internal` instead of a public hostname gives a self-signed test certificate.
- **nginx** (it passes nothing by default; set all four, and `X-Forwarded-For` to `$remote_addr` so a
  client-sent chain is replaced, not appended to):
  ```
  server {
      listen 443 ssl;
      server_name constructicon.example.com;
      ssl_certificate     /etc/nginx/certs/fullchain.pem;
      ssl_certificate_key /etc/nginx/certs/privkey.pem;
      client_max_body_size 30m;
      location / {
          proxy_pass http://constructicon-web:8000;
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-For $remote_addr;
          proxy_set_header X-Forwarded-Proto $scheme;
      }
  }
  server { listen 80; server_name constructicon.example.com; return 301 https://$host$request_uri; }
  ```
- **The MCP server** (port 8100) reads no client address and no scheme (bearer token only; only
  uvicorn's access log shows the peer), so it has no proxy handling. Put it behind its own route only if
  it must be reachable from outside, which a customer install doesn't need.
- Not done here (#646's issue text suggests them; owner call): `CONSTRUCTICON_PUBLIC_URL` (a fixed
  public origin, for passkeys' relying-party ID and to force Secure without inferring), the `__Host-`
  cookie prefix on HTTPS (renaming the cookie signs everyone out once). Check with
  `scripts/test_proxy_646.py` (throwaway DB, the whole trust matrix).

**The install token (`core/install_token.py`).** One secret, two containers:
- **MCP** (`mcp_server/auth.py`): every request needs `Authorization: Bearer <token>` (401 otherwise;
  only `GET /healthz` is exempt). Role **admin**, actor `mcp`. **With roles enforced and no token
  configured the MCP refuses to start** (exit with "refusing to start: no install token"): the safer
  choice, a crash-looping container is loud, a 401-everything server looks healthy.
- **Web**: `Authorization: Bearer <token>` = role **admin**, actor `token`, no session, **no CSRF**.
  A Bearer header with a WRONG token is 401 `invalid_token` on every path (even `/f`), so a
  misconfigured script fails loudly instead of browsing as anonymous. Other schemes (Basic) are
  ignored. No token configured = every Bearer refused; sessions still work. A broken token config
  (unreadable or empty file, under 32 chars) stops the web app at startup, like the MCP.
- **Where it's read from** (first hit wins): `CONSTRUCTICON_INSTALL_TOKEN`,
  `CONSTRUCTICON_INSTALL_TOKEN_FILE` (preferred), then the older `CONSTRUCTICON_MCP_TOKEN(_FILE)`.
  Use ONE file, mounted read-only into BOTH services:
  `./secrets/constructicon_token:/run/secrets/constructicon_token:ro` +
  `CONSTRUCTICON_INSTALL_TOKEN_FILE=/run/secrets/constructicon_token` (`docker-compose.yml.example`).
  `secrets/` is gitignored. Never print it, never put it in git, chat, a report or the audit log
  (the audit log stores no headers).

**How each client authenticates.**
- **Browsers:** sign in (session cookie + CSRF header, step 1).
- **Claude Code / any MCP client:** `{"type":"http","url":"http://<host>:8100/mcp","headers":
  {"Authorization":"Bearer <token>"}}` in `.mcp.json` / `~/.claude.json` (test box MCP:
  `http://10.0.1.242:8100/mcp`).
- **Repo scripts that talk HTTP** (`golden_master.py`, `test_office.py`, `seed_test_from_production.py`,
  `check_curator_queue.py`, the backfill / sync / import scripts): one line,
  `_http.install(args.base_url)` (`scripts/_http.py`), adds the header to requests for that origin
  only (never to YouTube/GitHub, never across a redirect). Token from `CONSTRUCTICON_TOKEN`,
  `CONSTRUCTICON_TOKEN_FILE`, else the install's own variables, so `./scripts/run_tool.sh <web
  container> scripts/x.py` (from the host, see "Scripts: what runs where") just works. `golden_master.py --anonymous` snapshots what a stranger sees.
- **In-process tests** (`scripts/test_*.py`): `_testenv.isolate()` configures a fresh random token
  per run (dropping the container's token variables); `_testenv.client(app)` is a TestClient that
  sends it (admin, actor `token`); a bare `TestClient` is anonymous. `_testenv.use_token()` for a
  test that builds its own temp DB.

**Rollout for an install (order matters; prod is the main session's job).**
1. Deploy the code (`./scripts/deploy.sh`). Until step 3, the MCP container keeps running its old
   process; a restart without a token makes it refuse to start.
2. On the box, in the checkout: `mkdir -p secrets && chmod 700 secrets && python3
   -m core.manage.mcp_token generate secrets/constructicon_token` (prints only the path, mode 600;
   stdlib only, so it runs on the host before any container or dependency exists).
3. Compose, BOTH services: add the volume `./secrets/constructicon_token:/run/secrets/constructicon_token:ro`
   and the env `CONSTRUCTICON_INSTALL_TOKEN_FILE=/run/secrets/constructicon_token`; `sudo docker
   compose up -d` (recreates both; a plain restart doesn't pick up compose changes).
4. Check: `sudo docker exec <web> python3 -m core.manage.mcp_token check --web-url http://localhost:<port>`
   (expects 401 without, 200 with the token) and the MCP log line "bearer token auth ENABLED".
5. First admin: open the site, it redirects to `/setup`, which asks for the install token (#648):
   paste the contents of `secrets/constructicon_token` (read it on the box, e.g. `cat
   secrets/constructicon_token`; don't send it through chat or email), then your name, username and
   password. Or skip the browser: `docker exec <web> python3 -m core.manage.reset_password --create-admin
   <name> --generate`. Until step 3 the web app has no token, so `/setup` refuses everyone (it shows
   that command instead): a fresh install can't be claimed by whoever finds it first.
6. Clients: every MCP client's config gets the `headers` block; scripts
   run elsewhere get `CONSTRUCTICON_TOKEN_FILE`.
**What breaks if a client isn't updated:** an MCP client without the header gets 401 on every call
(Claude Code shows the server as failed); an HTTP script gets 401 (or a 302 to `/login` on a page); a hotlink to a restricted or
redacted item 404s; an open browser tab is sent to `/login`.

**Rotate:** `python3 -m core.manage.mcp_token generate secrets/constructicon_token --force`, restart BOTH
containers (`sudo docker restart <web> <mcp>`), update every client. **Lost admin password:**
`sudo docker exec -it <web> python3 -m core.manage.reset_password <user>` (or `--create-admin`), see above.

Check with `scripts/test_auth_step2.py` (throwaway DB: the whole matrix per role and for the token,
mounts, `next`, the decision queue, the MCP refusing to start, no secrets
in the audit log). Later (not built): HTTPS itself (the app is ready for a TLS proxy since #646, see
"Behind a reverse proxy"). Per-user MCP tokens are built (#604 step 5): see "Per-user MCP tokens" below.

## Ownership and sensitive items (#604 build steps 1-2, #603)

Owner decisions 2026-10-07 (#604 "Decided" table + settled follow-ups). Step 1 records who made what;
step 2 is the per-item "This is sensitive" switch, its first consumer. Private cards and card members
(step 3) and per-user MCP tokens (step 5) are the next two sections. Step 4 (the edit rules) is built only
inside private cards (a viewer member only looks, see "Private cards and hobbies"); the owner-only rule
for OPEN cards is not built (owner questions on PR for #604 steps 3-5).

**Ownership (step 1).** A users.id, set at WRITE time from the actor context
(`users.user_id_for_actor`): a signed-in user's id; NULL for the install token, the MCP, scripts, the
system and anonymous, which means **admin-owned**. Read it back with `users.owner_info(id)` ({id,
username, name}, or None).
- `capture_events.uploaded_by_user_id`: set in `db.insert_upload`, the ONE place an item row is made
  (web upload and `/api/content`, MCP upload / import / add_content, a card's write-up). Not the
  `tech` Source label, which is unchanged (and is what the JSON key `uploaded_by` still means).
- `projects.created_by_user_id`: `db._create_project` (every card create, split, conversion).
- `hobby_settings.created_by_user_id`: `hobbies.create`, only when a signed-in user makes the hobby
  (an imaged `hobby_settings` insert; re-marking an existing hobby never changes it).
- `blog_entries.created_by_user_id`: `blog.create` (in the imaged insert).
- **Backfill** (`db.MIGRATIONS` `ownership_backfill_604`, once, `db._ownership_backfill`): only
  `actor = user:<name>` rows of a user who still exists. Items from the request-log row of a
  successful `POST /api/upload` (its logged `file` = the filename) or `/api/content` (its
  `external_url`), matched to the nearest unowned item with that name / URL created up to 10 minutes
  before; cards from `create_card` change-log rows (+ the write-up made with them); hobbies from
  `hobby_create` rows that turned a tag into a hobby; blog entries from `blog_entry_create`. Never
  overwrites. One change-log row `migration_ownership_backfill_604` carries the counts.
- **Shown:** item JSON and MCP item reads carry `uploaded_by_user` (null = admin-owned); the item page's
  ORIGIN group says "Uploaded by <name>" / "Admin (no user recorded)"; the card page's ORIGIN says
  "Created by"; MCP `get_project` carries `created_by_user`.

**The sensitive flag (step 2, #603).** `capture_events.sensitive` (0/1) + `sensitive_by` (the actor
string) + `sensitive_at`. Written only by `items.set_sensitive(slugs, on)`: ONE imaged change-log row
(op `item_sensitive`), undoable with the generic undo.
- **Mark: editor or above. Clear: the item's uploader or an admin** (owner decision on #603,
  2026-10-07; `policy.can_unmark_sensitive(actor, item)`, checked per item, else 403 `forbidden`).
  An undo that would clear a flag is an unmark, so the same rule applies
  (`items.check_undo_allowed`, called by `cards.undo`); undoing an unmark (re-locking) is open.
- **Where:** the item page's ORIGIN group ("This is sensitive" / "Clear sensitive", `POST
  /api/image/{slug}/sensitive`, `sensitive=true|false`); every file grid's bulk bar
  (#553: home, Unfiled, the uploader gallery, a hobby's loose objects; "Mark sensitive", `POST /api/bulk/sensitive`, one batch); the upload drawer's checkbox (`sensitive`
  form field on `/api/upload` and `/api/content`, set IN the INSERT so the item is never visible
  unlocked; a duplicate upload marks the existing item instead); MCP `constructicon_update(slug,
  fields={"sensitive": bool}, dry_run)`, and `sensitive` on `constructicon_upload` / `import` / `add_content`.
- **Policy:** `policy.is_restricted(item)` = a restricted TYPE (`is_type_restricted`) OR flagged
  (`is_flagged`); "sensitive" means either. `can_view`: an ordinary item -> yes; a sensitive one -> an
  admin, or **its uploader** (a signed-in, enabled user whose id = `uploaded_by_user_id`). The uploader
  rule applies to BOTH kinds when an uploader is known; NULL = admin-only (so every key and certificate
  uploaded before step 1 is exactly as locked as before). A REDACTION still beats it: the file doors
  refuse a redacted item to anyone but an admin, the uploader included.
- **Browsing** (`sql_browse_clause`, so search, OCR text and embeddings never surface it): a restricted
  type stays out of browsing for everyone (unchanged); a flagged item is in browsing for admins and its
  uploader only (`AND (COALESCE(sensitive,0) = 0 OR uploaded_by_user_id = <id>)`, nothing for an admin).
  `list_uploaders`' totals use the same clause, so a `?query=` can't probe a hidden description.
- **Every door:** item page, item API (and its revisions / similar / every write route through
  `viewable_item`), `/f/<slug>` and thumbnail (404 for anyone else, signed in or not), browse, search,
  the gallery, Unfiled, card / hobby / blog lists, exports (`filter_exportable`: never, for anyone), MCP
  get / search / download / view / get_project / get_related, `list_needs_caption` (never offers a
  sensitive item even to the admin MCP: `filter_exportable`, the picture would leave the install), the
  processing drawer (`/api/processing`), the caption-review list, the decision queue (a supersedes
  candidate the actor can't see isn't offered), revision chains (a hidden revision shows as "(an item
  you can't see)"), and the **Curator queue**: its shared cache is now built as `system` (the whole
  queue) and filtered per actor on read (`curation_queue.for_actor`; file questions carry `item_slug`).
  The MCP acts as admin with the install token, and as the user with a per-user token (#604 step 5).
- **Admin's list** (Admin > "Sensitive items", `GET /api/restricted`, MCP `list_restricted`): restricted
  types AND flagged items, each with `reason` ("type: Certificate" / "marked by Jason K. on ...") and
  how often it was opened.

**View logging (#604 follow-up 7): sensitive items only.** `core/access_log.py`, table
`item_access_log(slug, actor, how, at)`. A door that OPENS one item calls `policy.note_access(row, how)`
after its policy check passed (a no-op for an ordinary item): `page` (`/object`), `api` (`GET
/api/image/{slug}`), `file` (`/f`), `thumb`, `mcp_get`, `mcp_download`, `mcp_view`. The same actor
through the same door within 5 minutes is folded into one row (the page polls its item API; grids
re-request thumbnails). Refused looks aren't logged. Best effort: a failed write is logged
(`besteffort.warn`) and the read still succeeds. Admins see it on the item page (ORIGIN, "Opened by"),
in Admin's sensitive list (count + last opener) and at `GET /api/image/{slug}/access-log` (admin).
`item_access_log` is kept by delete-all (`reset.KEPT_TABLES`), like the audit log.

**Checks.** `scripts/check_policy_doors.py` also requires `policy.note_access` in every single-item
door (`ACCESS_LOGGED`) and lists the new doors (processing, caption review, the queue filter, MCP view).
`scripts/test_ownership_sensitive.py` (throwaway DB, real sessions for an admin, two editors and a
viewer, anonymous and the token): ownership on every create path, the backfill and its idempotence,
the full matrix per door, who may flip it, undo, bulk, the upload-time flag, a restricted type with an
uploader, the captioning agent, exports and the view log. `test_role_policy.py` covers a flagged
fixture next to the certificate.

## Private cards and hobbies (#604 build step 3; parts of step 4)

Owner decisions: the #604 table and follow-ups (2026-10-07), and 2026-10-17 ("shared by default,
private with a check box on the card"). **Cards and hobbies are open** (every signed-in user sees them
and their items) **unless ticked Private.**
- **Who sees a private card or hobby:** admins (signed-in admins, the install token, scripts, the
  system), its **owner** (`created_by_user_id`, #604 step 1; NULL = admin-owned, so admins only) and its
  **members** (`share_members`, viewer or editor; the member still needs an install role). Everyone else
  gets 404 `not_found` on every door, exactly as if it didn't exist.
- **Inherited downward** along every grouping edge: a hobby -> its cards (`project_hobbies`), a card ->
  its nested parts (`projects.parent_id`), a family / collection -> its members (`family_members`). A card
  is visible only when the actor may see EVERY private node above it and itself (an AND): unticking a
  child can't make it more open than its parent, and a private child with its own list is closed to the
  parent's members who aren't on it. Families count as an edge on purpose (owner question on the PR).
  Since a hidden node hides everything under it, everything ABOVE a visible card is visible too.
- **Items:** hidden when ANY card holding them is hidden ("private wins", follow-up 1), or when tagged
  directly with a hidden hobby (its loose objects). The uploader is not exempt (owner question on the PR).
  Unfiled items stay visible to everyone signed in. **Hotlinks are unchanged:** `/f/<slug>` and its
  thumbnail stay public for an ordinary item even inside a private card (owner rule: the link is the
  deliberate share); `policy.require_file` applies only the sensitive rule.
- **Schema:** `projects.private`, `hobby_settings.private` (0/1, ADD COLUMN at boot, default 0: no data
  migration), `share_members(target_kind card|hobby, target_id, user_id, role viewer|editor, added_at)`
  (in `IMAGE_TABLE_KEYS`, cleared by delete-all), all in `core/db.py`.
- **Policy (`core/policy.py`):** `card_access(actor)` -> an `Access` (the hidden card ids and hobby ids;
  empty for an admin or when nothing is private, which costs one cheap query); `can_view_card`,
  `can_view_hobby`, `require_card_view`, `filter_visible_cards`, `filter_visible_hobbies`,
  `filter_tag_tree` (a hidden card's linked tag and a hidden hobby's tag leave the tag tree),
  `outsider_access()` (everything under any private node). `can_view` / `filter_visible` /
  `viewable_rows` / `sql_browse_clause` apply it to items, so search, browse, the file grids and every
  item door follow. `access_scope(actor)` works the Access out once for a block (a list filter); keep
  calls to `policy.can_view(item, actor)` positional so tests can still monkeypatch it.
- **Doors:** pages (home, card, hobby, /hobbies, the item breadcrumb), the card / hobby APIs, the Curator
  queue (`for_actor` drops hidden card / hobby groups and options naming hidden cards or files), the
  decision queue, card needs and nudges, `cards.family_fields` / `list_links` / `resolve_home` (a manual
  home naming a hidden card is passed over) / `explain_card`, blog entries' card lists, MCP
  `list_projects`, `get_project`, `list_hobbies`, `list_tags`, every card tool (through `cards.get_card` /
  `get_hobby` / `membership.get_card`, which refuse a hidden card as `not_found`).
  `scripts/check_policy_doors.py` rule 5 lists them (`CARD_DOORS`).
- **The change log:** a non-admin reads only change rows that touch nothing hidden from them
  (`sharing.visible_change_rows`, used by `cards.list_changes`: MCP `list_changes`), and can't undo one
  (`sharing.check_undo_allowed`, from `cards.undo`). Undoing a sharing change needs the owner or an admin.
- **The write guard (step 4, inside private cards only): `policy.check_write`, called by
  `db.insert_change_log` for EVERY logged write** (so every service op, the undo and any future door).
  Inside private territory only an admin, the owner or an **editor** member may change a card, its
  nesting / families / hobbies / links, or a file on it; a **viewer** member only looks (follow-up 4).
  A viewer member may still file and edit their OWN uploads there (follow-up 2: "the item's owner or an
  editor of that private card"). Refused: 403 `forbidden`, `details.reason` `private_view_only`, naming
  the card when the actor can see it; the transaction rolls back, nothing is written. **Open cards are
  unchanged** (any editor may change them, as before): the owner-only rule for open cards, and which ops
  count as "editing", are owner questions (#604 step 4).
- **Writes (`core/sharing.py`, a service module):** `state(kind, target)`, `set_private(kind, target, on)`,
  `add_member(kind, target, user, role)` (adding again changes the role), `remove_member(...)`; each one
  imaged change-log row (`share_private`, `share_member_add`, `share_member_remove`), undoable, `dry_run`.
  **Only the owner (holding the editor role) or an admin** (follow-ups 4 and 6; `policy.can_manage_sharing`),
  else 403 saying who can; a hidden target is 404. **Role ceiling** (follow-up 8): making an install
  viewer a card editor is refused (400 `role_ceiling`, says how to fix it); a member whose install role
  later drops shows `capped`. Members stay listed while Private is unticked (ticking it again restores
  them). Warnings, never silent: unticking a card that still inherits privacy names the parent.
- **Keeping things private through reshaping:** `cards.delete_card` drops the card's member rows (undo
  restores them) and warns when its files / nested cards come out from under a private card;
  `merge_cards` refuses a private card into a card that isn't (409 `privacy_conflict`); a part split out
  of a private card is private with the same members; card <-> hobby conversions carry the tick, the
  owner and the members over. The static site export publishes nothing under a private card or hobby
  (skipped with a warning; owner question on the PR whether an admin may publish one on purpose); the
  project zip follows the actor's view.
- **UI:** the card page and the hobby page get a SHARING group (`templates/_sharing.html`,
  `static/js/sharing.js`): the **Private** checkbox; ticking it reveals the member list right there (add a
  person as viewer or editor, change a role, remove); "Private · inherited from <parent>" when it comes
  from above. Read-only (with who can change it) for everyone but the owner and admins. Every change
  offers Undo; a refusal shows the server's message and puts the checkbox back.
- **Routes:** `GET /api/sharing/{card|hobby}/{id or slug}` (viewer), `POST .../private` (`private=true|false`),
  `POST .../members` (`user`, `role`), `POST .../members/remove` (`user`) (editor, then the owner rule),
  `GET /api/sharing/users` (editor: the people you can add). **MCP:** `constructicon_sharing(action=get |
  set_private | add_member | remove_member | users, card= | hobby=, private=, user=, role=, dry_run=)`;
  `get_project` / `explain_card` carry `sharing`.
- **Not done:** the heir when a user is disabled or deleted (follow-up 5; their member rows stay and
  match nobody once deleted), an admin view of every private card; counts ("N nested", a hobby's
  project count) may still include hidden cards (numbers only, never names).
- Check with `scripts/test_private_cards_604.py` (throwaway DB: the whole owner / editor member / viewer
  member / outsider / viewer / admin / token matrix per door, inheritance, the write guard, undo, the
  export) and `check_policy_doors.py` rule 5.

## Per-user MCP tokens (#604 build step 5)

Owner decision 2026-10-17: **several named tokens per user** ("windows claude", "mac claude"), each
revocable on its own in Account, stored hashed like sessions; the MCP acts as that user. The install
token stays the admin key for scripts and ops.
- **Model (`core/mcp_tokens.py`, a service module; table `mcp_tokens(id, user_id, name, token_hash UNIQUE,
  created_at, rotated_at, last_used_at)`):** a token is `ctu_` + 32 random bytes (urlsafe); only its
  sha256 is stored. The secret is in the ANSWER to create / rotate, once, and is never logged, imaged or
  stored (request bodies carry only the name). Up to 20 per user (409 `too_many_tokens`), names unique per
  user in any case (409 `token_name_taken`, 400 `bad_token_name`). Each op is one change-log row with a
  `details` summary (`mcp_token_create` / `_rotate` / `_revoke`; never the secret), not imaged, so undo
  refuses it. `last_used_at` is written at most every 10 minutes. Kept by delete-all (`reset.KEPT_TABLES`);
  a deleted user's tokens go with them (`db._delete_user`); a disabled user's are refused until re-enabled.
- **Who:** every signed-in user (viewer and up) manages ONLY their own tokens; the install token / MCP
  (no user) get 403 `not_signed_in`. An admin can't see or revoke someone else's (owner question).
- **Web:** Account page (`/account/password`, the header's name link), section "My MCP tokens"
  (`static/js/account-tokens.js`): make (shows the secret once plus the client config line), rotate
  (confirm), revoke (confirm). API (viewer, JSON bodies): `GET /api/account/mcp-tokens`, `POST
  /api/account/mcp-tokens` `{name}`, `POST /api/account/mcp-tokens/{id}/rotate`, `.../revoke`.
- **MCP (`mcp_server/auth.py`):** the install token still runs as `mcp` (admin). A Bearer value starting
  `ctu_` is looked up (`mcp_tokens.authenticate`, off the event loop); valid -> the request's identity
  (`auth.IDENTITY`, a ContextVar) and the tool wrapper (`server._call_as_mcp`) runs the tool as
  `user:<name>`: ACTION_ROLES applies their role (a viewer's token can't write), the item and card policy
  apply (they see what they see), and the change log names them. Refused (revoked, rotated, unknown, a
  disabled or deleted user, no role) = 401 with a message saying what to do; a lookup that fails (DB) =
  503 saying the token couldn't be checked. Client config: the same `headers` block with the user's token.
- **The web API with a user token:** not accepted (the web's `Authorization: Bearer` is the install
  token only; a `ctu_` value there is 401 `invalid_token`). Owner question.
- Check with `scripts/test_mcp_user_tokens_604.py` (throwaway DB: the account API, the secret never at
  rest, authenticate's refusals, the middleware, and real tool calls through the streamable-HTTP app as a
  user: a card they create is theirs, a viewer can't write, a private card is not_found).

## Service layer (#541): one core module per domain

**The rule: all writes go through `core/{items,membership,tags,cards,hobbies,blog,decisions,reset}.py`
(plus `core/revisions.py` for revision chains, `core/duplicates.py` for duplicate folding (#615),
`core/install_config.py`, `core/users.py`, and since #604
`core/sharing.py` (private cards / members) and `core/mcp_tokens.py` (per-user MCP tokens)); the raw writers in `core/db.py` are private
(`_`-prefixed); `scripts/check_layering.py` enforces it.** Run `python scripts/check_layering.py`
(no server, no DB; `--list` prints every db.py writer and its class) before every PR that touches
`core/`, `web/` or `mcp_server/`. It fails when:
- a module outside the service modules calls (or imports) a private `db._*` function. The raw-write
  allowlist, each with its reason in the script: `core/captions.py` (caption status/results),
  `core/embedded_metadata.py` and `core/object_types/youtube.py` (upload-time file metadata, a
  YouTube publish date), `core/automatch.py` (upload-time automatch tagging), `core/card_migration.py` (migrations), the one-off scripts
  `scripts/archive/{apply_project_groupings,seed_example_projects,backfill_from_hooptiej_site,link_lil_dragon_brand}.py`
  (retired, kept readable),
  and `scripts/test_*.py` (throwaway-DB fixtures);
- anything uses an old public writer name (`RETIRED_PUBLIC`: `db.create_project`, `update_project`,
  `get_or_create_tag`, `attach_tags`, `add_item_to_project`, `mark_tag_as_hobby`,
  `add_project_to_hobby`, `create_blog_entry`, `set_entry_items`, `resolve_pending_decision`, ...);
- `core/db.py` gains a PUBLIC function that writes and isn't classified in `PUBLIC_WRITERS`. The
  public writers left are pipeline/infra, not curation: item creation (`insert_upload`,
  `insert_content`), OCR/similarity state, the caption queue, settings, the request log, the change
  log + undo plumbing (`insert_change_log`, `invert_image`, `mark_change_rows_undone`,
  `write_images`), asking a question (`add_pending_decision`, `queue_decision_once`; answering is
  `core/decisions.py`), Curator snooze state (`core/curation_queue.py`), boot/migrations.
A new write = a service op (or a private db writer called only from its service module). Read
functions stay public.

Every write follows `core/cards.py`'s contract: validate everything first, one
`db.transaction()`, rows written through `db.ImageLog` (row images in `audit_log`), a
`Result`, `dry_run` where it makes sense. Undo is the generic `cards.undo` (POST
`/api/changes/{id or batch}/undo`, MCP `constructicon_undo`). Web routes and MCP tools are thin
adapters; a route that composes two ops passes one `batch_id` inside one `db.transaction()` so
one undo reverses the request. Every table an op images must be in `db.IMAGE_TABLE_KEYS`.
- **Cards** (`core/cards.py`, V2 pieces 1-7), **revisions** (`core/revisions.py`),
  **provenance options** (`core/provenance_options.py`). Phase D added `cards.create(title, ...)`:
  the linked root tag (reused if one exists, else created and imaged), the card row and its blank
  write-up (added, tagged, set) as ONE batch, so undo leaves nothing behind (web create,
  from-selection / from-related with their files in the same batch, MCP create_project); and
  `cards.update(card, title, description, cover_slug, cover_project_id, writeup_slug, start, end)`:
  the card's own fields as one imaged `update_card` row (was `db.update_project` +
  `set_project_date_overrides`, unlogged). The web card Save runs update + nest + kind + status in
  one batch (the answer carries `batch_id`).
- **Hobbies (phase D): `core/hobbies.py`.** `create(name, status)` (tag reused or created, marked,
  group code derived), `unmark(hobby)` (its card memberships and home overrides go; the tag stays),
  `add_card` / `remove_card` (= `cards.add_to_hobby` / `remove_from_hobby`; the web add/remove
  routes used to write unlogged), `set_activity` / `set_group_code` (= the cards ops),
  `convert_from_card(card)` (project -> hobby, now fully imaged and undoable; dependents cleared
  as delete_card clears them) and `convert_to_card(hobby, kind, title=None, into_hobby=None)`
  (hobby -> card, the reverse; owner ask 2026-10-03, "GI Joe" -> a family under Collecting). The
  hobby -> card rules: the new card (family | collection | project) reuses the hobby's tag, gets a
  write-up, and its stage follows the hobby (active -> in_progress, inactive -> paused); top-level
  member cards become family members (family/collection) or nested parts (project), a member nested
  under another member stays under it; a group-kind member (bad_membership / nest_group_kind) or,
  for project, a member already part of an outside card (nest_second_parent) refuses the whole
  thing; loose objects go onto the card; home overrides naming the hobby now name the card; the
  hobby is unmarked. One batch, `dry_run` previews. Web: the hobby page's More > "Convert to card"
  (`POST /api/hobby/{id}/convert-to-card`), `POST /api/hobby/{id}/unmark`; MCP
  `constructicon_convert(direction="hobby_to_card")`, `constructicon_unmark_hobby`.
- **Blog (phase D): `core/blog.py`.** `create`, `update` (None = leave; `...` = leave for
  cover_slug / content_date, None clears), `delete`, `set_projects(entry, [(card, note)])`,
  `set_items(entry, [(slug, note)])`, all imaged (`blog_entries` is in `IMAGE_TABLE_KEYS`). Unknown
  cards / files are refused (404 not_found) instead of being stored as rows no page can show; a card
  can be given by id or slug.
- **Decisions (phase D, #551 item 3): `core/decisions.py`. Reads never write.** `list_open()` (GET
  `/api/pending-decisions`, the Curator queue, MCP list) leaves out a question `stale_reason()` calls
  stale and resolves nothing; `count_open()` = what it shows. The rules are exactly the old
  resolve-on-read ones, unchanged: a card question (`card:<slug>`) whose card is gone; a file
  question whose object is gone; project_match with < 2 candidate cards left; item_supersedes with
  no live candidate. `sweep_stale()` resolves those (`{"stale": reason}`) as ONE imaged change-log
  row (op `sweep_stale_decisions`), undoable; nothing stale = nothing written. The web worker runs
  it once at startup after the migrations and every `decisions.SWEEP_INTERVAL_SECONDS` (hourly) as
  `system`; MCP `constructicon_sweep_stale_decisions(dry_run)`. Answering (`resolve`) images the
  resolution in the same batch as what the answer applied (membership / retype), so one undo
  re-opens the question.
- **Items (phase B): item writes go through `core/items.py`; deletes are held 7 days, then
  purged; redacts are held until the owner clicks.** `items.update(slug, **fields)` covers display name, description,
  content_description, type_metadata (merged, physical-piece keys cleaned), file provenance,
  highlight, brand asset/role, display-date override and content_date as ONE batch per call (one
  Save = one undo). `redact` / `recover_redacted` / `delete_redacted_file` / `unredact`, `retype` (the media_type change is imaged; the caller's
  runner re-runs OCR/thumbnail/caption; undoing a retype re-runs them for the old type) and
  `delete`. Their raw writers (`db._rename_object`, `_update_content_metadata`, `_set_type_metadata`,
  `_set_provenance`, `_set_highlight`, `_set_brand_asset`, `_set_display_date_override`,
  `_set_content_date`, `_set_media_type`, `_mark_redacted`, `_unmark_redacted`, `_delete_upload`) are
  private since phase D. The MCP's agent notes (#206) go through `items.update(agent_notes=...)`; a card's (#745) through `cards.set_agent_notes`. Deliberate raw exceptions: pipeline bookkeeping (caption status/results in
  `core/captions.py`, upload-time `core/embedded_metadata.py`, a YouTube row's fetched publish
  date, OCR state). `items.relate` / `items.unrelate` (phase C) are the item <-> item "related"
  link: relate also shares tags and card memberships both ways (#16), all imaged, so one undo
  removes the link AND what it shared; unrelate leaves the shared tags/cards (as before).
- **Membership (phase C): `core/membership.py` is the one way files go on and off a card.**
  `add_files(card, slugs, *, link_tag, merge_free_tags, auto_cover)` (flags keyword-only, no
  defaults) and `remove_files(card, slugs)`. `link_tag` = the card's linked tag into `post_tags`;
  `merge_free_tags` = that tag's name into the free-text `tags` column (#274); `auto_cover` = a card
  with no `cover_slug` takes the first file (also clears `cover_project_id`, bumps `updated_at`).
  `membership.UI_EFFECTS` (all three) is what upload / `ingest.attach_to_project`, the item page,
  bulk add-to-project, from-selection / from-related, a resolved project-match question, AND (since
  phase C) MCP `constructicon_card_files(action="add")` passes. `NO_EFFECTS`: MCP
  `constructicon_update_project(fields={"writeup_slug": ...})`. Removing never untags and never touches the cover, on any path. The card
  reshaping ops (copy / move / split / merge / delete) call `membership.write(...)` / `write_items(log,
  ...)`: rows only, logged under their own op (`db.write_card_items` is gone). Don't call
  `db.add_item_to_project` / `remove_item_from_project` from a route or tool.
  **Tags follow a transfer (#590).** `membership.plan_transfer_tags` / `apply_transfer_tags` (read-only
  plan, then one imaged row under the op's own batch): `cards.move_files` and `cards.copy_files` give
  the files the destination's linked tag in BOTH stores (`post_tags` + the free-text chip); a move
  also drops the source's own linked tag (`projects.tag_id` exactly; hand-added tags stay), unless the
  file is still on another card whose linked tag is the same tag (chip: same name). `merge_cards` is
  a move into the kept card (the absorbed card is ignored in the "still elsewhere" check). Split is
  left alone (the new card has no linked tag). No cover change. Check: `scripts/test_move_tags_590.py`.
- **Tags (phase C): `core/tags.py`.** `create(name, parent_name)` (MCP create_tag), `attach(slug,
  names)` / `detach(slug, tag_id)` (MCP; `post_tags` only, as before), `set_item_tags` and
  `merge_item_tags(slugs, names)` (bulk attach-tags: free-text column + its `post_tags` sync, one
  batch). The item Save passes `tags=` to `items.update`, so tags + fields are still ONE change-log
  row. Creating a tag is imaged (`blog_tags` insert via `ImageLog.insert_auto`), so undo removes a
  tag the op created; `db.invert_image` refuses that (undo_conflict) while anything made later uses
  the tag. Lookups (`tags.find`, `find_root`, `find_any`) never create. The two tag stores (free-text column vs
  `post_tags`) stay as they were; unifying them is #555. Card creation and hobby creation mint their
  tags through `tags.ensure` too (phase D); the only raw tag writer left is the upload-time
  automatch tagging (pipeline, allowlisted; the OCR client tagging went with #704).
- **Delete-all (phase C): `core/reset.py` `delete_everything(confirm)`**, called by `POST
  /api/delete-all` and MCP `constructicon_delete(everything=true, confirm=...)` (#746: BOTH keys, either alone
  is refused); both need the phrase `DELETE EVERYTHING`. Permanent by design (no trash, no undo); one change-log row (op `delete_all`, actor
  from context) with per-table counts in `form_body`. Clears `reset.CLEARED_TABLES` (items and
  everything pointing at them, cards, tags and hobbies, blog entries, questions and their snoozes,
  caption queue, trash) plus every item file and thumbnail and `<storage>/.trash`; keeps
  `reset.KEPT_TABLES` (settings, users, provenance options, migrations, the audit/change log). A
  new table must be added to one of the two lists (`scripts/test_membership_tags.py` fails otherwise).
  Check phase C with `scripts/test_membership_tags.py` (throwaway DB).
- **Trash.** `items.delete` / `items.redact` move the file and its thumbnail to
  `<storage>/.trash/<batch_id>/` (same dataset, so ZFS snapshots cover it) and record a `trash`
  row. **Deletes are held 7 days, then purged** (`expires_at` = now + 7 days). **Redacts are held
  until the owner clicks** (owner decision 2026-10-04): the row is `reason='redact'`, `expires_at`
  NULL, and neither the hourly purge nor "Empty trash now" (`POST /api/trash/empty`,
  `constructicon_trash(action="empty")`) ever touches it. A redacted item's `/object/<slug>` page and the
  /admin "Redacted items" list show "The redacted file is still stored on the NAS. Recover it, or
  delete it permanently." with two actions: **Recover** (`items.recover_redacted`,
  `POST /api/image/{slug}/recover-redacted`, MCP `constructicon_redact(action="recover")`; file,
  `stored_filename` and visibility exactly as before the redact) and **Delete file permanently**
  (`items.delete_redacted_file(slug, confirm)`, `POST /api/image/{slug}/delete-redacted-file`, MCP
  `constructicon_redact(action="delete_file", confirm=true)`; confirm dialog, logged as `item_redact_erase`, not
  undoable; the item stays redacted, metadata only, and recover then refuses with
  `no_redact_hold`). `unredact` is only for file-less redactions and refuses with
  `redact_hold_exists` while a hold exists. `constructicon_trash(action="list")` lists holds separately
  under `held`. A delete images the `capture_events` row and everything
  pointing at it: `capture_event_relations` (both directions), `item_revisions` (incl. the
  A -> C re-link when B leaves a chain), `project_items`, `post_tags`, `blog_entry_items`,
  `pending_decisions` about the item and their `curator_dismissals` (`decision:<id>`). The
  embedding (a BLOB, never imaged) rides in the trash row. Undo restores rows AND files; if the
  file was purged it refuses with `trash_expired` (410) and writes nothing. The web worker
  (`web/app.py`, `_trash_purge_loop`) purges expired entries hourly (rows kept, `purged_at` set);
  `/admin` "Trash" shows count/size/oldest with a typed-phrase "Empty trash now" (`POST
  /api/trash/empty`, confirm `EMPTY TRASH`); MCP `constructicon_trash(action="list" | "empty")`. Delete and bulk delete answer with `batch_id`; the pages offer
  Undo (`web/static/js/undo-bar.js`). Check with `scripts/test_items_service.py` (throwaway DB).
- **Duplicates (#615): `core/duplicates.py`.** Owner rule (2026-10-17, on the issue): a duplicate is a
  BYTE-IDENTICAL stored file (same size + sha256), never a perceptual look-alike (PDF versions sharing
  a cover page, the .eps and .png of a logo). `plan(actor)` is read-only: it sizes every un-redacted
  stored file, hashes only same-size ones, and per group picks the survivor: copies with a real date
  (`timeline.resolve_item_date`) from BEFORE Constructicon's first upload (`MIN(timestamp)`) first,
  the earliest Mountain day wins; ties -> on more cards -> more metadata (description, caption, tags)
  -> a clean file name over a copy marker (`AlbertusMT.otf` over `AlbertusMT (1).otf`;
  `duplicates.has_copy_marker`: "(n)", "- Copy", "copy 2", "Copy of") -> oldest upload. A copy with no real date, or one on/after the first upload, wins only when no copy
  has an earlier real date. Groups with a copy the actor may not see (`policy.can_view`) are left out
  and only counted; two rows sharing ONE stored file are skipped with the reason; an unreadable file is
  listed under `unreadable`, never skipped silently; empty files are ignored. `apply(dry_run=True,
  expect_trash=None)` is admin only and DRY-RUN BY DEFAULT: it re-plans inside one transaction, and
  per group one imaged `duplicates_merge` row gives the survivor what the copies had and it lacks
  (card memberships, tags in both stores, blank description / caption / provenance / agent notes /
  brand role, the highlight / brand-asset / sensitive flags, related links, blog placements, and any
  card cover, card write-up, hobby cover or blog cover that named a copy), then ONE `items.delete` of
  every copy under the same batch (trash, 7 days). One `constructicon_undo(batch_id)` reverses it all.
  `expect_trash` = the slugs a dry run listed; a fresh plan that differs refuses (409 `plan_changed`)
  and writes nothing. No date is ever edited. Idempotent: a second run finds no groups. MCP
  `constructicon_duplicates(action="plan" | "apply", dry_run, expect_trash)` (plan editor, apply
  admin). Check: `scripts/test_duplicates_615.py` (throwaway DB: grouping, survivor rule,
  inheritance, sensitive filter, dry run, refusals, apply, idempotence, the full undo, the upload guard).
- Checks: `scripts/test_items_service.py` (phase B), `test_membership_tags.py` (phase C),
  `test_hobbies_blog.py` (phase D: hobbies, both conversions, card create/Save, blog, the sweep;
  every write undone and the whole DB compared), `check_layering.py`. All run on a throwaway DB.

## Card faces (#596)

One card component (`web/templates/_card.html`, `static/css/cards.css`; sizes full / small / mini),
fed by `core/cards.py`: `card_face(card)` (project, thing, action, family, collection, event),
`hobby_card_face(...)` and `file_face(...)` (files; `static/js/cards.js` builds the same file face
client-side for the item grids, `scripts/check_item_cards.py` compares the two). Zones, top to bottom,
matching the V2 mockup's Card template:
- **Name bar** (title + kind icon), **dates** (big) + curation pips (not on hobby / file faces),
  **art**, **type line** (`Thing - <home>`, `Hobby · Active`, the file's type) + group codes.
- **Text box:** relationship lines first (`rel_lines`): an action that is part of a card with no
  `applies_to` link of its own reads "Applies to: <parent>."; then each OUTGOING directed typed link,
  "Built for: X." / "Applies to: X." / "Used in: X." / "Inspired by: X." (2 lines, then "+N more").
  Then the card's own text (`text`), then a hobby's `notes`, then the italic `flavor` under a rule.
- **Status box** (`zone`): the stage ("In progress", "Stopped · failed"); "Inside · <parent>" for a
  nested card; "Hobby · active|inactive"; "Stacked · <card>" on a file. Activity dot + "needs your input".
- **Stat box** (`stat`): "N stacked · M nested" (files on the card, cards nested in it), "N members"
  (family / collection), "N projects" (hobby), the file extension (file).
- **Footer** (`foot`): "<CODE> · <hobby>" (the home hobby, else the first; "+N" for more), "<CODE> ·
  family card" / "collection card", "<CODE> · group card" (hobby), "<CODE> · <card>" (file); "n of N"
  on the right for an ordered family member.
- **No provenance, credit or whereabouts on any face** (project, file or hobby). They live in the
  details panel (ORIGIN / STATUS) only. Mini cards carry no text box, stat or footer at all.
- **Grey art = "I don't have it" (#770, owner decisions 2026-10-18).** `card_face` sets `gone` from
  `card_rules.whereabouts_gone(kind, whereabouts)`: any of `WHEREABOUTS_GONE` (partial, parted_out,
  sold, gifted, lost, abandoned, never_built) greys the art (`cx-gone`) at every size, with no reason
  tag (the reason stays in the details panel); a kind outside `WHEREABOUTS_KINDS` never greys. Stage
  Stopped no longer greys: it sets `stopped_tag` (`cx-stopped` + a small "Stopped" tag on the art),
  independent of grey. Hobby and file faces have neither. `cards.js` builds file faces only, so it
  has nothing to match. Whereabouts `abandoned` (#773, "Abandoned (left it behind)", the object was
  left behind) is not stop_reason `abandoned` (the project was); `WHEREABOUTS_HELP` says so in the
  picker. Check: `scripts/test_gone_grey_770_773.py`.
- Hobby face text box: its synopsis, then "Projects: A, B, C, +N more." and "Every card in this hobby
  carries its group code, CODE.".
- **Hobby covers (#686, owner decision: chosen, with an automatic fallback).** `hobby_settings.cover_slug`
  (added by `init_db`'s ALTER loop, NULL = automatic; no data migration). `cards.resolve_hobby_cover(hobby,
  projects, usable)` decides, read-only: the chosen cover; else the resolved cover of its **highlighted**
  member project (`projects.highlight`); else of its **most recent** member project that has one
  (`cards.recent_first`: latest `updated_at`, then `created_at`, then id); else none. A chosen cover whose
  item is deleted / trashed / redacted / hidden from the actor is skipped (`picked_unavailable`), never a
  broken image; the choice is kept, so undoing the delete brings it back. Web passes
  `web.shapes._hobby_cover_usable` (policy + the thumb route's rules). Write: `hobbies.set_cover(hobby,
  slug_or_None)` (op `hobby_cover`, imaged, undoable; `bad_cover` 422 for a redacted or picture-less item,
  `not_found`): web `POST /api/hobby/{id}/cover` (editor, blank = automatic; the hobby page's COVER group),
  MCP `constructicon_update_hobby(hobby, cover_slug | clear_cover=true, dry_run)`. Reads carry `cover_url`,
  `cover_slug`, `cover_picked`, `cover_source` (picked | highlighted_project | recent_project):
  `GET /api/hobbies`, `GET /api/hobby/{id}`, MCP `constructicon_list_hobbies`. The **/hobbies page and the
  Hobbies drawer** render the shared card (`_hobby_cards.html` -> `card(face, "small")`, faces from
  `web.shapes._to_hobby_face`, with the computed flags as `cx-flag` lines); the drawer fetches the same
  fragment from `GET /api/hobbies/html`; their JS is `static/js/hobbies.js`. The user-visible word is
  `card_rules.HOBBY_LABEL` (#700 will make it renameable). Check: `scripts/test_hobby_covers_686.py`.

**The text, in this order** (`cards.face_text(card)` -> `(text, source)`; never whereabouts or provenance):
1. `projects.synopsis`: a few hand-written sentences (max 600; one paragraph per line);
2. else `projects.writeup_lead`: the write-up's opening paragraph(s) as plain text, clamped to
   `cards.FACE_TEXT_MAX` (`markdown_render.lead`: headings, lists, all-italic editorial notes and
   "Reconstructed by / Written up by ..." production notes skipped; never across a heading);
3. else the description; else empty. Then `projects.flavor` (one line, max 140) in italics.
- `writeup_lead` is a **cache**, so a card list never reads write-up bodies (#528): `items.update` refreshes
  it whenever an item's `type_metadata` changes (`cards.refresh_writeup_lead(log, slug)`, same
  change-log row, so undoing the body edit restores both), `cards.update(writeup_slug=...)` recomputes
  it, `items.delete` of the write-up clears it, and the `writeup_lead_596` migration filled it once.
- **Writes:** `cards.set_text(card, synopsis=..., flavor=...)` / `hobbies.set_text(hobby, ...)` (hobby
  text lives in `hobby_settings.synopsis / flavor`): `...` leaves a field, None / "" clears it,
  `card_rules.validate_card_text` (`bad_card_text`, 422). One imaged row each (`set_card_text`,
  `hobby_text`), undoable. Web (editor): `POST /api/projects/{id}/text`, `POST /api/hobby/{id}/text`
  (omit a form field to leave it, send it blank to clear). MCP: `constructicon_update_project(card,
  fields={"synopsis", "flavor"})` and `constructicon_update_hobby(hobby, synopsis, flavor)` ("" clears). Reads: `cards.text_fields(card)` (in
  `explain_card`, MCP `constructicon_get_project`: synopsis, flavor, writeup_lead, face_text,
  face_text_source). UI: the ABOUT group of the details panel on the project and hobby pages
  (`templates/_card_text.html`).
- **Every write-up gets a synopsis** (owner, 2026-10-06). An agent that writes or rewrites a card's
  write-up (`constructicon_update_project(fields={"writeup_slug": ...})`, or an `update` of its body) also
  sets that card's synopsis (`fields={"synopsis": ...}`, in the same call): a few plain sentences saying
  what the card is.
- Item grids embed `stacked` (the title of the card a file is on, `db.first_card_titles()`, one query per
  grid) in the slim payload (`core/card_payload.py`); `provenance` left it. Check with
  `scripts/test_card_faces.py` (throwaway DB), `check_item_cards.py`, `test_home_payload.py`.
- **The home Files panel is paged (#624).** The page embeds only the first batch (120) of each type in
  the default view (All files, Date: newest), plus per-type counts and a cursor per tab (`FILES` in `home.html`);
  `GET /api/home/files` (viewer; `web/files_feed.py`) serves any other view and every later page:
  `type` (all | a media type), `filed` (all | unfiled | filed), `sort` (date_newest (default) |
  date_oldest | newest | oldest | az, the `files_feed.SORTS` registry; see "Grid sort" below), `cursor`,
  `limit` (1..500), `rev=all`.
  The answer is `{items (the card payload), unfiled_slugs (of this page), total, next_cursor}`. The
  visible list is built exactly as the page used to (browse clause, `policy.filter_visible`,
  `_split_revisions`), so restricted items never appear. A cursor is opaque (view + offset + last slug;
  it survives an upload at the top between pages) and only continues its own view. Errors are 400 in the
  shared shape with a specific code: `bad_type`, `bad_filed`, `bad_sort`, `bad_limit`, `bad_cursor`,
  `cursor_view_mismatch`. Client: `ItemCards.pager` takes `loadMore` and a total, `ItemCards.feed` keeps
  one view's items and cursor, `ItemCards.getJson` reports failures with code, message and status.
  Since #553 every file grid is paged this way (next item). Check with `scripts/test_home_paging_624.py`
  (and `--bench`) and `node scripts/test_home_paging_624.js [--compare DIR]`.
- **One file grid (#553).** Home's Files panel, /unfiled, the user gallery (`/gallery/user/<label>`) and a
  hobby's loose objects are one component: the page route calls `web/item_grid.py` `build(grid_id, source,
  show_all_revs, ...)` and passes the result as `grid`; the template includes `templates/_item_grid.html`
  (controls + a JSON config block, `<script type="application/json" id="<grid>-config" nonce=...>`, no
  inline code); `static/js/item-grid.js` mounts every such block. Every grid gets the same controls: the
  shared Sort, a type `<select>` with counts (it replaced home's type tabs), All / Unfiled / Filed (not on
  /unfiled), Select + the bulk bar (always on for /unfiled) with **add to card, turn into card, tag,
  provenance, mark sensitive, delete**, the `?rev=all` note. Bulk actions call the existing endpoints
  (`/api/bulk/add-to-project`, `/api/projects/from-selection` (now answers `batch_id`),
  `/api/bulk/attach-tags`, `/api/bulk/sensitive`, `/api/delete`) plus `POST /api/bulk/provenance`
  (`items.set_provenance`, one imaged row `item_bulk_provenance`; it was one `/api/image/{slug}` POST per
  file); each answer's `batch_id` feeds the Undo bar, then the grid re-reads its view from the server.
  **Viewers get no Select button, no bar and an empty `bulk` list** (`item_grid.can_edit()`, editor+, the
  bulk routes' own role). Every grid is server-paged: the page embeds the first batch of its default view
  plus per-type counts; `GET /api/items/grid` (viewer) = `/api/home/files` with `source` = `home` |
  `unfiled` | `user:<label>` | `hobby:<slug>` (`files_feed.parse_source`; 400 `bad_source`, unknown hobby
  404), and its answer also carries `counts` / `labels` (the type filter after a refresh). A cursor is
  bound to its source (`cursor_view_mismatch`); home's cursors are unchanged. Each source reads its rows
  as its page did, then `policy.filter_visible` + `_split_revisions` (`files_feed.load`, the door in
  `check_policy_doors.py`). User-visible grid words live in `item_grid.LABELS`. **Grid / List
  (#714), on every grid:** List is one compact row per file (`ItemCards.row` in `cards.js`: lazy thumb,
  `display_name`, type label, `card_date`, the card it's on or the unfiled lamp, the revision badge, the
  select checkbox outside the link), the grid element gets `role="list"` + `cx-item-list`; Select, the
  bar, Undo, paging, sort and filters work the same in both. Remembered per grid in localStorage
  (`constructicon.grid.<id>.layout`, try/catch, default Grid). The card face is unchanged. **The bulk
  bar wraps (#715):** groups in order (count + Clear | add to card + turn into card | tag | provenance |
  sensitive | delete, last and set apart), flex-wrap with no nowrap or fixed widths; sticky above 600 px,
  static on a phone (wrapped it is ~5 lines). Check with
  `scripts/test_item_grid_553.py` (throwaway DB: the four pages, viewers, every bulk action from a
  non-/unfiled grid with change log + undo, 1,050 gallery rows) and `node scripts/test_item_grid_553.js`.
- **Grid sort: the date on the card, or the upload time (#622, #725).** Every item grid (home Files, /unfiled,
  the user gallery, hobby loose objects) has the same Sort menu, `templates/_item_sort_options.html`:
  **Date: newest** (`date_newest`, the default) / **Date: oldest** (`date_oldest`), **Uploaded: newest** /
  **Uploaded: oldest** (`newest` / `oldest`, upload time, their pre-#622 meaning, kept for old links and
  API callers) and **A–Z** (`az`). "Date" is the date the card shows: the payload's `card_date_epoch`
  (`web/shapes.py`, from `core/item_dates.py` `item_date`, the same value `card_date` is the label of, so
  the order and the dates on the cards can't disagree). **#725: the upload time is never the card date.**
  `card_date_epoch` is the item's REAL date, else the start of the range it inherits from its card,
  else **null = undated, which sorts LAST in both directions** (by upload time among themselves).
  Ties on the card date go by upload time (same direction), then keep the list's order (stable). The
  JS treats a payload with no `card_date_epoch` key at all (cached from before #622) as its upload time. One sorter, `static/js/item-sort.js` (`ItemSort.sorted(items, name)`,
  `ItemSort.wire(select, onChange)`; loaded by `_item_grid.html` with `?v=static_version`): since #553
  every grid passes the name to `GET /api/items/grid`, where `files_feed.SORTS` uses the same keys
  (`ItemSort.sorted` has no page caller left; the tests keep the two in step). **To add a sort:** one entry in `files_feed.SORTS`, the same in
  `item-sort.js` `SORTS`, one `<option>` in the partial. Check with `scripts/test_card_date_sort_622.py`
  (throwaway DB: the field per dating rule, the four pages, the server face, the endpoint order) and
  `node scripts/test_item_sort_622.js` (the comparators, and that the three lists agree).
- Not done (no data for it): the mockup's "From: <item>, <date>." line on a file derived from another
  file (there is no derived-from relation), and image dimensions in a file's stat box (not stored).

## Project timeline: turn the dial (#593)

The strip under the breadcrumb on a project page (`static/js/project-video-timeline.js`) lets an
**editor** drag a marker to re-date its item. The date written is the item's **display-date override**
("date set by hand", `capture_events.display_date_override`, which `core/timeline.py` `resolve_item_date`
already ranks first), so it is the same field as the item page's DATES group.
- **Write path:** `POST /api/image/{slug}/date` (editor): form `display_date` (unix seconds, 1970-2100) or
  `reset=true`, optional `project=<card slug>`. It calls `items.set_display_date`, which is
  `items.update(display_date_override=...)`: ONE imaged change-log row, undoable (the answer's `batch_id` feeds
  `undo-bar.js`; null when nothing changed, e.g. a drop on the same date). Bad dates are 400 `bad_date`. The
  item policy runs first (`policy.viewable_item`), so a sensitive item is 404 for an editor who can't see it.
  With `project`, the answer also carries the card's recomputed `span` (start/end, plus `*_by_hand` for the
  card's own Timeline start/end overrides, which still win), built from `policy.filter_visible` items.
- **Read path:** the page's `timeline_items` come from `policy.filter_visible`, so a hidden item is not in the
  timeline data at all; `timeline_can_edit` (editor+) gates the JS, the route enforces it too.
- **Gestures:** drag a marker (mouse or touch, pointer events). A plain drag lands on a DAY and keeps the
  item's time of day; hold **Shift** while dragging for 15-minute steps. A focused marker: **arrows** = a day,
  **Shift+arrow** or **PageUp/PageDown** = a month, **Enter** saves, **Esc** cancels, **Delete** resets a
  hand-set date. Markers within 12 px form a **stack**: pressing one opens a chooser (thumbnail + name, "Open",
  "Reset date" for a hand-set one); pick one, then drag it. A hand-set marker has a yellow ring and its tooltip
  says "date set by hand". The drag is clamped to the axis as drawn; use the arrows (or the item's DATES group)
  to go beyond it.
- **Date maths** is a pure module, `static/js/timeline-drag-math.js` (pixel to date, day / fine snapping,
  key steps, stack grouping). Checks: `node scripts/test_timeline_drag.js` (the maths),
  `node scripts/test_timeline_drag_ui.js` (the widget against a fake DOM), `scripts/test_timeline_drag_593.py`
  (throwaway DB: write, undo, reset, roles, policy, span).
- **Ruler labels thin out when they don't fit (PR #782):** every tick keeps its mark, but
  `ProjectVideoTimeline.labelledTicks` labels only a calendar-friendly stride of them (quarters, halves, years;
  a January or round decade kept when there is one) once labels at their estimated width would touch. Before
  that, a two-year span (a label per month) or a ~60-day one (weekly) on a narrow panel ran together into one
  line of text. Check: `node scripts/test_timeline_scale_labels_782.js` (20 spans x 6 widths).
- **Undated items are off the axis (#725):** an item with no real date has no marker (its approximate date
  would only be this card's own range); the page says "N undated files not on the timeline". A reset that
  leaves an item with no real date takes its marker off and says so; the route answers `effective_date`
  null. A sub-card with no real dates draws no block, and a card with none shows "No real dates yet" for
  its start / end.
- Not built: dragging the axis end caps to set the card's Timeline start/end, dragging a whole stack.

## Home timeline: the radio dial (#775)

The home page's timeline is a horizontal dial across the top (`static/js/radio-dial.js`, maths in
`static/js/radio-dial-math.js`), styled like the card page's tuner. It replaced the vertical left rail
(`timeline-rail.js` and the `-shifted` layout offsets are gone). Data: the page's `timeline_projects`
(every card with a real start, children included), embedded as a nonce'd JSON block.
- **Behaviour (owner decisions on #775):** a fixed needle over a sliding band; 5 years visible by
  default (the owner's tuning on PR #782; #775 started at about a decade; fewer on a narrow screen, so
  every year label fits); drag (pointer events), wheel / trackpad and the
  arrow keys (Shift or PageUp/Down = a year, Home/End = the ends) slide it. One tick per card at its start.
  **Browse only:** it never scrolls or filters the cards. Starts **untuned** (needle mid-range, the readout
  shows static); once moved, the readout names the cards within a few px of the needle, with dates and
  links, capped with "+N more". **Marks never grow or zoom** (the old rail's hover-zoom was a time sink).
- **Feel values** live in one place: `DIAL_DEFAULTS` in `radio-dial.js` (span, tolerance, inertia and its
  falloff (a steady brake so a coast eases to a stop), snap, tick density, outer glow, internal backlight) plus the `--dial-*` CSS variables on `.radio-dial`. **The tuning panel**
  (`radio-dial-tuning.js`, live sliders, "Copy values") shows only on a dev instance (`CONSTRUCTICON_ENV=dev`)
  or with `/?tune=1` for an admin. Bake the owner's chosen numbers into `DIAL_DEFAULTS`.
- **One colour scheme for both tuners (PR #782):** the `--tuner-*` variables on `:root` in `style.css`
  (chassis, border, outer glow, inner backlight, readout hue and its derived blue + glow, needle/axis
  yellow, phosphor green for the dial's band labels) are read by the home dial AND the card page's
  timeline (`.project-video-timeline-*`, `.timeline-drag-readout`). Change a colour there, never in one
  tuner's rules. The tuning panel's colour sliders write the base values onto `<html>`.
- **The accent is hazard yellow (#785, was lavender):** ONE set on `:root`: `--accent-yellow-hue` /
  `--accent-yellow-light` build `--hazard-yellow` (= the needle's `--tuner-yellow`), and `--highlight`,
  `-hover`, `-ink` (dark text on any yellow fill), `-tint`, `-glow`, `-muted` / `-muted-edge` (resting card
  edges, the card page's timeline blocks) derive from it. No `--purple*` is left; the V2 card faces' own
  kind colours (Project / Family frames) stay. The tuning panel's "Accent colour (hue)" / "Accent tint"
  sliders nudge it (home page only, while the panel is open); bake a pick into `:root` AND `DIAL_DEFAULTS`.
- **Page gutters (#784):** `--rail-gutter` (rail width + breathing room, 64px) pads `.app-main` and
  `.app-header` on BOTH sides, so every page shares one centred content box; under 760px the right gutter
  stays rail-wide (60px) and the left is 20px (`--rail-gutter-left`). Don't re-pad `.app-main` per page.
- Empty, malformed or unloadable data shows a plain message in the chassis, never a blank bar.
- Pass 2 (not built): start-to-end span bars and a per-year density glow; their empty layers
  (`.radio-dial-layer-spans`, `-density`) and no-op `_renderSpans` / `_renderDensity` are the hooks.
- Checks: `node scripts/test_radio_dial_775.js` (maths), `node scripts/test_radio_dial_ui_775.js` (the
  widget against a fake DOM), `scripts/test_radio_dial_775.py` (the rendered page, the rail gone, who sees
  the tuning panel), `scripts/test_gutters_accent_784_785.py` (the gutters and the accent, static).

## Dates: real, approximate, undated (#725, "Import date is not a birthday")

Owner, 2026-10-17: stop using the import date for anything but the import date. **An item's date is
REAL or it is nothing.** `core/timeline.py` `resolve_item_date(row)` returns, in order: the
display-date override (set by hand), the date made (#425), `content_date` (EXIF, a container or PDF date,
a YouTube publish date, an accepted guess), the file's own mtime (`source_modified_at`) **only when it is
from before the install's first upload** (`db.first_upload_epoch()` = `MIN(capture_events.timestamp)`,
derived, cached until the DB changes; a later mtime is a download / copy date), else **None**. The upload
time (`timestamp`) is shown only as **"Imported <date>"** (the item page's header and DATES group).
`has_real_date` = not None (Curator coverage and the dates pip). `date_sort_key` puts None last;
`real_dates(items)` skips them.
- **Card spans** (`resolve_project_span`) are built from real dates only; the card's own Timeline
  start/end overrides win; an ongoing card's open end ("... - now") stays. A card with nothing real and no
  overrides has span **(None, None)**, not its `created_at` (faces show no date line, the home page's
  radio dial leaves it off, the DATES group says "No real dates yet").
- **What an item SHOWS** is `core/item_dates.py` `item_date(row)`: the real date; else, **when its card
  has a real-dated range, that range, approximately: "≈ Nov 2019 - Jul 2021 · from Desk Build"**; else
  **"Undated"**. Derived at read time, NEVER stored as `content_date`, and never an input to any span (spans
  read real dates only, so no circularity). **The lending card is the one the item was first filed into**
  (lowest `project_items.rowid`, the same card the file face's "Stacked · <card>" names), not the union of
  its cards. The lent range counts only non-sensitive, non-redacted items (the same for every viewer, so it
  can't hint that a hidden item exists), plus the card's overrides. One query for every card
  (`db.date_lending_rows`), cached by `db.change_fingerprint` (never across an open transaction). The grids'
  `card_date` / `card_date_epoch` (the range START for an inherited one, null for undated), file faces on
  a card's piles, MCP `date_shown` all read it.
- **The Curator's `needs_date`** (`core/curation_queue.py`, from `item_dates.needs_date_rows()`): one need
  per item with no real date AND no range to borrow (on a card with no real dates: grouped under that card;
  on no card: the "Undated files" group), never for a card's write-up (it documents the card; spans skip
  it for the same reason). It carries `item_slug` (the queue hides it from anyone who can't
  see the item) and its best guess as a one-click **"Use <date>"** (`POST /api/image/{slug}/date-guess`,
  editor, `items.accept_date_guess`: one imaged row, op `item_date_guess`, `batch_id` for Undo; refusals
  `already_dated` 409, `no_such_guess` 400, `bad_source` 400). Key `need:needs_date:<slug>`.
- **The guesser** (`core/date_guess.py`, pure; `scripts/test_date_guess_725.py`): each guess has a source,
  a confidence and its evidence. **Auto** (high, the evidence is in the file): `filename` (IMG_20190512_...,
  2019-05-12, 20190512, "Screenshot ... at 14.30.22", a 10/13-digit unix timestamp when it is the name's
  only long number; Facebook id runs never match), `file_metadata` (the type's `embedded_metadata_fn` date:
  EXIF, container, a PDF's CreationDate, ...), `youtube` (a stored `type_metadata.published_at`; nothing is
  fetched). **Suggested** (a click): `ocr_text` (a date in the extracted text: medium when it is the only
  one, else low) and `siblings` (median of the card's other real dates, low). Ranked filename > file_metadata
  > youtube > ocr_text > siblings. Naive dates are Mountain Time; a guess outside 1980..now is dropped.
- **Writes:** `items.apply_dates([(slug, epoch, record)])` writes guesses as `content_date` plus
  `type_metadata.content_date_source` = `{source, confidence, evidence, guessed: true, accepted: auto |
  by_hand}`, ONE imaged row (op `item_date_guess`) for the batch, **fill-only** (an item that has a real date
  is skipped, listed in `skipped`). New uploads take the filename date at upload (`embedded_metadata`, raw
  pipeline write, same record). The migration **`date_guess_backfill_725`** ran the auto guesses once over
  the archive (no network, one batch as actor `migration`, idempotent; undo the batch to take it back).
- **MCP:** `constructicon_undated(action="list" | "accept" | "apply_auto", ...)`: list the undated items with
  what they show, the range they borrow, `needs_date` and their guesses (`read_files=true` adds the file's
  own embedded date); accept one guess; apply every auto guess as one batch (`dry_run`). Role rows: list
  viewer, accept / apply_auto editor. **Never names a card the caller can't see (#604):** the list goes
  through `policy.filter_visible`, and `item_dates.listing_entry(row, access=...)` drops a hidden lending
  card's title from `shows`, `inherits` and the guesses' evidence (checked in `test_private_cards_604.py`).
- Checks: `scripts/test_undated_725.py` (throwaway DB: the owner's real-dates-only card, the lending rules,
  needs_date, accept / undo / refusals, apply_dates, the migration, uploads, pages, the MCP tool),
  `test_timeline_dates.py`, `test_card_date_sort_622.py`, `node scripts/test_item_sort_622.js`.

## Static export, preview and publish

The dynamic app freezes into a self-contained static site (relative links, media bundled), which can
be previewed in the app and pushed to a GitHub Pages repo. All of it is admin-only.
- **Build** (`core/site_export.py` `build_site(config, out_dir=None)`, `POST /api/export/build`):
  renders `web/export_templates/` for the chosen cards (default: `card_rules.export_included_by_default`,
  every in-progress card that isn't a collection, by its stage alone since #545)
  and `ready` blog entries, copies each item's media and thumbnail once, and returns a report with
  counts and `warnings`. What may leave the install is `policy.filter_exportable` (restricted and
  sensitive items never do, whoever asks), and only the current revision of a chain goes in. The
  footer carries "Built with Constructicon <version>". The submitted config is saved to the
  `export_config` setting (`GET /api/export/config`). A build with no `out_dir` writes
  `<exports>/<timestamp>/`, refreshes `<exports>/current` and prunes to the newest two builds; a build
  with an `out_dir` touches nothing else (#576).
- **Preview:** `/preview` is a mount over `<exports>/current` (`web/app.py` `_CurrentExportFiles`), so it
  serves the latest default build. It needs at least viewer; the bundled `media/` files get the same
  safe-serving headers as `/f/` (#610).
- **Publish** (`POST /api/export/publish`, body `{"target": "<name>"}`): `site_export.publish_build`
  commits `exports/current` into a work checkout (`<exports>/.publish/<target>`) and pushes it. The
  targets are install config (`GET /api/export/targets`; none configured = 400 `no_publish_target`,
  never someone else's repo, see "Install config"); the token is the write-only `pages_publish_token`
  setting, used only for the push URL and scrubbed from errors and from `.git/config`.
- **One lock:** build and publish share one lock; a second one while one runs is 409 `export_busy`.
- **UI:** the **Export** tab in the nav rail opens the export builder drawer
  (`web/templates/_export_builder.html`). Live pages only: the static site's own templates are
  `web/export_templates/`, not `web/templates/`.
- **Separate and older:** `core/project_export.py` / `GET /api/projects/{id}/export.zip` bundles one
  card's contents as a zip for offline or agent analysis; it is not the site export.
- There is no test file of its own for the export. Its lock and failure paths are covered by
  `scripts/test_async_blocking_550.py` and the `/preview` mount's headers by
  `scripts/test_f_sandbox_610.py`.

## Decisions and the Curator

Two ideas share the nav rail's "Curator" tab and one queue:
- **Decisions** (`core/decisions.py`, table `pending_decisions`): stored questions the app will not
  answer for itself. The kinds are listed under "Data model" (`project_match`, `retype`,
  `item_supersedes`, and the four `card_*` kinds). `list_open()` / `count_open()` read, `resolve()`
  answers, `sweep_stale()` closes the ones nothing can answer any more (see "Service layer").
- **The Curator** (`core/curator.py` scoring, `core/curator_needs.py` nudges, `core/curation_queue.py`
  the queue; #519): `curator.py` scores a card's completeness from declarative `SCORING_RULES`;
  `curator_needs.py` derives **nudges** from the unmet checks (missing cover, missing write-up, thin
  captions, ...), never stored; `cards.list_needs_decision` supplies **needs** (missing provenance, a
  blank write-up, an untyped link, hobby flags). `curation_queue.py` merges questions, nudges and needs
  into ONE list grouped by card (order rules in its docstring). Only the owner's **Defer** / **Dismiss**
  is persisted (`curator_dismissals`, keyed by the item's `key`; a question can be deferred, never
  dismissed).
- **`needs_date` (#725):** a file with no real date and no card range to borrow, one need per item, with its
  best date guess as a one-click "Use <date>" (see "Dates" above).
- **One report card (#545, owner decisions 2026-10-17).** The Curator score and the card pips
  (`core/card_level.py`) used to grade the same card differently. Now the checks they share are
  defined once, in `card_level`: `check_cover`, `check_writeup` (the write-up has **>= 200
  characters**, spaces not counted) and `check_dates` (**>= 80%** of the card's files, write-up
  excluded, have a real date per `timeline.has_real_date`, or both the card's start and end are set).
  `curator.score_project` calls them for `cover_image` / `writeup_body` / `content_dates`, and those
  checklist rows carry the pips' `reason`, which is also the summary of the matching nudge
  (`missing_writeup`, `missing_dates`). A new shared rule goes into `card_level`, never into both.
  **The stage is the only status:** `curator.STAGE_APPLICABILITY` is keyed on `card_rules.STAGES`
  (done / in use: every check; in progress: the timeline gap and the highlight excused; paused,
  idea, stopped: silent), plus a reference collection (kind collection, provenance none or
  referenced) is silent. Scores and the dashboard return `stage` / `stage_label`
  (`projects_by_stage`), never a v1 word; a nudge's `status_weight` is 3 for done / in use, else 1;
  the stale nudge (`stale_wip`, key unchanged) is for in-progress cards. The v1 adapters
  (`card_rules.curator_status`, `export_status`, `cards.curator_status_for`, `STATUS_APPLICABILITY`,
  the means-to-an-end scoring, `db.PROJECT_STATUSES`) are gone. Check with
  `scripts/test_one_report_card_545.py`.
- **Where it surfaces:** the **Curator tab in the nav rail** (`_nav_tabs.html`) opens the Curator drawer
  (`_nudges_drawer.html`, rendered by `static/js/curator-queue.js`); its count badge is
  `GET /api/curator/queue?summary=1` and refreshes on the `pending-decisions-changed` /
  `needs-changed` events; `/curator` is the full page; `/admin` ("Needs your input") now only shows the
  open count and a link. The old header gear badge and the admin-page list are gone (#345, #519).
- **Routes** (`web/routes/curator.py`): `GET /api/pending-decisions`, `POST
  /api/pending-decisions/{id}/resolve` (editor); `GET /api/curator/dashboard`, `/needs`, `/queue`,
  `/queue/html`; `POST /api/curator/needs/dismiss`, `/queue/defer`, `/queue/bring-back` (editor). The whole
  queue is built once as actor `system` and cached until the database changes
  (`db.change_fingerprint`) or `CACHE_TTL` (120 s) passes, then filtered per actor on read
  (`curation_queue.for_actor`, see "Ownership and sensitive items").
- Checks: `scripts/test_bulk_decisions.py` (bulk-accepting card decisions) and
  `scripts/test_ownership_sensitive.py` (the queue as each role sees it), both on a throwaway DB, and, against a
  live instance, `scripts/check_curator_queue.py`.

## Writing a card's write-up (#638)

A process for Claude sessions; the app does not draft write-ups itself yet (that is #639). Two passes, in
this order, and never straight to final prose:
1. **Evidence pass** (from the card's own items, via `constructicon_get_project`):
   - **When:** sort items by their resolved date (`effective_date`) and cluster same-day or close items into
     likely work sessions. That is the story's skeleton.
   - **What and where:** read each item's auto-caption (`type_metadata.auto_caption`) and OCR text
     (`extracted_text`) per cluster. A detail that recurs across independent items is signal; a detail that
     appears once is probably caption noise, so drop it. **Any count in an auto-caption is untrustworthy**
     (wheels, parts, anything): moondream fails at counting every time, and repetition doesn't fix it.
   - **Which parts:** CAD/STL/source filenames are a separate signal that photos can't give (part names, sub-assemblies).
   - **Seams:** files sharing a tag or card are not necessarily one story. On Clod-a-Pede, a set of truck-face
     STLs looked like part of the mascot but was an unrelated, abandoned idea. Flag anything that could be a
     separate or abandoned direction; don't stitch it in.
   - Write this up as a **draft** with confidence noted and the open questions listed.
2. **Oral-history pass** (with the owner): bring the draft and ask how it worked and why, one question at a
   time. The owner's answers go into the write-up as fact; any Claude analysis goes in a separate, clearly
   labelled section. Confirm an interpretation before stating it. The data has the "when" and the "what";
   only the owner has the "why" (intent, what was tried and dropped, what a part is for).

Only then save it (`constructicon_update_project(card, fields={"writeup_slug": ..., "owner_words": true})`). If the owner isn't available, a write-up built from the
evidence pass alone must say so at the top ("Reconstructed by Claude from the project's own files ...").
Voice: narrative, in chronological order, with real dates and tools named, not a dry spec sheet.

## Adding an object type

To add a new object type (issue #448 contract v2):

1. **One new file in `core/object_types/`**: create a module (e.g. `core/object_types/mytype.py`) with:
   - A `preview(ctx: PreviewContext)` function returning `markupsafe.Markup` HTML or None.
   - A `properties(row: dict)` function returning a `dict[str, str]` of display properties.
   - A `register(ObjectTypeSpec(...))` call at the end with both functions, plus optional hooks below.

2. **Required fields**: Every type must declare `preview_fn` and `properties_fn` at registration (enforced, will raise `ObjectTypeContractError` otherwise).

3. **Optional hooks** (all callbacks, all in the same module file): `sniff_fn` (content-based file detection), `pre_store_fn` (accept/reject/defer uploads), `url_match_fn` (classify external URLs), `actions` (per-row UI actions), `edit_fields` (form fields for type_metadata), `preview_assets` (deferred assets), `embedded_metadata_fn` (extract metadata from the file at upload time — used once, never per-view), `writeup_body_key` (the type_metadata key holding write-up text, if this type can be a project write-up), `raw_text_fn` (#703: `(path) -> codec or None`, whether `/f/<slug>/raw` may show this file as plain text; `code` has one), and `sniff_priority` (for resolution when multiple sniffers claim the same extension).

4. **Preview guidance**: previews must escape all data (no raw HTML from untrusted sources; build with `_preview.py` helpers or `markupsafe.escape`) and handle `ctx.mode`: `"live"` (object page) and `"export"` (static site; most types render export markup via the helpers, some deliberately return the download link or `None` to keep the export as it was). A preview that needs its own file reads `ctx.file_path`; never guess at item-dict keys. **Anything expensive goes in `embedded_metadata_fn` (computed once at upload, stored in `type_metadata`), never in `preview_fn`/`properties_fn`, which run on every page view.** Measured lesson: per-view STL mesh parsing took 7.9 s on a real 22 MB file.

5. **Verify BEFORE deploying**: run `./scripts/run_tool.sh constructicon-test scripts/check_object_types.py` (from the host, in the test checkout; #634, see "Scripts: what runs where") first; it is also in `run_checks.py`. It renders every type live + export against hostile synthetic data, flags unescaped output, and exits 1 on any failure. Registration enforcement means **an incomplete type file stops the app from booting at all** (verified: restart loop with `ObjectTypeContractError`), so a bad type deployed to prod = the site is down. Then snapshot real object pages + the static export before/after the change and diff them.

   A type whose `sniff_fn` can refuse a supported extension should set `content_noun` (a string, or
   `{ext: noun}`), so the refusal reads "This .msi file isn't a valid Windows Installer package"
   (`object_types.mismatch_reason`, #602) rather than "Unsupported file type". `vpptoken.py` (#602) is
   the model for a restricted credential type that stores facts only (never the secret).

6. **No other changes needed**: All dispatch (thumbnails, OCR, type lists, etc.) works off the registry. No per-media-type branches in templates, no `if media_type == "..."` conditionals in app code — the registry is the single source of truth.

7. **Reference**: see docs/design/object-type-contract-v2.md for the full specification.

**Previews of big, HTML, wide and non-UTF-8 files (#606, #607).**
- **A preview never carries the whole file.** `code` reads only the head of the stored file
  (`_textstats.read_head_lines`: at most 2,000 lines, 200 K characters and 140 K of escaped markup, so the
  object page stays near 300 KB whatever the file size), says "Showing the first N of M lines" (the count reads
  at most 64 MB: "at least" past that) with Download and Open raw (#703: `/f/<slug>/raw`, the source as plain
  text in a new tab; offered only when `code.raw_text_codec` says the file is showable text, and not for `.html`,
  which has its Rendered / Code toggle instead), and leaves the block unhighlighted above 60 K characters
  (`data-nohl`). **`extracted_text` is never embedded in the page**: the template gets only
  `item.extracted_text_chars`; the OCR/text panel fetches `GET /api/image/{slug}/text?limit=` on first open
  (viewer, item policy, default 100,000 characters, hard cap 1,000,000, also returns `ocr_status` for the OCR
  poll). The OCR lamp's tooltip is a short label, and the link scan reads only the first 100,000 characters.
- **HTML files** (`code` with `.html`) get a Rendered / Code toggle (choice in localStorage
  `constructicon.htmlPreviewView`). Rendered is `<iframe sandbox="">` (no tokens at all: no scripts, forms,
  popups, same-origin or top navigation) loading `GET /api/image/{slug}/rendered` (viewer, item policy,
  redacted = admin only), served as UTF-8 with `Content-Security-Policy: sandbox; default-src 'none'; img-src data:;
  style-src 'unsafe-inline'; font-src data:; media-src data:; form-action 'none'; base-uri 'none';
  frame-ancestors 'self'`, `nosniff`, `Referrer-Policy: no-referrer`. So a saved page can't phone home, run script or
  read the cookies; images and fonts show only when embedded as `data:`. The frame is a fixed 70vh box (big pages
  scroll inside it); a file over 8 MB waits for a "Load rendered view" click. Limits: a link inside the frame can
  still navigate the frame itself, and a `<meta refresh>` likewise (no `navigate-to` in CSP).
- **Tables** (`data`, `spreadsheet`): `ObjectTypeSpec.preview_layout="table"` gives the preview the page width
  (`.dp-hero-left.dp-wide.dp-table`; the side panel wraps below) and turns the flex-row frame into a block, which
  was the cause of the squeezed, 250 px rows (see #606). Rows are one line (`nowrap`, ellipsis, 32ch max
  per column, 4px padding, line-height 1.35), every cell carries its full value as its `title` (click to
  expand), header and first column are sticky. Image, PDF and video pages keep `dp-wide` (700 px).
- **Encodings** (`core/object_types/_textstats.py`): `detect_encoding` reads a BOM (UTF-8-sig, UTF-16 LE/BE,
  UTF-32 LE/BE) or, with none, the NUL pattern of UTF-16; `open_text` / `read_text` decode accordingly (bad bytes
  replaced) and every text reader uses them (code, data/CSV, text, markdown, text stats; the HTML rendered view
  normalises to UTF-8). SQLite's `length()` stops at the first NUL, so a UTF-16 file read as UTF-8 looked "3
  characters long" in SQL while holding a full NUL-ridden string. The one-time `reextract_utf16_text_607`
  migration re-extracts rows whose file reads as UTF-16/32 and whose text is NUL-ridden or short.
- Check with `scripts/test_previews_606_607.py` (throwaway DB; the xlsx part needs openpyxl, in the app image).

### Type sampler and drop test (#720)

One generated file of every registered type and extension, to drop-test a new install (owner, 2026-10-17).
- **Generate** (in the app image, so the executables get made on Linux):
  `docker run --rm --network none -u 1000:1000 -v "$PWD":/app:ro -v "$PWD/samples-out":/out -w /app <image> python scripts/make_type_samples.py /out`.
  It writes one sample per entry in its `SAMPLES` list (108 files: every extension any type claims, incl.
  both `.zip` types and both `.exe` types), `content.json` + `urls.txt` (the no-file types: YouTube, web
  page, written post), `manifest.json` (per file: the type it must land as and what a correct install shows,
  derived from the registry at run time) and a `README.md` table. Nothing comes from the archive; credentials
  are obviously fake (TEST / NOT REAL, a key from a public fixed seed, VPP token "TEST-NOT-A-REAL-TOKEN").
  Formats with no writer in the image (legacy Office/MSI, ISO, VHD, xar, rpm, PE, ELF, SketchUp, ...) are
  hand-built minimal files. Byte-identical every run except `VARYING` (the PKCS#12 pair, the .7z).
  `--list` prints the manifest; `--no-executables` skips the executable class.
- **Drop-test an install** (writes to it; never prod: it refuses `/api/version` env `prod` without
  `--i-know-this-is-prod`): `./scripts/run_tool.sh <web container> scripts/drop_test.py --base-url
  http://localhost:<port> --samples <dir>` (the samples dir must be visible to that container), or from any
  container in the install's network namespace with `CONSTRUCTICON_TOKEN_FILE`. It uploads through
  `/api/upload` / `/api/content`, waits on `/api/processing`, prints one row per file (expected vs detected
  type, thumbnail, text, object page, `/f/` disposition, raw view, content date, and a temporary VIEWER
  account's view: restricted items must 404), then deletes exactly its own slugs and empties the trash only
  when it held nothing else. `--keep` leaves the items. Verdict line `ALL PASS: ...` / `FAILED: ...`.
- **Known findings it shows:** the web link (`https://example.com/`) reads its page title with network
  and settles grey "web link: ..." without (#724 d); legacy `.doc/.ppt` end OCR "not compatible" (grey,
  `ocr_status='unsupported'`, #724 part a: the sampler's `.doc/.ppt` are stubs, not real Word 97 /
  PowerPoint 97 files, so the part b readers find nothing to read; a real one gets its text, see
  `scripts/test_legacy_office_724b.py`); an encrypted Office file reads "not compatible: encrypted: can't
  read without the password", a corrupt one fails with "corrupt or unreadable .<ext>: ..." in `ocr_note`
  (`object_types.NotCompatibleFile` / `UnreadableFile`, owner decision 2026-10-17); the hand-built `.xls` stub
  ends "failed" (xlrd can't parse it: a real read error). (WAV downloading instead of playing, #723, is fixed: its row passes.)
- **Windows / Defender (owner rule):** the executable-class samples (PE `.exe`, every installer package,
  the zipped `.app`, ELF firmware, the `.dmg`) are written only when `sys.platform` is Linux, and never
  committed: the default output `samples-out/` is gitignored and `.dockerignore` keeps `scripts/` and it out
  of the image. Never generate them on, or copy them to, the Windows machine.
- **Checks:** `scripts/check_type_samples.py` (static) fails when a registered type or extension has no
  sample; `scripts/test_type_samples_720.py` generates the set twice into a temp dir and checks every file is
  written, sniffs as its type (`detect_media_type`) and is stable (skips the executables off Linux). A new
  type or extension needs a `Sample(...)` line in `make_type_samples.py`.

## Data model (verify against `core/db.py`'s `SCHEMA` before trusting this — it evolves)

- **`capture_events`** — the core item table. Despite the name (a holdover
  from imagerepo's screenshot-capture origins), this holds *every* kind of
  object: images, PDFs, STLs, PSDs, SVGs, EPS, audio, YouTube links,
  whatever else the `core/object_types/` registry holds. Key columns:
  - `slug` — unique, unguessable, used in URLs.
  - `media_type` — loose classifier (`'image' | 'video' | 'youtube' |
    'document' | 'any'`, ...). **Deliberately no CHECK constraint** — not a
    rigid enum, a new value just needs a registry entry in
    the `core/object_types/` registry.
  - `tech` — repurposed. Originally "which technician uploaded this" in
    imagerepo's multi-user days; now a free-text **Source** label ("who or
    what added this row, and how"). See `source_manual_upload()` /
    `source_automated_upload()` (the owner label is install config, #562), `SOURCE_AUTHORED`,
    `source_migrated_from()`/`source_group()` for the fixed vocabulary
    (manual web upload, the legacy automated-upload label, migrated by
    Claude from a named source, or authored by Claude directly).
  - **Gone (#704, owner decision 2026-10-17):** `source` (how the dead uploader app captured a
    row; link vs file comes from the type's `is_file`), `client`, `ticket_id` (imagerepo
    IT-ticketing), `artifact_link` (always `/f/<slug>`; the payloads carry `url`) and the `icon`
    override (never set). `db._drop_dead_columns_704` drops them at web boot (`init_db`, every
    boot, never the MCP), together with the `clients` / `client_domains` tables, after counting:
    a column or table holding REAL data is KEPT (client/ticket_id/icon non-empty; artifact_link not
    the row's own `/f/<slug>`, relative or absolute; source outside NULL/''/'screenshot'/'external';
    any client row but the three 'special' placeholders; any client_domains row), with a
    `WARNING drop_dead_columns_704: KEPT ...` line at every boot saying how many rows and how to see
    them; it drops once they are cleared. The first run is recorded in `schema_migrations`; a run
    that drops or keeps anything writes one change-log row (op `migration_drop_dead_columns_704`,
    counts in `details`). Nothing reads or writes a kept column. Row images logged before the drop
    still name the old columns: `db.invert_image` restores only the columns the table has now, so
    those undos keep working. Check with `scripts/test_drop_columns_704.py`.
  - `filename`/`stored_filename` — for uploaded files; `NULL` for content
    with no local file (e.g. `media_type='youtube'`, which uses
    `external_url` instead). `db.insert_upload()` handles the file path;
    `db.insert_content()` is the thin wrapper for the no-file path.
  - `description` — uploader/tagging metadata, vs. `content_description` —
    the content's *own* description/title (e.g. a YouTube video's real
    title). Similarly `timestamp` (capture/upload time) vs. `content_date`
    (the content's own real-world date). These pairs are easy to confuse —
    check which one a given piece of code actually means. **#725: `timestamp` is never an item's
    date** (only "Imported <date>"); a guessed `content_date` records where it came from in
    `type_metadata.content_date_source`. See "Dates".
  - `display_name` — optional per-object override of the item's title (the badge icon is always
    the type's `badge_icon`; the per-object `icon` override went in #704). **An item's title (#542) is
    `core/item_title.py` `title_of(row)` and nothing else:** `display_name` → `short_title` →
    `content_description` **only when hand-written** → `filename` → `slug` (owner decisions on #542:
    2026-10-18 "a separate short title field", 2026-10-25 "keep the description as the title
    fallback ONLY when it is hand-written"). **A model's caption is never the title.** Hand-written vs
    model (`item_title.description_is_handwritten`, the rule lives only there): caption provenance
    `description_caption_model` "owner" = hand-written; any other model, or
    `description_caption_used_at` with no model, or the text equal to `auto_caption` = a model
    caption; no caption record at all = hand-written (the uploader's or a migration's own words, a
    content's own title). A hand edit of a captioned description (`items.update`) re-records it as
    "owner" (or "mcp-agent" when the MCP made it). `description` (the uploader's note) is never a
    title either. Python calls `title_of`; Jinja uses the `item_title` filter (the
    static export registers it); JS reads the `display_name` (item payloads) or `title`
    (project grid, timeline, blog entry items) the server already resolved and never
    rebuilds the chain. `scripts/test_item_title_542.py` fails on a new hand-rolled
    chain.
  - `short_title` (#542) — the item's short name, a few words (at most `item_title.SHORT_TITLE_MAX`,
    100). Where it comes from: a content's OWN title given at creation (`db.insert_upload`: a
    YouTube title, a write-up's "<card> — Write-up", a link, an MCP `add_content` title) and a file's
    embedded title (`core/embedded_metadata.py`), when one line and short enough; a caption's title
    (the local captioner suggests one with every caption, `constructicon_set_caption(title=...)`, the
    caption review page); an accepted caption that is itself name-shaped (one line, at most
    `CAPTION_AS_TITLE_MAX`, 80) when no title came with it; by hand (the item page's ABOUT group,
    `POST /api/image/{slug}` `short_title`, MCP `constructicon_update(short_title=)`; `items.update`).
    The `short_title_seed_542` migration copied every existing name-shaped `content_description`
    into it once (one change-log row, op `item_short_title_seed`, actor `migration`), so those kept
    their names; longer model captions show the filename until a short title is accepted (longer
    hand-written ones still show the description). **Backfill:** `captions.propose_short_titles`
    proposes one for every item whose long description used to be its title, into the caption
    review queue (nothing auto-applies; dry run by default; `POST /api/captions/propose-titles`
    admin, or `scripts/caption_cleanup.py propose-titles [--source auto|words|model] [--execute]`).
    **Words-first** (owner decision 2026-10-25), the default for hand-written descriptions: a title
    cut from the item's own words on word/phrase boundaries (`item_title.title_from_words`, no GPU,
    author `words`); model captions go to the local captioner (`suggest_title`), left out with a
    reason when captioning is off. Everything a run writes shares ONE batch, so one undo takes the
    whole backfill back (the worker then skips what is still queued). Search indexes it (folded into
    the view's `display_name` column). Check: `scripts/test_captions_titles_787_743.py`.
  - `uploaded_by_user_id` (#604 step 1, NULL = admin-owned) and `sensitive` / `sensitive_by` /
    `sensitive_at` (#603): see "Ownership and sensitive items".
  - `type_metadata` — freeform JSON bag for per-type properties that don't
    fit a generic column (e.g. YouTube view/like/comment counts; an audio
    file's ID3 artist/album/track/year/genre, #255). One
    shared column so a new object type never needs a schema migration; see
    `core/object_types/__init__.py`'s `MetadataField` for the documented shape per type.
  - `extracted_text`, `perceptual_hash`, `embedding`, `ocr_status` —
    OCR/similarity pipeline state. `ocr_status` is `pending | done | failed | unsupported` (NULL =
    not an OCR type). **#724: `failed` is only a real error** (tesseract or the pipeline raised, a
    text reader raised, the stored file is missing, a picture that should exist couldn't be made).
    `unsupported` = nothing OCR can read (no text source and no picture expected:
    `object_types.nothing_to_read` / `picture_expected`; a legacy .doc/.ppt whose reader
    returns None), with the plain reason in `type_metadata.ocr_note` (the type's
    `ocr_unsupported_note`, str or `{ext: note}`); `db.set_ocr_status(slug, 'unsupported', note=...)`
    writes both, any other status clears the note. Shown grey ("Not compatible with OCR: <note>")
    in both lamps, `off` in the processing drawer; Retry still runs it. A text reader that read the
    file and found no text (`""`) settles `done` with empty text. The `ocr_unsupported_724` migration
    re-settled old `failed` rows that pass `nothing_to_read` (only those). Check with
    `scripts/test_ocr_unsupported_724.py`.
    **#724 (c), more than one text source:** a type with `ObjectTypeSpec.text_sources_fn` (YouTube:
    its stored `type_metadata.description`) gets that text in `extracted_text` too, so search finds
    it: `core/text_sources.py` `combine()` puts each source with text under a header
    ("Description:" ... then the picture's OCR text last, "Thumbnail text:" ...) and
    `type_metadata.text_source` names the sources that gave text, `+`-joined in order
    (`description`; part e's transcript will make it `description+transcript`). OCR text alone keeps
    no header and no `text_source`, as before. No source text and no picture text = grey
    `unsupported` ("video: no description or captions yet"); a thumbnail that can't be made or read
    keeps the description, `done` with a warning `ocr_note`. A corrected description
    (`items.update`) rebuilds the text in the same imaged change (`text_sources.resettle`, no OCR
    re-run; the stored thumbnail section is recovered by `picture_text_of`), so one undo restores it.
    The `text_sources_724c` migration did the same once for existing settled rows. Check with
    `scripts/test_youtube_descriptions_724c.py`.
    **#724 (d), web links read their page:** `url`'s `text_extract_fn` (`url.page_text`, in a parse
    child like every reader) fetches the page through `core/safe_fetch.py` and stores its `<title>`,
    `meta name=description` and `og:title` / `og:description` (stdlib `html.parser`, deduplicated,
    capped) as "Page title:" / "Page summary:" sections, `text_source` `page`. Settles: text -> `done`;
    the site blocks fetching (cults3d.com's Cloudflare 403 challenge: 16 of prod's 18 links), the guard
    refuses the address, not HTML, no title/summary, or fetching turned off -> grey `unsupported` with
    the reason ("web link: site blocks fetching (HTTP 403 challenge)"); a page over 2 MB is read to
    the cap and parsed (`fetch(truncate=True)`; nothing in the first 2 MB -> grey "no title or summary
    found in the first 2,000,000 bytes"); a dead link, HTTP 404/410 -> RED `failed` "page not found
    (HTTP 404): dead link, or a private page this server can't see" (owner, PR #754: "Red, it's
    broken"; GitHub answers 404 for a private repo); a timeout, an unreachable site, HTTP 429/5xx ->
    `failed` "...; try OCR again". Retry re-fetches any of them. Nothing tries
    to get past a bot check. The `link_page_text_724d` migration re-queued existing `failed` /
    `unsupported` url rows to `pending` once (web's startup self-heal re-runs them; the fetches go one
    at a time). Check with `scripts/test_safe_fetch_724d.py`.
    **Outbound fetches (#724 d, `core/safe_fetch.py`).** A stored URL is untrusted input (it could aim
    the server at the NAS, the router or a metadata address), so `safe_fetch.fetch(url, accept=,
    max_bytes=, timeout=, headers=, polite=)` is the only fetch path (the page reader, and
    `thumbnails._from_fetch` for YouTube thumbnails). It allows http/https on ports 80/443 only, no
    credentials in the link; resolves the host itself and refuses ANY non-public answer (RFC 1918 incl.
    10/8, loopback, link-local incl. 169.254.169.254, CGNAT 100.64/10, multicast, unspecified,
    reserved, IPv6 ULA / site-local / link-local, and the IPv4 inside IPv4-mapped / 6to4 / NAT64);
    **pins the checked IP** for the connection (request to the IP, real name in `Host` and as TLS SNI,
    so the certificate is still checked; one client per hop, `trust_env=False`), so DNS rebinding
    can't slip through; follows redirects **by hand** (max 5), re-checking scheme, port and the
    resolved IP on every hop; caps the body (2 MB, counted while streaming, `Accept-Encoding:
    identity`; over it = `TooLarge`, or with `truncate=True` the first 2 MB and the rest never read),
    the whole fetch (10 s shared by DNS, connect and reads) and the content type
    (text/html, application/xhtml+xml by default; thumbnails ask for `image/`). The page reader sends
    an honest User-Agent (`Constructicon/<version> (...)`); thumbnails keep their Chrome-style UA
    (#284, Imgur). `polite=True` (the page reader) takes a per-container one-at-a-time turn (a lock
    file in the temp dir, so parse children share it) with `CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS`
    (default 2) between fetches. Every refusal/failure is a `FetchError` with a plain reason and a
    `transient` flag. `CONSTRUCTICON_LINK_FETCH=off` turns page fetching off for an install;
    `_testenv.isolate()` sets it, so no test reaches the internet unless it turns it on with fakes
    (`safe_fetch._getaddrinfo` / `_transport`).
    **#724 (e), YouTube transcripts (`core/transcripts.py`).** Captions through
    `youtube-transcript-api` (pinned; an unofficial endpoint, owner accepted). `ObjectTypeSpec.text_fetch_fn`
    (YouTube: `youtube.fetch_transcript`) runs in the OCR thread before the sources are combined and
    returns `type_metadata` changes, stored by `db.set_text_source_fields`; `text_sources_fn` then reads
    `type_metadata.transcript` as a "Transcript:" section (`text_source` `description+transcript`). Bounds:
    a **hard 20 s deadline** on the library's whole run (a parse_limits child killed at the deadline,
    `parse_limits.call(seconds=)`; no sandbox = an abandoned daemon thread), safe_fetch's one-at-a-time
    `polite_turn()` (shared with page fetches and their delay), `CONSTRUCTICON_TRANSCRIPTS=off` (default
    on; `_testenv.isolate()` sets it off; **the home prod install runs it off** until YouTube stops 429ing
    the NAS, owner on PR #756). Off = nothing fetched, a video with no status yet is marked `queued` (the
    backlog waits for it to be turned on), and the "Captions" line says "transcripts are turned off on
    this install". **Web only:** the MCP process never fetches (`transcripts.queue_only()`,
    `CONSTRUCTICON_ROLE=mcp`, like captions): its OCR runs mark the video `queued` (a Retry OCR there
    re-queues, so web asks again). Manual captions over auto-generated, English first, any language
    recorded (`transcript_language`, `transcript_kind`). Noise filter: bracketed tags, sound words in
    parentheses, music notes, filler-only snippets ("foreign", "um") dropped; under 30 real words = none.
    `transcript_status`: `found`; `none` (no captions, captions off, only noise, video unavailable:
    settled, the grey reason of a row with nothing else says "no captions"); `retry` (network error,
    timeout, YouTube blocking or rate-limiting: reason in `transcript_note`, backoff 15 min doubling to a
    day, stops by itself after 8 tries; NEVER sets OCR failed and never removes text or an earlier
    transcript); `queued`. The object page's properties show a "Captions" line. Retries and the backlog:
    web's `_transcript_loop` (every 5 min, up to 3 rows, one at a time, `transcripts.work_due`) works
    `queued` / due `retry` rows and folds each result into the stored text without OCR (resettle); a block
    stops the pass early. The `transcripts_724e` migration queued existing YouTube rows (no network).
    Seen live 2026-10-17: YouTube answered HTTP 429 to the caption download from the NAS address for some
    videos (`IpBlocked`), which lands as `retry`. Check with `scripts/test_youtube_transcripts_724e.py`.
  - `redacted` — "file removed, metadata kept" (the detail page's "Remove
    file, keep info" button / `constructicon_redact`; since #541 `items.redact`
    holds the file in the trash with no expiry until the owner recovers it or
    deletes it permanently, see "Trash" above). Since #282 a
    redacted row is hidden from **every** list/browse/search query in
    `core/db.py` (`search`, `list_unfiled_items`,
    `list_recent_items_by_type`, `list_project_items`,
    `list_posts_for_tag`, and `list_uploaders`'
    totals) and reachable only by its direct `/object/<slug>` link, the
    admin page's (`/admin`) "Redacted items" list (`GET /api/redacted` /
    `db.list_redacted()`) or the MCP `constructicon_list_items(which="redacted")`.
    `POST /api/image/{slug}/unredact` / `constructicon_redact(action="unredact")` flips it
    back — visibility only, for redactions whose file is already gone (refused while a
    file is still held: recover it or delete it permanently first).
    `db.search(include_redacted=True)` is the escape hatch for a caller that
    must see hidden rows (delete-all now reads every row directly, in
    `core/reset.py`). There is no `redacted_at` column.
- **`blog_tags`** — the tag tree: `{id, name, slug, parent_id}`, nestable
  to arbitrary depth via self-referencing `parent_id`. Not a fixed
  Section/Category/Tag split — a post can attach to any tag at any depth,
  and to more than one branch at once. A tag is looked up / created per
  parent (`tags.find` / `tags.ensure`; the same tag name can exist under
  different parents).
- **`post_tags`** — many-to-many join, `(post_slug, tag_id)`, between
  `capture_events.slug` and `blog_tags.id`.
- **`projects`** — hand-curated portfolio collections, deliberately
  *distinct* from the tag tree (auto-grouping by tag was the original plan
  and was rejected in favor of manual curation — "projects are how the
  other objects come together"). Has an optional `tag_id` linking the
  project to a `blog_tags` row. `cards.create` sets it at creation time: it reuses the root tag of
  the same name or makes one (`link_tag=False` makes none; a hobby -> card conversion reuses the
  hobby's tag), so tagging into a project also surfaces it via ordinary tag browsing. The link is the
  id, **not** the name: nothing renames the tag when the card is retitled (or the reverse), so the two
  names drift (7 of 75 prod cards had, per #552) and older cards have `tag_id` NULL.
  `private` (#604 step 3, 0/1) ticks the card private; its members are `share_members` (see "Private
  cards and hobbies"). `agent_notes` (#745) is the card's private agent scratch field: never in a row `core/db.py` hands
  out, read only by `db.get_card_agent_notes` (see the MCP section, "Agent notes on cards").
- **`project_items`** — many-to-many join, `(project_id, post_slug,
  sort_order)`, with a manual `sort_order` for deliberate (non-chronological)
  ordering within a project.
- **`clients`, `client_domains`** — gone (#704): the imagerepo IT-client list, its OCR client
  matcher, `/api/clients` and the repo-root `sync_clients.py` were removed; see "Gone (#704)" above
  for the safeguard that keeps either table if it holds anything but the old placeholder rows.
- **`app_settings`** — generic key/value store for app-level secrets (e.g.
  the YouTube Data API key), so new integrations don't need a
  docker-compose env var wired in from outside. `GET /api/settings` only
  ever reports *presence* of a key, never its value.
- **`pending_decisions`** (#240) — a small generic "don't auto-decide, ask
  the owner" queue: `{id, kind, post_slug, payload JSON, created_at,
  resolved_at}`. Resolved rows are kept (resolution stored in `payload.resolution`). A new "ask, don't
  guess" case adds a new `kind` + payload shape, not a table. The kinds that exist, who queues them
  and who answers (see "Decisions and the Curator" for how they surface):
  - `project_match` (`core/automatch.py`): an upload's filename/folder name matches more than one card
    title (one match auto-adds, tag-name matches always auto-apply).
  - `retype` (`core/ingest.py`): a type's pre-store hook answered `needs_decision`, so the file is stored
    under a provisional type and the owner picks the real one.
  - `item_supersedes` (`core/revisions.py`, via `ingest.auto_match`): see "Revision chains" below.
  - `card_status`, `card_kind`, `card_built_for`, `card_family_members` (the "four `card_*` kinds",
    `core/cards.py`'s `CARD_DECISION_KINDS`): V2 card questions. `post_slug = "card:<project slug>"`,
    NOT a `capture_events` slug, so `decisions.stale_reason()` branches on that prefix (validating
    against `projects`); the file-row stale check would otherwise call every card question stale.
    `core/card_migration.py` queues the first three; `card_family_members` was queued only by the
    one-time v2c_3 step, now `scripts/archive/v2c_owner_questions.py` (#562; never moves anything).
    Resolving a family question runs unnest -> set_kind family -> add_to_family in one change-log batch.
  Answering is `POST /api/pending-decisions/{id}/resolve` (editor; `core/decisions.py` `resolve`). Since
  #551 item 3 no read resolves anything: stale questions are left out of `list_open()` and resolved only
  by the explicit, logged `decisions.sweep_stale()` (see "Service layer").
- **Card kind + status (V2, piece 1)** — `projects.kind` plus
  `activity`/`stage`/`stop_reason` are the *live* status, and the stage is the ONLY status (#545):
  legacy `projects.status` is **frozen and retired**. No live code reads it (the Curator, the export,
  every API / page / MCP read go by the stage; no read returns the old word). It is still written
  with its default on create, and only the one-time v1 -> v2 migration ever read it. Dropping the
  column is a separate step (#760). A v1 word a caller SENDS (the old `status` form field, the MCP's
  retired `set_project_status`) is still translated to a stage by `card_rules.legacy_to_status`.
  Rules live in `core/card_rules.py` (pure), operations in `core/cards.py`,
  every core write is logged with row images in `audit_log` via
  `core/changes.py`, and the v1 -> v2 mapping is `core/card_migration.py`
  (run from `init_db()`, idempotent: only cards with `stage IS NULL`). `card_migration.suggest_kind`
  (Thing or Project) reads what the card holds (#584): mostly non-photo/3D files -> Project ("61 of 68
  files are source code"); only photos/3D files, physical-piece fields or a whereabouts -> Thing; no
  evidence -> Project. The title is not read.
- **Families + nesting (V2, piece 3)** — `family_members(family_id, member_id)`
  is many-to-many membership for `kind=family|collection` cards (not nesting;
  no files move). `projects.parent_id` now means "part of" only:
  `card_rules.validate_nest` (no self/cycle/second parent, neither end a group
  kind) guards `cards.create` (`db._create_project`) and `cards.nest`;
  `card_rules.validate_membership` guards `cards.add_to_family`. Violations are
  `CardError` codes (`nest_*` -> 409, `bad_membership` -> 422), same over HTTP
  and MCP. The AlienWhoop `card_family_members` decision was queued by the
  one-time v2c_3 step, now `scripts/archive/v2c_owner_questions.py` (#562; never moves anything); resolving it runs
  unnest -> set_kind family -> add_to_family in one change-log batch.

- **Provenance options (#529)** — the card list (`projects.provenance`) and the
  file list (`capture_events.provenance`) are rows of `provenance_options(scope,
  key, label, sort_order, retired_at)`, seeded idempotently by `init_db` and
  managed in `/admin`. `core/provenance_options.py` owns reads, validation
  (active keys for new writes; a record's existing retired key stays valid) and
  change-logged writes. Pickers, labels and the MCP tools read the table; the
  seed is `provenance_options.SEED` (`card_rules`' label dicts are only fallbacks;
  `db.PROVENANCE_TYPES` is gone, #546).
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
  type. It only ever asks; the link exists only if the owner answers. The question does not assume the
  new file is the newer one (#586): each candidate gets "Yes, it replaces", "No, <candidate> replaces
  this one" (`reverse:<slug>`) and, for byte-identical files, "It's the same file: keep one"
  (`same:<slug>`: this upload goes to the trash via `items.delete`, 7-day undo). The suggestion follows
  identical contents, then the ` (n)` copy marker (higher n = later; no marker = older than (1)), then
  `source_modified_at`, then upload time, and `suggested_reason` names which one decided it. Not
  `capture_event_relations`: that one is symmetric and syncs tags/projects.
  **#615: a byte-identical copy under ANOTHER name** (the same photo from Imgur, Facebook, the photo
  library) is not a name candidate, so `revisions.identical_items(slug)` (same `file_size` from the DB,
  then compared byte for byte) adds it to the same question with only the `same:<slug>` answer, and the
  question reads "... is the same file as one already in the library. Keep one?" when there is no name
  match at all. So renamed dupes no longer come back in silently.
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

**A keyword search (`db.search()`, `GET /api/search`) matches the words an item itself
carries (see "Search" below), not the tag tree above it.** An item tagged only under a child
tag of a topic (`Walker studies` under `Kerbal Space Program Builds`) has no text that
names the topic, so a keyword search for the topic misses it. If you (human or agent) are
trying to answer "show me everything about X," walking the tag tree from X's tag (via
`list_posts_for_tag`/`list_tag_tree`) finds items that `db.search()`/`/api/search` will
miss. Don't assume a keyword search is a substitute for a tag-tree walk.

### Search (#543)

`db.search()` (behind `GET /api/search`, the object page's related/attach pickers, the blog
builder, `/api/gallery` and the MCP `constructicon_search`) reads one SQLite FTS5 index,
`item_search` (rowid = `capture_events.id`). The text it holds per item is defined once, by the
view `item_search_src` in `core/db.py`: `display_name` (with `short_title` folded in, #542),
`content_description` and `filename` (every name the item has had, so a rename never makes the old filename unfindable; `core/item_title.py`
picks only one of them to SHOW), the free-text `tags` column plus tag-tree tag names (`post_tags`),
the titles of the cards it is on (`project_items`), the uploader's note (`description`) and
`extracted_text`. Not indexed: `agent_notes` (an item's or its card's, #745), `type_metadata`, `provenance`, the slug.
(A YouTube description, which lives in `type_metadata`, is searchable because #724 (c) copies it into
`extracted_text`; see the `ocr_status` notes under "Data model".)

- **Matching:** each run of letters/digits you type is a word-START match, all required ("tun
  gig" finds "Tuning the Gigabyte"); accents fold; case is ignored. Quotes, `*`, `-`, `:`, brackets
  and AND/OR/NOT/NEAR are plain punctuation/words, never FTS5 syntax (`db._search_match`). It is
  NOT a substring match: "ning" does not find "Tuning". A query with no letters or digits finds
  nothing. Up to 16 words.
- **Ranking:** with a query, best match first by bm25 with column weights (name > filename > tags
  and card titles > note > OCR text), ties newest first. With no query it is newest first, as before.
- **Visibility is unchanged:** the same SQL clauses as ever (redacted, restricted types, flagged
  items via `policy.sql_browse_clause("ce.")`, brand assets, `include_superseded`), then
  `policy.filter_visible` in the callers. The index holds flagged items' text; they are filtered
  by the query, so a viewer never finds one by a word, a tag or a card title.
- **Tag filter and limit are SQL** (`json_each` over the free-text list, any-of; `LIMIT ?`).
- **Kept current by triggers**, not by each write path: item insert/update/delete, `post_tags`,
  `project_items`, a tag renamed or deleted (`blog_tags`), a card retitled or deleted (`projects`)
  all re-index exactly the items they touch. The table is contentless with `contentless_delete=1`
  (SQLite >= 3.43; the image has 3.46) so there is no second copy of the OCR text and the triggers
  never need old values. A NEW table or column that should be searchable goes into
  `item_search_src` + `_search_sync_triggers()`, and needs a rebuild migration (#542's is
  `search_short_title_542`). `_ensure_search_schema` re-creates the view and any trigger whose
  stored definition differs from the code's, so an existing DB never keeps an old one.
- **Schema vs data:** `_ensure_search_schema()` (table, view, triggers; idempotent) runs at every
  boot in both processes; the one-time backfill is the `search_index_543` migration (web process),
  which logs the item count and the time. `db.rebuild_search_index()` is the repair for any drift
  (`db.check_search_index()` reports missing/stale rows). `db.list_tables()` hides the `item_search*`
  tables (derived data, not part of `reset.CLEARED_TABLES`/`KEPT_TABLES`); "database exactly as
  before" snapshot tests skip them too (their bytes differ after an undo, their content does not).
- Check with `scripts/test_search_543.py` (throwaway DB).

## MCP server: `constructicon-mcp` (issue #68, resolved)

Constructicon **does have a live MCP server** — `constructicon-mcp` runs
as its own container alongside `constructicon-web` (and
`constructicon-test-mcp` alongside `constructicon-test`), built from
`mcp_server/server.py`, exposing 58 tools (55 after #746, plus `constructicon_undated` (#725),
`constructicon_sharing` (#604) and `constructicon_duplicates` (#615)) like `constructicon_search`, `constructicon_get`,
`constructicon_update`, `constructicon_list_projects`, `constructicon_update_project`,
`constructicon_card_files`, `constructicon_needs`, etc.
(`mcp__constructicon-mcp__*` in a session with it configured). #68 tracked
standing this up and was closed 2026-09-02 — **don't assume it's still
missing**; if a session's tools list doesn't show it, that's a
configuration gap for that session, not evidence the server itself is gone.

- #167 tracks auditing this tool surface for real gaps found in later
  work (e.g. whether `constructicon_search`'s `tags` filter shares
  `db.search()`'s row-limit-before-filter bug (fixed in #543: the filter is SQL), whether there's a tool for
  setting a project's cover/write-up) — check it before assuming a
  capability needs building from scratch.
- **The tool surface (#746, "108 tools in a trench coat"; owner decisions 2026-10-17).** 108 tools
  were folded into **55** with no capability lost (the full old -> new table is on #746 and in
  `scripts/test_mcp_consolidation_746.py`'s scenarios). The pattern:
  - **Selector tools.** Related operations share one tool with a selector: `action=` (`redact`,
    `trash`, `item_tags`, `related`, `revision`, `card_files`, `card_hobby`, `family`, `nest`, `link`,
    `queue_item`), `view=` (`needs`: queue | nudges | caption | card_decisions | pending), `which=`
    (`list_items`: tag | brand_assets | redacted | restricted), `direction=` (`convert`); `delete` picks
    its target by which of `slug` / `slugs` / `card` / `blog_entry` / `everything` is given. The selector
    is a `Literal[...]`; a parameter that doesn't apply to the chosen action is refused (`_only_for`),
    never ignored. Each action's answer is documented in the tool's description.
  - **Multi-field setters.** `constructicon_update(slug, ..., fields={...}, dry_run)` (provenance,
    content_date, highlight, is_brand_asset, brand_role, sensitive) and
    `constructicon_update_project(card | project_id, ..., fields={...}, dry_run)` (activity, force_kind,
    whereabouts(_note), provenance(_credit), highlight, home, synopsis, flavor, writeup_slug,
    owner_words) and `constructicon_update_hobby` / `constructicon_update_blog_entry(projects=, items=)`:
    key present = set it (null / "" clears), absent = leave it; unknown key = `unknown_field`. The whole
    call is ONE `db.transaction()` and ONE change-log batch (one undo). **`dry_run` is the real thing
    rolled back** (`db.transaction(dry_run=True)`), so a step sees what the earlier ones did (kind and
    stage together validate against the new kind). Agent notes are written ONLY by
    `constructicon_set_agent_notes` (`agent_notes` is refused as a `fields` key); read them with
    `constructicon_get(slug, include=["agent_notes"])` (`include` also takes "related", "revisions").
  - **Agent notes on cards (#745).** `constructicon_set_agent_notes(slug=None, notes=None, card=None)`
    takes exactly ONE target (neither or both: `bad_request`): `slug=` an item (#206, unchanged, still
    positional `(slug, notes)`), `card=` a card's id or slug (`not_found` if missing). The card path is
    `cards.set_agent_notes(card, notes)`: one imaged change-log row (op `card_agent_notes`), undoable,
    a no-op logs nothing, **`updated_at` is not bumped** (so a private note never reorders the card
    lists); its answer is the card plus `agent_notes` and `batch_id`. `constructicon_get_project` always
    returns the card's `agent_notes` (null when none). Role rows: `(set_agent_notes, "slug")` and
    `(set_agent_notes, "card")`, both editor. **Privacy is enforced at the reader:** `projects.agent_notes`
    (schema DDL in `init_db`, both processes) is stripped from every whole card row `core/db.py` hands out
    (`db._card_row`, `db.PRIVATE_CARD_COLUMNS`: `get_project`, `list_projects`, children, family members,
    hobby / item / blog entry / related / referencing lists), so a route that dumps a raw row (the card
    Save, orphan-child) can't leak it; `db.get_card_agent_notes(id)` is its one reader. Change summaries
    (`cards._changes_from_log`, which every card / hobby / blog / undo answer uses) drop private card
    columns too; the raw row images stay in the change log (MCP `list_changes`, admin `/api/audit-log`),
    as for item notes, so undo still restores them. Never indexed for search, never exported.
    Check: `scripts/test_card_agent_notes_745.py`.
  - **Undated items (#725):** `constructicon_undated(action="list" | "accept" | "apply_auto")` lists the
    items with no real date and their date guesses, accepts one guess, or applies every in-file guess as one
    batch (see "Dates" above). Added after #746's count, like `constructicon_sharing` and `constructicon_duplicates`.
  - **The whole-archive wipe** is `constructicon_delete(everything=true, confirm="DELETE EVERYTHING")`:
    both keys or nothing (either alone, or with another target, is refused); `delete`, `redact` and
    `trash` carry `destructiveHint`.
  - **`run_type_action(slug, "retry_ocr")`** is built in; no object type may declare an action keyed
    `retry_ocr` (the MCP refuses to start).
  - **Retired names are hidden, logged aliases for 2 releases** (removal is a later issue): every old
    name still answers `tools/call` with its old signature, defaults (e.g. `set_highlight()` with no
    `on` unmarks) and return shape, but `tools/list` leaves it out (`@mcp.tool(alias_of=(tool,
    action))` -> `_HIDDEN_ALIASES`, the #700 mechanism). Each call logs `MCP alias call: <old> -> <new>
    [action] (deprecated name, #746)` in the MCP container's log, so it shows who still uses old names.
    `scripts/data/mcp_aliases_golden_746.json` holds what each old name returned before #746 (on the
    test's fixture); `scripts/test_mcp_consolidation_746.py` checks every alias against it, and that
    the new call leaves the database exactly as the alias did.
  - **Conventions live in the server's startup instructions** (`SERVER_INSTRUCTIONS`: error shape,
    undo / batch ids, dry runs, visibility, text previews, dates, selectors), not in every description;
    descriptions say usage and one short example, no issue numbers or history. **No tool publishes an
    `outputSchema`** (`structured_output=False`): results are the JSON text (an empty list is `[]`).
  - **Who may run what: `ACTION_ROLES`** in `server.py`, one row per `(tool, action)` (viewer for reads,
    editor for curation, admin for anything permanent or install-wide). The wrapper checks it on every
    call (an alias uses the row of the call it stands for). The install token is admin, so it is never
    refused; a per-user MCP token (#604 step 5) runs as its user, and this table refuses what their role
    doesn't allow (403 forbidden naming the role). Startup
    refuses a visible tool or selector value with no row.
  - **Adding or changing a tool:** add its `ACTION_ROLES` row(s), classify it in
    `scripts/check_policy_doors.py` `MCP_TOOLS` (rule 4 covers EVERY MCP tool and alias: opens | items |
    admin | new | none, each verified), and update `scripts/data/mcp_tool_names_700.json`.
- **Captioning through MCP (#588, #585, #587 item 5).** On an install with no Ollama
  (`CAPTION_DISABLED=1`) an agent that can see images does the captioning:
  1. `constructicon_needs(view="caption", limit=50, include_failed=True)`: caption-capable items with no
     accepted description and a caption status that is absent (or failed/skipped), with project and
     hobby context and `total`. Redacted and restricted items never appear.
  2. `constructicon_view(slug, size="preview")`: the picture as MCP **image content** (PNG/JPEG,
     long edge 1024 px, at most about 1.5 MB; `size="thumb"` is the 400 px thumbnail) plus a text
     block (name, type, description, OCR text truncated). It uses the types' own thumbnail renderers
     (`thumbnails.render_view`), so a PDF page, an STL render or a video frame work wherever the type
     has a thumbnail; `not_viewable` when there is no picture (or the item is redacted).
  3. `constructicon_set_caption(slug, text=None, title=None, accept=False, replace=False)`
     (`items.set_caption`): stores the text as `type_metadata.auto_caption` and the short title as
     `type_metadata.auto_caption_title`, status `done`, model `mcp-agent`, in ONE imaged batch (actor
     `mcp`, undoable). `accept=False` leaves them in `/captions/review` for the owner to approve;
     `accept=True` also sets `content_description` and `short_title` (the item's name), like Accept
     there. Give a `title` with every caption: the long caption is never the title (#542). No model
     runs, so it works with captions off. **#787, never overwrite silently:** replacing a suggestion,
     description or short title that a local model didn't write (an agent's, the owner's, a hand
     edit) is refused with 409 `would_replace_suggestion` (`details.existing` holds each text that
     would go) unless `replace=true`; a moondream draft is replaced freely. Every answer carries
     `replaced_suggestion` (the old suggestion text or null) and `replaced` (every replaced text, by
     field). The review page's Accept (`POST /api/captions/{slug}/accept`, editor) runs the same op:
     on that refusal it shows the existing text and offers "Replace it" / "Keep what is there". A hand
     edit of a description or short title (`items.update` without saying who wrote it) drops its
     recorded model, so it is protected from then on.
  4. `constructicon_update(..., content_description=...)` corrects a description or caption (same
     `items.update` merge and validation as the web route).
  With captions off, the processing drawer shows the Caption stage as `off` (settled, not pending),
  with a one-line note, and Admin > Caption tuning explains it and disables nothing else. When
  captions are on, Admin's "Caption skipped items" (`POST /api/captions/queue-skipped`, admin;
  `captions.queue_skipped()`) queues every caption-capable item that never got a caption (or whose
  caption failed or was skipped) and has no description; with captions off it answers 409
  `captions_disabled`. Check with `scripts/test_captions_mcp.py` (throwaway DB).
- **Extracted text is a preview in multi-item results (#697).** One search used to return every
  match's whole `extracted_text` (224,000 characters for 5 matches on the work install). Now every tool
  that returns many items (`search`, `list_items`, `get_project` items and cover, `get(include=["related"])`,
  `related`, `card_files`, a blog entry's items) sends `_to_public_listed(row)`: `extracted_text` is a
  window of `TEXT_PREVIEW_CHARS` (300) characters around the first query word found in it (word start,
  case and accents folded, like the search; else the start), with "…" at each cut end, plus
  `extracted_text_chars` (full length) and `extracted_text_truncated`. `search`, `list_items` and
  `get_project` take `text_chars` (0 = no text, capped at `TEXT_PREVIEW_MAX_CHARS` = 5,000). Single-item
  tools (`constructicon_get`, the setters) still send the WHOLE text, uncapped (one item by design; a
  window-by-offset read for very long single items is not built). `get_project`'s write-up object is
  sent whole. Check with `scripts/test_mcp_search_cap_697.py` (throwaway DB).
- `mcp_server/server.py` still carries some stale-vocabulary rough edges
  from its imagerepo origin in places; treat naming inconsistencies as
  worth fixing opportunistically, not as evidence the server isn't real.

## MCP auth (#561, part of #467)

The MCP HTTP transport (port 8100) is guarded by one **install bearer token**
(`mcp_server/auth.py`, an ASGI middleware around the streamable-HTTP app; the server now
runs that app via uvicorn itself, same host/port).
- **Where the token lives:** it is the install token, shared with the web app (`core/install_token.py`,
  see "Auth enforcement"): `CONSTRUCTICON_INSTALL_TOKEN_FILE` (preferred; the older
  `CONSTRUCTICON_MCP_TOKEN(_FILE)` still work), one file mounted read-only into both containers.
  Create it with `python3 -m core.manage.mcp_token generate <file>` (mode 600, prints only the path,
  never the token); `... check [--url http://host:8100/mcp] [--web-url http://host:port]` reports
  whether a token is configured and probes both servers. Never put the token in git or in chat.
- **Behaviour:** token set -> every request needs `Authorization: Bearer <token>`
  (`hmac.compare_digest`), else 401 + `WWW-Authenticate: Bearer` + `{"ok":false,"error":
  {"code":"unauthorized",...}}`; only `GET /healthz` is exempt. **Token unset -> the server refuses
  to start** (#467 step 2: roles are enforced, an open MCP would be an open admin door). Token under
  32 chars, or an unreadable/empty file -> refuses to start.
- **Client config** (Claude Code `.mcp.json` / `~/.claude.json`):
  `{"type":"http","url":"http://<host>:8100/mcp","headers":{"Authorization":"Bearer <token>"}}`
- **Rotate:** `python3 -m core.manage.mcp_token generate <file> --force`, restart BOTH containers (web
  and MCP), update every client's `headers`. Old token stops working at the restart.
- **Identity:** the install token = role **admin** (owner decision 2026-10-07), actor `mcp`, so the
  agent sees restricted items (the policy's admin role). A **per-user token** (`ctu_...`, #604 step 5)
  acts as its user instead: see "Per-user MCP tokens" (after "Ownership and sensitive items").

## Install config (#562, groundwork for #467)

One install per customer, so **nothing owner-specific lives in code**. Who the install belongs to
and where it publishes is data: the `install_config` key/value table, owned by
`core/install_config.py`, edited in **Admin > Install** (`GET/POST /api/install-config`, admin role,
JSON body). No secrets there: the publish token stays a write-only API key (`app_settings`).
- **App name and logo (#581):** `app_name` (header, home title, every tab title via the `app_name()` Jinja
  global; the `DEV-` prefix is still added by `base.html`) and `app_logo` (slug of a brand-asset image,
  served at `/f/<slug>`; `app_logo_url()`). Unset = "Constructicon" and `/brand/logo.png`. `/brand` serves
  `assets/brand/` first, then `web/static/brand-fallback/` (logo, favicons, touch icon), so an install
  without the `./assets` mount still has a logo.
- **Audit reason (#583):** `web/middleware.py` fills `audit_log.error_detail` for any >= 400 response from a
  small, already-complete JSON body (`"<code>: <message>"` or a plain `detail`); streams and files are never
  read; redacted routes keep only the code.
- **Keys:** `owner_name` (home page gallery tab + its initials), `owner_label` (the "who" prefix of
  every upload's Source label, defaults to `owner_name`), `site_title` (static export title),
  `copyright_holder` (export footer "(c) <holder>.", nothing when unset), `publish_targets`
  (`{name: {repo: "owner/repo", branch}}`). Owner's installs, seeded: "Hooptie J", "Hooptie J (me)",
  "hooptiej.com", "hooptiej", test = `hooptiej/constructicon-export-test`, live =
  `hooptiej/hooptiej.github.io` (both `master`).
- **Reads** are cached per process (`CACHE_SECONDS`, cleared on write and by `cards.undo`):
  `owner_label()`, `display_owner_name()`, `owner_initials()`, `site_title()`, `copyright_holder()`,
  `publish_targets()`, `setup_needed()`. Upload labels come from `db.source_manual_upload()` /
  `db.source_automated_upload()` / `db.source_groups()` (were the `SOURCE_*` constants): on the
  owner's installs byte-identical to before ("Hooptie J (me) — manual upload").
- **Writes:** `install_config.update({key: value})` ("" clears): validated (`bad_install_key`,
  `bad_install_config`, `bad_publish_target`), one imaged change-log row (op
  `install_config_update`), undoable with the generic undo.
- **Fresh install** (empty DB): no rows. Neutral fallbacks ("Owner", "Owner — manual upload",
  site title "Constructicon", no footer holder); publishing refuses with `no_publish_target` ("Set one
  in Admin → Install"), never a fallback to someone else's repo; the admin page shows a
  dismissible "Finish setting up this install" banner until `owner_name` is set. #467's first-run
  setup (first admin account) builds on `setup_needed()`.
- **Existing installs** were seeded once by the `install_config_seed_562` migration (only when the
  DB already holds items or cards) with exactly the values the code used to hard-wire
  (`install_config.LEGACY_VALUES`; publish targets from the old `pages_publish_targets` app setting
  when it was set). Note: that includes the owner's work install, which got the owner's publish
  targets too (as before); change them there in Admin > Install.
- **Per-hobby settings** (`hobby_settings`, `core/hobbies.py` `set_physical_piece`, `POST
  /api/hobby/{id}/physical-piece`, editor, undoable): "shows physical-piece fields" puts the
  PHYSICAL PIECE group on items in that hobby's cards (`physical_piece.in_physical_piece_hobby`)
  and links the in-app capture guide (`GET /guides/capture-physical-piece`, from
  `web/guides/capture-physical-piece.md`). Was a name match on "Traditional Media"; the
  `hobby_physical_piece_562` migration switched it on for that hobby on the owner's installs.
  Toggle it on the hobby page's SETTINGS group.
- **Owner-archive migrations** live in `scripts/archive/` (`v2c_owner_questions.py`: the
  AlienWhoop family and AW canopy questions, formerly `v2c_3` / `v2c_4` in `db.MIGRATIONS`), not in
  `init_db`. A data migration about specific archive content goes there, never into `MIGRATIONS`.
- Check with `scripts/test_install_config.py` (throwaway DBs: a fresh install and a legacy one).

### The hobby label (#700, "A hobby by any other name")

An install can call a hobby something else: Admin > Install, keys `hobby_label` (singular) and
`hobby_label_plural`, both or neither (default "Hobby / Hobbies" = not set). Owner decision
2026-10-16: rename EVERYTHING the user sees, keep the plumbing.
- **Rules** (`core/hobby_label.py`, pure): 2-32 characters, letters (any alphabet), spaces and
  `- ' & .`, starting with a letter; each word's slug (`slugify`: accents dropped, lower case, `-`)
  must not be in `RESERVED_SLUGS` (every first URL segment of the app and a few likely ones;
  `test_hobby_label_700.py` checks the route table is covered); singular and plural slugs differ;
  "hobbies" only as the plural, "hobby" only as the singular. Refusals: 400 `bad_hobby_label`. A save
  is the usual imaged `install_config_update` row, undoable.
- **One place to read it:** `install_config.hobby_label()` -> `Label` (`.one` "Hobby", `.many`
  "Hobbies", `.one_lc` / `.many_lc` for inside a sentence, `.one_slug` / `.many_slug`, `.index_url`,
  `.url(slug)`). Templates: the `hobby_label` Jinja global (`{{ hobby_label.many }}`,
  `{{ hobby_label.url(h.slug) }}`, `web/common.py`). JS: `HobbyLabel.*` (`static/js/hobby-label.js`,
  loaded first by `base.html`, which puts the label on `<html data-hobby-label="...">`; no inline
  script). Python: `install_config.hobby_label()`; `card_rules.HOBBY_LABEL` still works (a module
  `__getattr__` that returns the singular). Guides are relabelled at render (`relabel_text`).
- **URLs:** the routes stay at their internal paths `/hobbies` and `/hobby/{slug}`; `web/hobby_paths.py`
  (pure ASGI, inside the security headers and origin guard, outside the access gate) rewrites the
  label's `/<plural>` and `/<singular>/<slug>` to them, and **308-redirects** `/hobbies`, `/hobby/<slug>`
  and every earlier label's paths (query kept) to the label's. Earlier labels are remembered in the
  non-imaged `hobby_label_known` row (written by `install_config.update`, never dropped, not even by an
  undo), so no link ever breaks. Links are built with the label (`hobby_label.url`, never a literal).
- **Immediate vs relaunch:** the web side follows at once (install config is cached ~2 s per process
  and cleared on save; no restart). The **MCP reads the label when its process starts**: tool names
  (`constructicon_create_hobby` -> `constructicon_create_department`, `list_hobbies` -> `list_departments`)
  and descriptions follow it (`server.tool_registrations` / `tool_name_for` / `tool_description_for`).
  The old names (the default ones and each earlier label's) stay registered as **hidden aliases**:
  `tools/call` answers them, `tools/list` leaves them out (`mcp.list_tools` is filtered by
  `_HIDDEN_ALIASES`). At startup the MCP records the label it used (`app_settings`
  `mcp_started_hobby_label`); Admin shows a persistent "Restart the MCP server" notice while it
  differs from the current label (`install_config.hobby_label_status()`), and a one-time notice after a
  save when no MCP has recorded one. The warning text (`install_config.HOBBY_LABEL_WARNING`) is on the
  Install panel, and the Save asks to confirm a label change with it.
- **Stays "hobby", on purpose:** DB tables/columns, API routes (`/api/hobby/...`, `/api/hobbies...`),
  JSON field names, MCP parameter names, `core/` identifiers and route function names, and the core
  error texts the API returns.
- **Guard:** `scripts/check_hobby_label.py` (text/AST, in the suite) fails on a hard-coded
  hobby/hobbies word a user can read in `web/templates`, `web/static/js` or `web/**/*.py` (visible text,
  title/aria-label/placeholder/alt/label, string literals that read like text, a page link written as
  `/hobby...`), and skips comments, identifiers, paths and one-token lower-case keys. Exceptions go in
  its `ALLOW` with a reason. A word that arrives as DATA (e.g. a home source "hobby") is not seen by
  it: render it through the label. Check the behaviour with `scripts/test_hobby_label_700.py`
  (throwaway DB; the MCP tool names are compared with `scripts/data/mcp_tool_names_700.json`, which a PR
  that adds or renames a tool updates).

## Web owns background work; MCP enqueues (#549)

`constructicon-web` and `constructicon-mcp` are two processes on one SQLite DB, so
anything that must happen "only once" cannot live in process-local state.

- **Captions:** only the web process captions, and every caption is persisted first (#592).
  `captions.run_caption` (called by upload, import, retype, the regenerate click and MCP alike)
  marks the item caption-pending, inserts into the `caption_queue` table and wakes web's worker
  (`captions.wake_worker()`, an Event the worker waits on instead of sleeping); it never runs the
  model itself. The model call is `_run_caption_now`, called only by the worker. So a restart just
  resumes the queue rows. Safety net: `captions.requeue_orphans()` (startup + every 5 min in
  `web/app.py`) re-queues an item left `pending` with no queue row for over 10 minutes (no-op with
  captions off, #585). Thumbnails regenerate lazily on view and embeddings ride inside OCR, which has
  its own self-heal, so captions were the only request-started work that could be orphaned. Web's `captions.start_queue_worker()`
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
- **Stale-decision sweep (#551 item 3):** web only, once at startup after the migrations, then
  hourly (`_decision_sweep_loop`, `decisions.SWEEP_INTERVAL_SECONDS`). MCP can run it on demand
  (`constructicon_sweep_stale_decisions`); no GET ever resolves a question.

## Build / test / run

### Tests and checks: `scripts/run_checks.py` is the pre-merge step (#552)

There is no pytest, no `tests/` folder and no CI config (no `.github/`). The suite is the
self-contained scripts in `scripts/` (`test_*.py`, `check_*.py`, `test_*.js`), and **one runner runs
all of them and prints a table** (script / result / passed / failed / time) and an overall `RESULT:`
line. **Run it before you open or merge a PR**, in a throwaway container with the checkout mounted
read-only and no network (nothing in it can reach a real install):

```
docker build -t constructicon:pr-NNN .        # layers are cached; the code layers come last (#688)
docker run --rm --network none -v "$PWD":/app:ro -w /app constructicon:pr-NNN python scripts/run_checks.py
docker image rm constructicon:pr-NNN          # untag it afterwards
```

(`python scripts/run_checks.py -j 2` runs two at a time; `-k text` selects by file name; `--list`
shows what would run; `--show-output` prints the whole output of anything that did not pass;
`--selftest` checks the output reader.) On the shared dev box, name any container you start
`pr<NNN>-*` or use `--rm`, and never touch `constructicon-web` / `constructicon-mcp`.
- **Exit codes are not used, on purpose.** The runner decides each script's result from what it
  prints (`PASS` / `ok` / `FAIL` lines, `FAILED: <names>`, a final "all checks passed" / "ALL PASS" /
  "OK: ..." / "0 failure(s)" / "N/M passed" line) and never reads its exit status, because several
  tests never call `sys.exit(1)` (`test_attic_546.py` prints `FAILED: <names>` and ends normally) and a
  bare code says nothing about what failed. It prints the failing check names. A script whose output
  matches nothing is reported `UNKNOWN: <its last lines>`, never as a pass; a traceback with no verdict
  after it is a FAIL ("crashed: ..."); a timeout is a FAIL. The runner itself ends with the `RESULT:`
  line and sets no exit status either. **A new test must print one of those verdict lines at the end**
  (copy `scripts/test_actor_errors.py`'s `check()` + final `FAILED: ...` / `ALL PASS`).
- **What it skips, with the reason printed:** the two **live-server** scripts, which talk to a running
  instance and write to it: `test_office.py` (give `--live-url http://host:port` to run it, against
  `constructicon-test`, never production) and `check_curator_queue.py` (it uses localhost:80, so run it
  beside the test instance: `./scripts/run_tool.sh constructicon-test scripts/check_curator_queue.py`
  from the host; the runner always skips it). It also skips the
  **JS** tests (`test_*.js`, plus `check_item_cards.py`, which drives node) when `node` is not on PATH,
  which is the case in the app image. Run those in a Node container:
  `docker run --rm --network none -v "$PWD":/app:ro -w /app node:22-alpine sh -c 'for f in scripts/test_*.js; do echo "== $f"; node "$f"; done'`
  (`check_item_cards.py` needs Python with the app's packages AND node together; neither image has
  both, so in this container route it stays SKIP: run it on a machine that has both.
  `golden_master.py` is a before/after snapshot tool, not a check, and is not collected.)
  It also skips **`check_dependency_audit.py`** (#650) whenever there is no network, which is always the
  case in the `--network none` run above: see "Dependency audit" below.
- **Pin watch (#676): `scripts/check_requirements_freeze.py`** runs in the app image (no network, so it
  is part of the `--network none` suite) and fails on any installed package that `requirements.txt`
  (plus the Dockerfile's `torch==` pin) doesn't pin, or pins at another version, on a non-`==` line, on a
  stale pin, and on a torch that isn't the `+cpu` build. Fix by pinning what the image actually has.
  `pip` is allowed unpinned (its `ALLOWED` dict): it comes from the base image, which the Dockerfile pins
  by digest (the comment above `FROM` says how to bump it; `ops/ollama-restarter/Dockerfile` pins the
  same digest, so bump both together, #696). Outside the app image (Windows, a venv) it
  prints SKIPPED. Until #668's full pin (PR #674) lands it reports the 9 unpinned py7zr/openpyxl helpers.
- **Dependency audit (#650): run it with network before a PR that touches `requirements.txt` or the
  Dockerfile, and now and then on `main`** (new advisories appear for versions that were clean):
  ```
  docker run --rm -v "$PWD":/app:ro -w /app constructicon:pr-NNN python scripts/check_dependency_audit.py
  ```
  It audits every package installed in the image (transitive ones, pip and setuptools included; torch's
  `+cpu` label is dropped) with `pip-audit -r <list> --no-deps --disable-pip`, and prints one
  `FAIL <pkg>==<version>: <advisory>, fixed in ...` line per advisory, then `FAILED: ...` or `OK: no known
  advisories in N packages`. pip-audit is **not** in the app image (a scanner and its dependencies would
  ship to production for a pre-deploy check): the script installs the pinned `PIP_AUDIT` into a temp
  directory and runs it in a subprocess, so the audited environment and the checkout are untouched.
  `--requirements FILE` audits a pinned file instead (e.g. a `pip freeze` from a running container).
  An advisory accepted on purpose goes in its `IGNORE` dict with the reason. `requirements.txt` is
  a full freeze (#668): every package in the image is pinned there (transitive ones too: PyJWT via
  `mcp`, setuptools via torch, py7zr's and openpyxl's helpers in their own marked section), except
  torch, which the Dockerfile pins and installs first. So a rebuild installs exactly the tested set.
  When you change a pin, rebuild and diff `pip freeze` against the old image: nothing new may appear
  unpinned. The audit still reads the built image, not the file, so it covers whatever is really there.
- **Environment:** for each script the runner points `CONSTRUCTICON_DB_PATH`, `CONSTRUCTICON_STORAGE_DIR`
  and `CONSTRUCTICON_EXPORTS_DIR` at fresh temp directories (unless you already set them) and sets
  `PYTHONDONTWRITEBYTECODE`, so it works from a read-only mount. The tests also call
  `_testenv.isolate()` themselves (see "paths" above).
- **A clean `main` is the baseline.** The old known failure (`test_timeline_dates.py`,
  `test_project_span_excludes_writeup`) was a stale fixture, fixed in #635: since #423 every
  `db._create_project` adds its own blank write-up, so a test that brings its own write-up passes
  `with_writeup=False`. A new red line is a real failure: fix it or file it.
- **What each one covers** (the module docstring has the detail; `--list` is the authoritative list):
  - service layer and writes: `test_items_service`, `test_membership_tags`, `test_hobbies_blog`,
    `test_drop_columns_704` (the dead-column drop and its safeguard),
    `test_move_tags_590`, `test_revisions`, `test_duplicates_615` (duplicate folding), `test_bulk_decisions`, `test_provenance_options`,
    `test_replace_file_617`, `test_card_faces`, `test_hobby_covers_686`, `test_install_config`,
    `test_hobby_label_700` and `check_hobby_label` (the renameable hobby word, #700), `check_layering`;
  - auth, roles, policy: `test_auth_step1`, `test_auth_step2`, `test_setup_race_648`, `test_session_cap_651`, `test_role_policy`,
    `test_ownership_sensitive`, `test_bulk_visibility_709`, `test_private_cards_604`, `test_mcp_user_tokens_604`,
    `test_request_guard`, `test_audit_secrets_559`, `test_mcp_auth`,
    `test_f_sandbox_610`, `test_body_cap_649`, `test_proxy_646`, `test_private_files_652`, `check_routes_roles`,
    `check_policy_doors`, `test_manage_634` (the `core/manage/` tools, their actor, the stubs);
  - errors, actors, background work, captions: `test_actor_errors`, `test_swallowed_errors`,
    `check_no_silent_except`, `test_async_blocking_550`, `test_one_brain`, `test_captions_mcp`,
    `test_restart_sidecar_643`, `check_compose_ollama` (the compose example's Ollama sidecar, #687);
  - object types and previews: `check_object_types`, `test_types_602`, `test_previews_606_607`,
    `test_email_type`, `test_traditional_media`, `test_raw_view_703` (the code raw view), `test_office` (live),
    `check_type_samples` and `test_type_samples_720` (the type sampler covers every type and sniffs right, #720);
  - pages, search, dates, UI: `test_search_543`, `test_mcp_search_cap_697` (MCP text previews), `test_item_title_542`,
    `test_captions_titles_787_743` (short titles, the set_caption guard, mid-word captions, the backfill), `test_mountain_time_544` (`.py` and
    `.js`), `test_home_paging_624` (`.py` and `.js`), `test_item_grid_553` (`.py` and `.js`), `test_home_payload`, `test_autocomplete`,
    `test_timeline_dates`, `test_timeline_drag_593` (`.py`) with `test_timeline_drag` and
    `test_timeline_drag_ui` (`.js`), `test_radio_dial_775` (`.py` and `.js`) with `test_radio_dial_ui_775` (`.js`),
    `test_gutters_accent_784_785` (even gutters, the yellow accent, its contrast and tuning slider),
    `check_item_cards`, `check_curator_queue` (live);
  - housekeeping and bundles: `test_attic_546` (dead code and CSS stay gone), `check_dead_code`
    (#546: the reference scan, **report only**, never fails: module-level functions, classes and
    UPPER_CASE constants in `core/` and `web/` that nothing else names, the ones only tests use, and
    CSS classes no template/JS/Python mentions; read its list before a cleanup PR, then grep each name
    yourself; entry points go in its `ALLOW`; its own test is `test_dead_code_546`), `check_archive_guard`
    (every retired script in `scripts/archive/` refuses to run, #633), `test_live_bugs_563`,
    `test_round2` and `test_round3a` (the work-install shakedown rounds: several small fixes each),
    `test_release` (the CalVer logic), `check_dependency_audit` (known advisories in the installed
    packages; needs network, see above), `check_image_layout` (#688: the Dockerfile bakes the code in
    after the pip layers, `.dockerignore` keeps secrets/data/.git/tests out, the example compose mounts
    no code; #696: every `FROM` in every Dockerfile in the repo is pinned by `@sha256:` digest and the
    ollama-restarter uses the app's exact base image; text only, no Docker), `check_text_bytes` (#706: no NUL or other C0 control byte besides
    tab/LF/CR in the `.css .js .html .py .md .json .txt` files under `web/`, `core/`, `mcp_server/` and
    `scripts/`; `--all` scans the whole repo, report only; its own test is `test_text_bytes_706`).
- Besides the runner, **the `constructicon-test` container** (below) is where a change is exercised
  against a copy of real data before prod.

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
- `scripts/archive/seed_example_projects.py` (retired, #552) once seeded a few example Projects
  on an empty dev DB; to fill a test box now use `scripts/seed_test_from_production.py`.

There's no linter/formatter config checked in (no `.flake8`, `pyproject.toml`
lint section, etc.) — match the surrounding file's style.

## Deployment

Runs on a shared TrueNAS box at `10.0.1.78` as a **plain `docker compose`
checkout** — this is *not* deployed via TrueNAS's "Apps"/catalog system.
There is no `docker-compose.yml` committed to this repo; the compose file
lives only on the TrueNAS box itself (outside this checkout), which is why
you won't find one here. `docker-compose.yml.example` is the committed template
(web, mcp, and the test pair).

**The image is the version (#688, 2026-10-15).** The Dockerfile `COPY`s `core/`, `web/`,
`mcp_server/` and `assets/` into `/app` (root-owned, read-only to the `app` user, precompiled), after
the pip layers so a code change rebuilds only the last few small layers. `.dockerignore` is an
allowlist: secrets, data, `.git`, worktrees, `*.bak*` / `web.bak-*` backups, tests, `scripts/` and
`node_modules` never enter the build context. The version is stamped at build time (build args
`CONSTRUCTICON_VERSION` / `_COMMIT` / `_BUILT_AT` -> `/app/core/VERSION.json` + OCI labels), and
`deploy.sh` tags each build `<repo>:<version>` next to the compose file's moving tag
(`constructicon:latest`). So:
- **Prod runs the baked image with NO code bind mounts.** A deploy builds and `up -d`s; rollback is
  `./scripts/deploy.sh --rollback <previous version>` (retag + `up -d`); `sudo docker image ls
  constructicon` lists what's on hand (the newest 5 version tags are kept).
- **Test/dev keep the fast loop with an override file.** `docker-compose.override.yml` (copy
  `docker-compose.override.yml.example`, rename its services to the checkout's) bind-mounts the
  checkout's code over the baked copy; compose merges it in automatically. "Copy files in, restart, no
  rebuild" works exactly as before. Delete the override (and `up -d`) to run the baked image the way
  prod does, e.g. as a last check before a prod deploy.
- **`scripts/` is not in the image, and not mounted (#634).** It holds tests, release and deploy tooling.
  The operational tools live in `core/manage/` and ride along in the image (`python3 -m
  core.manage.<tool>`); a maintenance script runs with `./scripts/run_tool.sh` (`--volumes-from <web>`
  plus a `./scripts:ro` mount for one run; see "Scripts: what runs where"), which works unchanged on a
  baked image. The home prod and test compose files mounted `./scripts:/app/scripts` before #634;
  removing that line is part of the owner's deploy step (then `sudo docker compose up -d`).
- `scripts/check_image_layout.py` (text only, in the suite) keeps all of this true.

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
./scripts/deploy.sh                  # prod: build the versioned image + up -d; test: restart
./scripts/deploy.sh --build          # test/dev: also rebuild the image (requirements/Dockerfile)
./scripts/deploy.sh --rollback VER   # prod: run the image built for VER again
```

**What `scripts/deploy.sh` does, in order (read from the script, #552, #688):** it first reads the
checkout's EFFECTIVE compose config (`docker-compose.yml` + `docker-compose.override.yml`) to find the
web service (`web` on the NAS, `constructicon-web` in the example), its `image:` and whether
`/app/core` is bind-mounted. Then (1) unless `--no-snapshot`, takes the pre-deploy backup described below
(a ZFS snapshot, or on a host/folder without its own dataset a db/ copy; aborts the deploy if it fails); (2) `git fetch origin --tags`; (3) `git reset --hard
origin/main` (nothing if already there); then one of two modes:
- **Baked (no code mounts: prod):** (4) `sudo docker compose build <web>` with the version build args
  from `git describe --tags --always`, tagged `<repo>:<version>` as well as the compose tag; (5) `sudo
  docker compose up -d`, which recreates web AND mcp on the new image (the image changed, so this
  always recreates; the old "cached build left the old process running" trap is gone); prunes version
  tags beyond the newest 5. `--build` is accepted and changes nothing (it always builds).
- **Mounted (an override mounts the code: test/dev):** (4) writes the gitignored `core/VERSION.json`
  into the checkout (the mount hides the baked one); with `--build`, builds the versioned image and
  `up -d`; (5) always `sudo docker compose restart` (re-reads the mounted files).
Both end by waiting 3 s, printing `docker compose ps` and the version the running web process reports.
**#741: every other compose service with a `build:` section** (`BUILD_SVCS`, e.g. the restarter) is
built too, plainly (no version args): in baked mode and mounted `--build` right after web, and in
plain mounted mode on its own followed by `up -d <those services>`. `--backup-plan` prints the list.
**#740:** it stops with a plain message (what it tried, what happened, the fix; never a traceback or
`unbound variable`) when there is no compose file in the folder above its `scripts/` (a copy run from
`/tmp`), when `docker compose config` fails (its stderr is shown), prints nothing or not-JSON, and
when no web service is found (it lists the services). Not a git checkout = it says to use `--image`.
**#736: `--image REF`** pulls a published image (falls back to a local copy if the pull fails), takes
the pre-deploy backup, tags it as the compose file's image and `up -d`s: no git, no build, so it
works on a host with only the compose file, `.env`, `secrets/` and the script. There is no registry
publishing step yet; this is the consuming half.
`--write-version-only` runs the VERSION.json step alone. `--rollback VER` (no git, no build): checks
`<repo>:VER` exists, snapshots, retags it as the compose tag, `up -d`. Migrations only go forward: if
the newer version migrated the DB in a way the old code can't read, restore the DB from the snapshot
too (owner call).
**So: every prod deploy is `./scripts/deploy.sh` (code, requirements and Dockerfile alike); on test, a
code change is `./scripts/deploy.sh` and a `requirements.txt` / Dockerfile change is
`./scripts/deploy.sh --build`; a compose-file change needs `sudo docker compose up -d` in mounted
mode. Either way, verify the running process has the change.** For a
requirements change that means the package versions in BOTH running containers, e.g. `sudo docker exec
<web or mcp container> python3 -c "import importlib.metadata as m; print(m.version('py7zr'))"`; if a
container still reports the old version, it is still running the old image: `sudo docker compose up -d`
(or `docker restart` it) and check again.

This replaces `git pull` (still stuck on the old HTTPS-with-no-creds
failure mode if anyone reverts the remote) and replaces routinely
tar-over-ssh'ing files in by hand. **Tar-over-ssh is now a fallback, not
the default** — reach for it only if `scripts/deploy.sh` itself can't run
(e.g. git credentials break again). If you do fall
back to it: `tar czf - core web mcp_server scripts assets | ssh ... 'cd
.../constructicon && tar xzf -'`, confirmed working 2026-09-03 deploying
#103/#95/#88+#90/#92+#93 this way. **Since #688 prod runs baked code**, so files
copied into prod's checkout do nothing until the image is rebuilt: follow the tar with
`sudo docker compose build web && sudo docker compose up -d` there (the image is then
unversioned unless you pass the `--build-arg CONSTRUCTICON_VERSION=...` that deploy.sh does).

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

`deploy.sh` fetches tags and takes the version from `git describe --tags --always`. **Baked (prod):**
it passes it to the image build, which writes `/app/core/VERSION.json` (`{version, commit,
deployed_at}`, deployed_at = build time) and the OCI labels (`sudo docker inspect <container>
--format '{{index .Config.Labels "org.opencontainers.image.version"}}'`), and tags the image
`<repo>:<version>`. **Mounted (test/dev):** after the reset it writes the gitignored `core/VERSION.json`
into the checkout, as before (#508). `core/version.py` reads the file (fallback `dev`) and appends
`-dev` at read time when the instance has `CONSTRUCTICON_ENV=dev` (unless the file already carries
it), so one image reports correctly wherever it runs. The version shows in the bottom-left page
badge (`base.html`), `GET /api/version` (`{version, commit, deployed_at, env}`),
the MCP `constructicon_version` tool and server info, and the static export
footer ("Built with Constructicon <version>"). `deploy.sh --write-version-only`
runs just the file step. A tar-over-ssh deploy has no version file, so the app
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
so its deploys don't snapshot (by design, don't snapshot 2.75 TB for a test). **They take no
pre-deploy backup at all (owner decision 2026-10-17):** deploy.sh skips the backup for any instance
whose web service has `CONSTRUCTICON_ENV=dev` (the dev/test marker, #310; not the code mounts, which
test can drop for a baked check), printing "Pre-deploy backup: skipped (dev/test instance: ...)".
Every other install, ZFS or not (e.g. the work install), still takes one.
- **No ZFS (#698):** on a host without ZFS, a data folder that isn't its own dataset, or no way to run
  `zfs` as root, deploy.sh copies the DB folder instead: a one-off `--network none` container of the
  app image (root only to make the folder, then the data folder's owner uid) copies every SQLite file
  with the online backup API (+ `quick_check`), other files as they are, skips `-wal`/`-shm`, into
  `<instance>/predeploy-backups/predeploy-<ts>-<commit>-v<ver>/` (0700/0600, newest 10 kept; gitignored).
  storage/ is not copied. `./scripts/deploy.sh --backup-plan` prints which route a deploy would take
  and changes nothing (reads the compose config and `df` only). The route is chosen from `df`'s
  filesystem type, not `command -v zfs` (zfs isn't on hoop's PATH on the NAS; it runs via sudo).
- **Docker and sudo (#698):** deploy.sh runs `docker` directly when the user can reach the socket (the
  `docker` group, or root), else `sudo docker` (hoop on the NAS: not in the group, so unchanged); a
  missing `docker` or Compose v2 plugin stops it with the install link. Compose stays required.
- **Restore data:** read-only copies are browsable at
  `/mnt/Storage Pool/Media/constructicon/.zfs/snapshot/<name>/` (e.g. copy the
  DB file back with the app stopped). `sudo zfs rollback` discards everything
  newer than the snapshot, so that's an owner call, never automatic.
- **Restore code:** prod: `./scripts/deploy.sh --rollback <version>` (the previous image, #688);
  test/dev (mounted code): git. The snapshot name records the commit and version that were running.
  The old `cp -r` of code dirs is retired (it only duplicated git).
- `POST /api/backup` (full DB+storage zip, retention 3) is now for manual or
  off-box exports, not a routine pre-deploy step. Its result includes
  `integrity` (#453).
- **Download + destination (#442, owner decision 2026-10-25):** `GET /api/backups` (admin) lists the
  newest 3 zips and `paths.backup_dir_status()` (folder, where it came from, writable, `problem`);
  `GET /api/backups/download[?name=]` (admin) streams the newest or a named zip (`FileResponse`,
  attachment, nosniff, no-store). **A GET never builds a backup** (none yet = 404 saying "Back up
  now"); `core/backup.backup_file` accepts only names matching `constructicon-backup-YYYYMMDD-HHMMSS.zip`
  that exist. UI: Admin > Maintenance (folder line, "Not writable" warning, list with links) and the
  Account page (`/account/password`), where non-admins see the control greyed "(admins only)" with
  the reason (#779 rule). The folder is env `CONSTRUCTICON_BACKUP_DIR` (default unchanged:
  `<storage>/../backups`), read-only in Admin. An unwritable backup folder still stops startup (#699
  check, unchanged); the Admin warning covers a folder that goes bad later. Check:
  `scripts/test_plumbing_batch1.py`.
- The fallback tar-over-ssh path doesn't snapshot: run
  `sudo zfs snapshot "Storage Pool/Media/constructicon@predeploy-<ts>-manual"`
  first.

**The SQLite DB lives in its own directory, never a bare file mount (#453, 2026-09-30).** Both services mount `Media/<instance>/db/` at `/app/data/` with `CONSTRUCTICON_DB_PATH=/app/data/imagerepo.db`. WAL mode keeps `-wal`/`-shm` beside the DB, and the old single-file mount gave web and mcp private copies, which corrupted constructicon-test's DB. Read DB state with a plain `sqlite3.connect(db.DB_PATH)` inside the app container, not a `?mode=ro` URI (that can miss the WAL and show stale rows). Every `/api/backup` result now includes `integrity`.

**Captions: guarded, not per-image-restarted (#454, 2026-09-30).** The old per-image Ollama restart cycled the GPU container ~180 times during a bulk seed and hard-crashed the whole NAS. Captions are back ON everywhere with the #454 safety valve (restart only on failure, 10-min cooldown, circuit breaker; status in `GET /api/captions/defaults` → `breaker` and `restart_valve`, reset via `POST /api/captions/reset-breaker`). All four services join `ollama_default` (the separate `~/ollama` project's network) today; once the home install moves to the compose-owned sidecar (#687) only the two web services join the internal `captions` network and the MCPs join neither. **No app container mounts `/var/run/docker.sock` (#643):** web asks the `ollama-restarter` sidecar (see `core/captions.py` above), which is the only holder of the socket; MCP has neither (#458), so only web can trigger safety-valve restarts. After a reboot, check `ollama` is actually running: it can fail to start before the NVIDIA driver loads (see #454).

**The app runs as a non-root user (#643):** UID/GID 1000 (`app` in the image, Dockerfile `APP_UID`/`APP_GID`), which is `hoop` on the NAS. Every read-write bind mount (storage, db, backups, exports, import) must be owned by 1000:1000 on the host; files a root container wrote before #643 need a one-time `sudo chown -R 1000:1000 <dir>`. A `Permission denied` on upload, thumbnail, backup or export after a deploy means a directory missed that chown. `docker exec <web> id` should print `uid=1000(app)`. **Since #699 both processes check at startup**
(`core/paths.unwritable_data_dirs`: the DB folder and file/-wal/-shm, storage, backups, exports; a real
create-and-delete test, a missing folder judged by its nearest parent; import/ is never written, so not
checked) and refuse to start with the path, its owner and mode, the uid they run as and the chown fix
(`paths.data_dirs_error`): web raises from its startup hook (uvicorn exits 3), the MCP `sys.exit`s (1).
Check with `scripts/test_data_dirs_699.py`. README "Install on a new host" is the non-NAS guide.
**#736: secret files too.** `paths.secret_file_problems()` checks every set `*_FILE` secret variable
(`CONSTRUCTICON_INSTALL_TOKEN_FILE`, `CONSTRUCTICON_MCP_TOKEN_FILE`, `CAPTION_RESTART_TOKEN_FILE`) is a
readable, non-empty file for this uid: missing, a directory (what Docker makes when a single-file
mount's host file is missing: root-owned and empty), empty or unreadable, each with the fix. A broken
install token stops web and MCP; the optional restart secret only prints "... is OFF ... to turn it
on". The compose example now mounts the whole `./secrets` folder into web (an absent optional secret
is just absent) and uses `create_host_path: false` for the single-file mounts (MCP token, restarter),
so `up` fails instead of making a directory. Host-specific values (URL, docker socket gid, backup
folder) come from `.env` with defaults listed in the example's header.

**Data files are private to the app user (#652).** Target on the host: `storage/`, `db/`, `backups/`, `exports/` are directories `0750` owned by the app uid (1000:1000), the files in them `0640`, backup zips `0600`. The app keeps new files that way itself: `core/paths.restrict_new_files()` sets the process umask to `027` when `web/app.py` and `mcp_server/server.py` load (it covers every thread and subprocess; `CONSTRUCTICON_UMASK`, octal, overrides it, a bad value falls back to 027), and `core/backup.py` writes each zip `0600` whatever the umask. SQLite gives `-wal`/`-shm` the DB file's own mode, so an existing DB stays as loose as it was until fixed by hand. **Existing installs:** `bash ops/fix-data-permissions.sh DATA_ROOT` prints what it would change (dry run), `--apply` changes it (idempotent; never chowns, lists entries not owned by uid 1000). It leaves `DATA_ROOT` itself and `import/` alone: the import inbox is the owner's drop folder, mounted read-only into the MCP and never written by the app. **Check before applying on the NAS:** the `Media` SMB share (`/mnt/Storage Pool/Media`, which holds both instances' data) is `guestok` with guest = `nobody`, so today any guest on the LAN can read the DB and the backups over SMB; after the fix a guest can't open `storage/` / `db/` / `backups/` / `exports/` at all, and only an SMB session authenticated as `hoop` (uid 1000) can. Check with `scripts/test_private_files_652.py` (throwaway DB, Linux).

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
   **From a Windows checkout, ship the branch's committed files, not the
   working copy, and turn off CRLF conversion (#654):** the working files
   are CRLF (`core.autocrlf=true`), and so is a plain `git archive`. Use
   `git -c core.autocrlf=false archive HEAD core web | ssh -i
   ~/.ssh/id_ed25519_truenas hoop@10.0.1.78 'cd "<target-dir>" && tar xf -'`.
   Then check `git diff --stat` on the box: it should show only your real
   change. A whole-file rewrite means the endings came across as CRLF
   (seen 2026-10-12 on #642: 1838 lines changed for an 8-line fix).
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
   find /app/core -name __pycache__ -exec rm -rf {} +`. (Since #643 the app runs
   as UID 1000, so new ones are hoop-owned; leftovers from before still need this,
   via `sudo docker exec -u 0 ...` since the container's user can't remove them.)
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
   for every registered type except `svg` (as of 2026-10-12 it has four real SVGs; its
   `archive` uploads are all `.zip`, no `.7z` or tar) — if a type has zero real
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
- **This recipe needs the code mounts (#688).** The image carries baked code; on
  constructicon-test, `docker-compose.override.yml` bind-mounts the checkout's `core/`, `web/`,
  `mcp_server/` and `assets/` over it, which is what makes "copy in, restart, no rebuild" work. If
  a copied-in change doesn't show up after a restart, check the override is still there (`sudo
  docker compose config | grep /app/core`). An ordinary code deploy on test is
  `./scripts/deploy.sh` (reset + VERSION.json + restart); `requirements.txt` / Dockerfile changes need
  `--build`. Prod has no override and always rebuilds (see "Deploy with `scripts/deploy.sh`").
  To check a branch's BAKED image before prod, build it under a throwaway name and run it with no
  code mounts (never retag an image a running container uses):
  `sudo docker build -t constructicon-prNNN:check --build-arg CONSTRUCTICON_VERSION=prNNN .` then
  `sudo docker run --rm --network none ... constructicon-prNNN:check`.

Typical redeploy from a checkout on the TrueNAS box (the same commands as "Deploy with
`scripts/deploy.sh`" above):

```
./scripts/deploy.sh            # prod: always (build + up -d); test: code only
./scripts/deploy.sh --build    # test: requirements.txt / Dockerfile changed
```

**Mounted mode: `docker compose up -d --build` can report success without actually
restarting the containers** (confirmed 2026-09-07, deploying #197/#198, before #688; deploy.sh's
mounted mode now always restarts after it, and baked mode's new image always recreates):
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
  the `core/object_types/` registry, not validated at the DB layer.
- **`description` vs. `content_description`, `timestamp` vs.
  `content_date`.** Easy to grab the wrong one of each pair — they mean
  different things (uploader metadata vs. the content's own
  description/date). See the `capture_events` notes above.
- **An item can be undated (#725).** `timeline.resolve_item_date` returns None when nothing real
  exists, and `resolve_project_span` returns (None, None) for a card with no real dates: sort with
  `timeline.date_sort_key`, take min/max over `timeline.real_dates(...)`, and show
  `item_dates.item_date(row)["label"]`. Never fall back to `timestamp` or `created_at`.
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
- **The imagerepo ticketing vestiges are gone (#704).** `client`, `ticket_id`, `source`,
  `artifact_link`, the `icon` override and the client tables were dropped; don't bring them back.
  `tech` stays (it is the Source label).
- **`mcp_server/server.py` IS live** (`constructicon-mcp` / `constructicon-test-mcp`
  containers — see the MCP section above). If a session can't see
  `mcp__constructicon-mcp__*` tools, that's a session configuration gap,
  not evidence the server is missing.
- **`GET /api/settings` never returns real secret values, only presence.**
  Scripts needing the actual value of a stored setting (e.g. the YouTube
  API key) must call `core.db.get_setting(...)` directly from a process
  that shares the DB (e.g. via `docker exec` into the app container), not
  through the HTTP API.
- **Two parallel local checkouts may exist on the owner's Windows
  machine** (`Constructicon/` and `constructicon-work/`, on different
  branches) — both are this same repo, not separate projects. If you're
  working across machines/sessions and something looks like uncommitted
  work-in-progress, check which checkout/branch you're actually in before
  assuming it's stale or foreign.
