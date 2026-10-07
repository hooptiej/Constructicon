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

It started as a single-owner tool on a LAN-only server with no port forward, where the network
perimeter was the security boundary. Auth is on (#467; owner decisions 2026-10-07): **step 1**
added users, sign-in and first-run setup; **step 2 enforces it**: every page and API needs at least
a viewer, roles are checked, restricted items are admin-only, and scripts / the desktop uploader /
the MCP send the install token. See "Users and sessions" and "Auth enforcement" below.

Long-term goal (see `README.md` for the full writeup): this is the dynamic
backend for a future blog-driven personal site. A future static-export step
will freeze content out of here and publish it to GitHub Pages (the owner's
`hooptiej/hooptiej.github.io`; the targets are per-install config since #562, see
"Install config" below).

## Architecture at a glance

- **`web/`** — FastAPI/Starlette app, split into routers (#547). The ASGI
  entry point is still `web.app:app`.
  - `web/app.py` is only the assembly point: `app = FastAPI()`, the error
    handlers (one shape, see "Actor and errors" below), the static mounts
    (`/static`, `/brand`, `/preview`), middleware (request guard outermost,
    then the actor context, then audit logging), the startup hook (`init_db`
    migrations, the stale-decision sweep, OCR self-heal + watchdog, caption queue
    worker, the trash purge and decision-sweep loops; runs as the `system` actor) and the
    `include_router` calls.
  - `web/routes/` holds one `RoleRouter` (an `APIRouter` whose routes all carry a role label,
    see "Roles and policy" below) per area, **no prefix** (each
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
- **`core/paths.py`** (#578, #576) — every on-disk location (storage, `<storage>/.trash`,
  `<storage>/../backups`, `exports/` with its `current` and `.publish`) comes from here and is
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
  web app over the same `/api/upload`/`/api/content` HTTP API. It sends the
  install token (`Authorization: Bearer`, menu "Set Install Token…", #467 step 2);
  its `X-Constructicon-Client: desktop-app` header only picks the upload's Source
  label and grants nothing.
- **`mcp_server/server.py`** — the live `constructicon-mcp` sidecar (see
  "MCP server: `constructicon-mcp`" below for the tool surface and how it
  runs alongside `constructicon-web`). Tool names are `constructicon_*`;
  the old imagerepo vocabulary is gone from here.
- **`scripts/`** — one-off/maintenance scripts (YouTube channel sync,
  backfill from the old static site, project-grouping fixups). These talk
  to a running instance over HTTP (`--base-url`), the same discipline the
  app's own UI would use, rather than writing to the DB directly — see
  individual script docstrings for the reasoning and any exceptions.
  Since #467 step 2 each one calls `_http.install(<base url>)` (`scripts/_http.py`) so its
  requests carry the install token; see "Auth enforcement".
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
- HTTP: `web/middleware.py`'s `ActorMiddleware` sets `anonymous` per request; a signed-in
  request is overridden to `user:<username>` by `web/auth.py`'s `SessionMiddleware` (#467 step 1),
  an install-token request to `token` by its `AccessMiddleware` (#467 step 2). The request-log rows
  in `audit_log` record it too. `owner-ui` is the pre-step-2 anonymous actor: old rows and
  in-process tests still carry it, no request does.
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
  row touched at most every 10 minutes (and the cookie re-sent then). Logout deletes the row.
- Sign-in backoff (`users.limiter`, in memory, web process): after 5 failures per username or per IP,
  each attempt must wait 2^(extra failures) s (max 15 min) after the last failure, else 429
  `too_many_attempts` (`details.retry_after`) without checking the password; a success clears that
  username's and that IP's counts (behind a Docker bridge or NAT every client can share one IP, so
  the IP count must not outlive a good sign-in). A wrong user, wrong
  password and disabled account all answer the same 401 `invalid_login` (an unknown user still
  spends a hash's time).
- `delete_everything` keeps `users` and `sessions` (`reset.KEPT_TABLES`).

**How the actor is set (`web/auth.py`).** Middleware order: origin guard -> `ActorMiddleware`
(anonymous) -> `SessionMiddleware` -> `AccessMiddleware` (install token + sign-in gate, step 2) ->
audit logger -> `CsrfMiddleware` -> routes. A request with the
`constructicon_session` cookie that resolves to a live session runs as actor `user:<username>`
with `users.current_user()` set (pages' `current_user()` Jinja global; `roles.role_of` reads it).
No cookie, or a dead one = `anonymous`. The cookie: HttpOnly, SameSite=Lax, Path=/,
Max-Age 30 days, `Secure` only when the request is HTTPS (deferred). Change-log rows AND
request-log rows record `user:<name>`.

**CSRF.** On top of the origin guard (#558): a POST/PUT/PATCH/DELETE **that carries a live session
cookie** must send that session's token in `X-CSRF-Token`, else 403 `csrf_failed` (in the request
log). Install-token requests (scripts, the desktop uploader; `Authorization: Bearer`) never need it:
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
or "Sign in". **No secrets in logs:** `request_guard.AUDIT_ROUTE_RULES` / `AUDIT_ROUTE_PATTERNS`
log no body values for login, setup, my password, user create and the admin reset; error reasons on
those keep only the code.

**First run.** While `users` is empty, every page (and `/login`) redirects to `/setup` (step 2),
which creates the first admin, signs them in and fills `install_config.owner_name` if unset (that
fill is logged as actor `anonymous`: nobody is signed in yet). `/setup` is 404 once any user exists.
`/admin` (reachable with the install token before then) shows "Create the admin account".

**Reset script (on the box).** `sudo docker exec -it <web container> python3 scripts/reset_password.py
<username>` (prompts twice), `... <username> --generate` (prints a generated password once, no -it
needed), `--enable` (re-enable), `--create-admin <username> [--display-name N]` (creates an admin, or
makes an existing user an enabled admin with a new password: recovery for an install with no usable
admin), `--list`. Runs as actor `script`; never prints a hash.

Check with `scripts/test_auth_step1.py` (throwaway DB).

## Auth enforcement (#467 step 2: login everywhere, roles, the restricted lock, the install token)

Owner decisions 2026-10-07. **Login scope is everything**, roles are enforced, restricted items are
admin-only, and the one install token is how every non-browser client gets in (as admin).

**What's open without login (role public):** `/healthz`, `/login`, `/logout` (the page), `/setup`
(only while there are no users, 404 after), `/api/auth/login|logout|setup|me`, `/static`, `/brand`,
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
  or `X-Forwarded-For`. The old `X-Constructicon-Client: desktop-app` header grants nothing (it only
  picks the upload's Source label).

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
  `CONSTRUCTICON_TOKEN_FILE`, else the install's own variables, so `docker exec <web container>
  python3 scripts/x.py` just works. `golden_master.py --anonymous` snapshots what a stranger sees.
- **In-process tests** (`scripts/test_*.py`): `_testenv.isolate()` configures a fresh random token
  per run (dropping the container's token variables); `_testenv.client(app)` is a TestClient that
  sends it (admin, actor `token`); a bare `TestClient` is anonymous. `_testenv.use_token()` for a
  test that builds its own temp DB.
- **Desktop uploader:** menu "Set Install Token…" (stored in its config.json, written mode 600, never
  shown back in full); every upload sends the Bearer header; a 401 shows "The server needs the
  install token: set it in the menu, Set Install Token...".

**Rollout for an install (order matters; prod is the main session's job).**
1. Deploy the code (`./scripts/deploy.sh`). Until step 3, the MCP container keeps running its old
   process; a restart without a token makes it refuse to start.
2. On the box, in the checkout: `mkdir -p secrets && chmod 700 secrets && python3
   scripts/mcp_token.py generate secrets/constructicon_token` (prints only the path, mode 600).
3. Compose, BOTH services: add the volume `./secrets/constructicon_token:/run/secrets/constructicon_token:ro`
   and the env `CONSTRUCTICON_INSTALL_TOKEN_FILE=/run/secrets/constructicon_token`; `sudo docker
   compose up -d` (recreates both; a plain restart doesn't pick up compose changes).
4. Check: `sudo docker exec <web> python3 scripts/mcp_token.py check --web-url http://localhost:<port>`
   (expects 401 without, 200 with the token) and the MCP log line "bearer token auth ENABLED".
5. First admin: open the site, it redirects to `/setup` (or `docker exec <web> python3
   scripts/reset_password.py --create-admin <name> --generate`).
6. Clients: every MCP client's config gets the `headers` block; the uploader gets the token; scripts
   run elsewhere get `CONSTRUCTICON_TOKEN_FILE`.
**What breaks if a client isn't updated:** an MCP client without the header gets 401 on every call
(Claude Code shows the server as failed); the uploader's uploads fail with the "install token"
notification; an HTTP script gets 401 (or a 302 to `/login` on a page); a hotlink to a restricted or
redacted item 404s; an open browser tab is sent to `/login`.

**Rotate:** `python3 scripts/mcp_token.py generate secrets/constructicon_token --force`, restart BOTH
containers (`sudo docker restart <web> <mcp>`), update every client. **Lost admin password:**
`sudo docker exec -it <web> python3 scripts/reset_password.py <user>` (or `--create-admin`), see above.

Check with `scripts/test_auth_step2.py` (throwaway DB: the whole matrix per role and for the token,
mounts, `next`, the decision queue, the MCP refusing to start, the uploader's API module, no secrets
in the audit log). Later (not built): HTTPS (Caddy), per-user MCP tokens, a separate uploader token.

## Service layer (#541): one core module per domain

**The rule: all writes go through `core/{items,membership,tags,cards,hobbies,blog,decisions,reset}.py`
(plus `core/revisions.py` for revision chains, `core/install_config.py` and `core/users.py`); the raw writers in `core/db.py` are private
(`_`-prefixed); `scripts/check_layering.py` enforces it.** Run `python scripts/check_layering.py`
(no server, no DB; `--list` prints every db.py writer and its class) before every PR that touches
`core/`, `web/` or `mcp_server/`. It fails when:
- a module outside the service modules calls (or imports) a private `db._*` function. The raw-write
  allowlist, each with its reason in the script: `core/captions.py` (caption status/results),
  `core/embedded_metadata.py` and `core/object_types/youtube.py` (upload-time file metadata, a
  YouTube publish date), `core/automatch.py` (upload-time automatch tagging), `core/ocr.py` (OCR
  client tags), `core/card_migration.py` (migrations), the one-off scripts
  `scripts/{apply_project_groupings,seed_example_projects,backfill_from_hooptiej_site,link_lil_dragon_brand}.py`,
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
  `constructicon_convert_hobby_to_card`, `constructicon_unmark_hobby`.
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
  purged; redacts are held until the owner clicks.** `items.update(slug, **fields)` covers display name/icon, description, client,
  content_description, type_metadata (merged, physical-piece keys cleaned), file provenance,
  highlight, brand asset/role, display-date override and content_date as ONE batch per call (one
  Save = one undo). `redact` / `recover_redacted` / `delete_redacted_file` / `unredact`, `retype` (the media_type change is imaged; the caller's
  runner re-runs OCR/thumbnail/caption; undoing a retype re-runs them for the old type) and
  `delete`. Their raw writers (`db._rename_object`, `_update_content_metadata`, `_set_type_metadata`,
  `_set_provenance`, `_set_highlight`, `_set_brand_asset`, `_set_display_date_override`,
  `_set_content_date`, `_set_media_type`, `_mark_redacted`, `_unmark_redacted`, `_delete_upload`) are
  private since phase D. The MCP's agent notes (#206) go through `items.update(agent_notes=...)`. Deliberate raw exceptions: pipeline bookkeeping (caption status/results in
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
  phase C) MCP `constructicon_add_to_project` / `add_items_to_project` pass. `NO_EFFECTS`: MCP
  `set_project_writeup`. Removing never untags and never touches the cover, on any path. The card
  reshaping ops (copy / move / split / merge / delete) call `membership.write(...)` / `write_items(log,
  ...)`: rows only, logged under their own op (`db.write_card_items` is gone). Don't call
  `db.add_item_to_project` / `remove_item_from_project` from a route or tool.
- **Tags (phase C): `core/tags.py`.** `create(name, parent_name)` (MCP create_tag), `attach(slug,
  names)` / `detach(slug, tag_id)` (MCP; `post_tags` only, as before), `set_item_tags` and
  `merge_item_tags(slugs, names)` (bulk attach-tags: free-text column + its `post_tags` sync, one
  batch). The item Save passes `tags=` to `items.update`, so tags + fields are still ONE change-log
  row. Creating a tag is imaged (`blog_tags` insert via `ImageLog.insert_auto`), so undo removes a
  tag the op created; `db.invert_image` refuses that (undo_conflict) while anything made later uses
  the tag. Lookups (`tags.find`, `find_root`, `find_any`) never create. The two tag stores (free-text column vs
  `post_tags`) stay as they were; unifying them is #555. Card creation and hobby creation mint their
  tags through `tags.ensure` too (phase D); the only raw tag writers left are the upload-time
  automatch tagging and OCR client tags (pipeline, allowlisted).
- **Delete-all (phase C): `core/reset.py` `delete_everything(confirm)`**, called by `POST
  /api/delete-all` and MCP `constructicon_delete_all(confirm)`; both need the phrase `DELETE
  EVERYTHING`. Permanent by design (no trash, no undo); one change-log row (op `delete_all`, actor
  from context) with per-table counts in `form_body`. Clears `reset.CLEARED_TABLES` (items and
  everything pointing at them, cards, tags and hobbies, blog entries, questions and their snoozes,
  caption queue, trash) plus every item file and thumbnail and `<storage>/.trash`; keeps
  `reset.KEPT_TABLES` (settings, clients, provenance options, migrations, the audit/change log). A
  new table must be added to one of the two lists (`scripts/test_membership_tags.py` fails otherwise).
  Check phase C with `scripts/test_membership_tags.py` (throwaway DB).
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
- Hobby face text box: its synopsis, then "Projects: A, B, C, +N more." and "Every card in this hobby
  carries its group code, CODE.".

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
  (omit a form field to leave it, send it blank to clear). MCP: `constructicon_set_card_text(card=|hobby=,
  synopsis, flavor, clear_synopsis, clear_flavor, dry_run)`. Reads: `cards.text_fields(card)` (in
  `explain_card`, MCP `constructicon_get_project`: synopsis, flavor, writeup_lead, face_text,
  face_text_source). UI: the ABOUT group of the details panel on the project and hobby pages
  (`templates/_card_text.html`).
- **Every write-up gets a synopsis** (owner, 2026-10-06). An agent that writes or rewrites a card's
  write-up (`constructicon_set_project_writeup`, or an `update` of its body) also sets that card's
  synopsis with `constructicon_set_card_text`: a few plain sentences saying what the card is.
- Item grids embed `stacked` (the title of the card a file is on, `db.first_card_titles()`, one query per
  grid) in the slim payload (`core/card_payload.py`); `provenance` left it. Check with
  `scripts/test_card_faces.py` (throwaway DB), `check_item_cards.py`, `test_home_payload.py`.
- Not done (no data for it): the mockup's "From: <item>, <date>." line on a file derived from another
  file (there is no derived-from relation), and image dimensions in a file's stat box (not stored).

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
    what added this row, and how"). See `source_manual_upload()` /
    `source_automated_upload()` (the owner label is install config, #562), `SOURCE_AUTHORED`,
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
  project to a root-level `blog_tags` row of the same name, so tagging into
  a project also surfaces it via ordinary tag browsing.
- **`project_items`** — many-to-many join, `(project_id, post_slug,
  sort_order)`, with a manual `sort_order` for deliberate (non-chronological)
  ordering within a project.
- **`clients`, `client_domains`** — vestigial imagerepo IT-client list
  (Hudu-synced company names, used for OCR auto-tagging). Not part of
  Constructicon's actual personal-gallery use case; present because it
  rode along with the fork. The three 'special' rows are no longer seeded at boot (#562).
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
  `capture_events` slug — `decisions.stale_reason()` branches on that prefix
  (validating against `projects`), because the file-row stale check would
  otherwise call every card question stale. Since #551 item 3 no read
  resolves anything: stale questions are left out of `list_open()` and
  resolved only by the explicit, logged `decisions.sweep_stale()` (see
  "Service layer").
- **Card kind + status (V2, piece 1)** — `projects.kind` plus
  `activity`/`stage`/`stop_reason` are the *live* status; legacy
  `projects.status` is **frozen** (the static export still filters on it).
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
  type. It only ever asks; the link exists only if the owner answers. The question does not assume the
  new file is the newer one (#586): each candidate gets "Yes, it replaces", "No, <candidate> replaces
  this one" (`reverse:<slug>`) and, for byte-identical files, "It's the same file: keep one"
  (`same:<slug>`: this upload goes to the trash via `items.delete`, 7-day undo). The suggestion follows
  identical contents, then the ` (n)` copy marker (higher n = later; no marker = older than (1)), then
  `source_modified_at`, then upload time, and `suggested_reason` names which one decided it. Not
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
- **Captioning through MCP (#588, #585, #587 item 5).** On an install with no Ollama
  (`CAPTION_DISABLED=1`) an agent that can see images does the captioning:
  1. `constructicon_list_needs_caption(limit=50, include_failed=True)`: caption-capable items with no
     accepted description and a caption status that is absent (or failed/skipped), with project and
     hobby context and `total`. Redacted and restricted items never appear.
  2. `constructicon_view(slug, size="preview")`: the picture as MCP **image content** (PNG/JPEG,
     long edge 1024 px, at most about 1.5 MB; `size="thumb"` is the 400 px thumbnail) plus a text
     block (name, type, description, OCR text truncated). It uses the types' own thumbnail renderers
     (`thumbnails.render_view`), so a PDF page, an STL render or a video frame work wherever the type
     has a thumbnail; `not_viewable` when there is no picture (or the item is redacted).
  3. `constructicon_set_caption(slug, text, accept=False)` (`items.set_caption`): stores the text as
     `type_metadata.auto_caption`, status `done`, model `mcp-agent`, in ONE imaged batch (actor
     `mcp`, undoable). `accept=False` leaves it in `/captions/review` for the owner to approve;
     `accept=True` also sets `content_description`, like "Use this caption" there. No model runs, so
     it works with captions off.
  4. `constructicon_update(..., content_description=...)` corrects a description or caption (same
     `items.update` merge and validation as the web route).
  With captions off, the processing drawer shows the Caption stage as `off` (settled, not pending),
  with a one-line note, and Admin > Caption tuning explains it and disables nothing else. When
  captions are on, Admin's "Caption skipped items" (`POST /api/captions/queue-skipped`, admin;
  `captions.queue_skipped()`) queues every caption-capable item that never got a caption (or whose
  caption failed or was skipped) and has no description; with captions off it answers 409
  `captions_disabled`. Check with `scripts/test_captions_mcp.py` (throwaway DB).
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
  Create it with `python scripts/mcp_token.py generate <file>` (mode 600, prints only the path,
  never the token); `... check [--url http://host:8100/mcp] [--web-url http://host:port]` reports
  whether a token is configured and probes both servers. Never put the token in git or in chat.
- **Behaviour:** token set -> every request needs `Authorization: Bearer <token>`
  (`hmac.compare_digest`), else 401 + `WWW-Authenticate: Bearer` + `{"ok":false,"error":
  {"code":"unauthorized",...}}`; only `GET /healthz` is exempt. **Token unset -> the server refuses
  to start** (#467 step 2: roles are enforced, an open MCP would be an open admin door). Token under
  32 chars, or an unreadable/empty file -> refuses to start.
- **Client config** (Claude Code `.mcp.json` / `~/.claude.json`):
  `{"type":"http","url":"http://<host>:8100/mcp","headers":{"Authorization":"Bearer <token>"}}`
- **Rotate:** `python scripts/mcp_token.py generate <file> --force`, restart BOTH containers (web
  and MCP), update every client's `headers`. Old token stops working at the restart.
- **Identity:** one install token = one identity: role **admin** (owner decision 2026-10-07), actor
  `mcp`, so the agent sees restricted items (the policy's admin role). Per-user MCP tokens are a
  later step (hook comment in `auth.py`).

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
- **IT-client seed:** the "Unknown" / "Not Business" / "Internal Infrastructure" rows are no
  longer inserted on every boot (nothing reads them; only `/api/clients` lists them, and no page
  calls it). `sync_clients.py` still seeds them.
- Check with `scripts/test_install_config.py` (throwaway DBs: a fresh install and a legacy one).

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
