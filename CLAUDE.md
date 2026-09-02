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

- **`web/app.py`** — FastAPI/Starlette app (`@app.get`/`@app.post`
  decorators, `HTTPException`, `JSONResponse`/`HTMLResponse`). All HTTP
  routes live in this one file (~1100 lines): page routes
  (`/`, `/object/{slug}`, `/project/{slug}`, `/gallery/user/{uploader}`,
  `/account`) render Jinja2 templates from `web/templates/`; `/api/*`
  routes are the JSON/form API the templates' JS calls; `/f/{slug}` and
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
- **`core/backup.py`** — standalone `POST /api/backup` backup-to-zip
  (DB snapshot + `storage/`). Deliberately **not** wired into any delete
  path (a past incident wiped storage while only the DB got backed up).
- **`desktop_app/`** — a separate desktop uploader app (own README), built
  and distributed as a downloadable zip from `/downloads/...`. Talks to the
  web app over the same `/api/upload`/`/api/content` HTTP API, identified
  server-side via the `X-Imagerepo-Client: desktop-app` header (not a
  client-supplied identity — this is still single-owner).
- **`mcp_server/server.py`** — **not currently deployed** (see "Known gap:
  no MCP server" below). Still carries the old imagerepo naming/tool
  vocabulary (`ccc-imagerepo-mcp`, `imagerepo_*` tool names, `client`/
  `ticket_id` params) — treat it as a stale reference implementation to
  update, not a working sidecar to assume is running.
- **`scripts/`** — one-off/maintenance scripts (YouTube channel sync,
  backfill from the old static site, project-grouping fixups). These talk
  to a running instance over HTTP (`--base-url`), the same discipline the
  app's own UI would use, rather than writing to the DB directly — see
  individual script docstrings for the reasoning and any exceptions.

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
    fit a generic column (e.g. YouTube view/like/comment counts). One
    shared column so a new object type never needs a schema migration; see
    `object_types.py`'s `MetadataField` for the documented shape per type.
  - `extracted_text`, `perceptual_hash`, `embedding`, `ocr_status` —
    OCR/similarity pipeline state.
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

## Known gap: no MCP server (issue #68)

Constructicon currently has **no MCP server wired up** — no `/mcp` route
on the web app, no `constructicon-mcp` sidecar container on TrueNAS. This
is a real, tracked gap, not something to silently fix as a drive-by: see
https://github.com/hooptiej/Constructicon/issues/68.

- The sibling app `imagerepo` (this app's fork origin) *does* run one —
  `imagerepo-mcp`, a separate container alongside `imagerepo-web`.
- `mcp_server/server.py` exists in this repo (and in the deployed image,
  at `/app/mcp_server/`) with a working set of `imagerepo_*` tools, but
  nothing runs it as a service — it was apparently dropped when
  Constructicon split off from imagerepo and never re-wired.
- If asked to stand this up, treat `mcp_server/server.py` as a stale
  starting point that still needs renaming/updating to Constructicon's
  actual vocabulary (no more `client`/`ticket_id` as meaningful concepts,
  tag tree instead of flat tags, etc.) — not a finished, ready-to-deploy
  file.

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
  https://download.pytorch.org/whl/cpu`) since the TrueNAS box has no GPU —
  do the same locally if disk space for CUDA wheels is a concern.
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

Two containers run side by side on that box:

- **`constructicon-web`** — the real production instance.
- **`constructicon-test`** — an isolated instance with its own DB/storage,
  used to test changes (e.g. new sync scripts, schema-affecting work)
  before pointing them at production. `scripts/full_youtube_channel_sync.py`'s
  own docstring is explicit about this discipline: its issue's
  implementation work was scoped to "testing against the isolated
  constructicon-test container," with running against the real production
  instance requiring the owner's separate explicit go-ahead. Follow the
  same discipline for any script that writes data or hits a real external
  API (YouTube, etc.) — default to testing against `constructicon-test`
  first, never assume production is the right target.
  **Checkout may be ahead of `main`, deliberately**: `constructicon-test`'s
  bind-mounted checkout can be left on a feature branch between sessions
  when that branch is the one the *next* piece of work builds on. As of
  2026-09-02, it's on `feat/object-types-youtube-75` (the #75 YouTube
  object-type migration, which builds on the #67 scaffold) — so follow-on
  migration issues #76+ have a live container to test against. Check `git -C
  "/mnt/Storage Pool/home/hoop/hoop/constructicon-test" log -1` (or just
  read its `core/`/`web/` files) before assuming this container reflects
  `main` — don't silently reset it to `main` without checking whether it's
  intentionally parked on something else first.

### Live-testing a branch against `constructicon-test`

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
3. `sudo docker restart constructicon-test`, then `sudo docker logs
   constructicon-test --tail 20` — look for `Application startup complete`
   with no traceback.
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
- **Projects are curated, not auto-generated.** Don't "fix" the Projects
  section to auto-derive from tags — that was the original design and was
  explicitly rejected in favor of manual curation via the `projects` /
  `project_items` tables.
- **`clients`/`client_domains`/`ticket_id` are imagerepo-era vestiges.**
  They still work (e.g. OCR auto-tagging by client domain) but aren't part
  of Constructicon's actual personal-use case — don't build new features
  assuming they're a live, meaningful concept for this app the way they
  were for imagerepo.
- **`mcp_server/server.py` is not live** (see "Known gap" above) — don't
  assume MCP tools are reachable against a running Constructicon instance.
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
