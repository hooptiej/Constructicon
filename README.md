# Constructicon

Named after the Transformers Decepticon that assembles itself out of smaller
robots — because of what this app is *for*: pulling in individual pieces of
content (Images, Art assets, 3dobjects, videos, audio, code snipptest, and links) and assembling them into something bigger — a
**Project**, a **Blog entry**, and ultimately a whole **website** to be deployed to io

## What it is

A self-hosted **media gallery + content store** that doubles as the **control
room for a static personal site** ([hooptiej.com](https://hooptiej.com)). You
ingest content, curate it into Projects and Blog entries, then **export a
self-contained static website** and preview it — and, when you're ready,
**publish it to GitHub Pages**.

It runs as a FastAPI/Starlette app (`web/app.py`) backed by SQLite
(`core/db.py`, no ORM) on a LAN-only home server with **no login** — the
network perimeter is the security boundary, not an auth gate. Constructicon
itself always stays private; only the *exported* static site is ever public.

## How it works

The whole pipeline, from a dropped file to a published page:

```mermaid
flowchart TD
    subgraph ingest [Ingest]
        U[Web upload / drag-drop or folder]
        L[Paste any URL — YouTube / web]
    end
    U --> CE
    L --> CE
    CE[("capture_events — every object<br/>image, video, STL, PDF, PSD, SVG,<br/>audio, YouTube link, …")]
    CE --> ENRICH[OCR · perceptual/embedding similarity · auto-caption · thumbnail]
    CE --> TAGS[Nestable tag tree]
    CE --> PROJ["Projects<br/>curated collections + a write-up"]
    CE --> BLOG["Blog entries<br/>attach projects AND objects, with notes"]
    PROJ --> EXPORT
    BLOG --> EXPORT
    EXPORT["Export engine<br/>core/site_export.build_site()"] --> BUILD["exports/current/<br/>self-contained flat site<br/>(relative links, bundled media + thumbnails)"]
    BUILD --> PREVIEW["/preview/ — served verbatim<br/>(WYSIWYG, no DB)"]
    BUILD --> PUBLISH["Publish → git push<br/>test or live target"]
    PUBLISH --> PAGES[["GitHub Pages → hooptiej.com"]]
```

**The key idea:** `capture_events` holds *every* object. **Projects** and
**Blog entries** are hand-curated ways those objects come together. The
**export engine** flattens whatever you select into a static site whose
`/preview/` is the exact bytes that would ship — so previewing *is* the QA.

## Architecture

```mermaid
flowchart LR
    Browser -->|HTTP| Web["constructicon-web<br/>FastAPI/Starlette (web/app.py)"]
    Agent["Claude / MCP client"] -->|MCP| MCP["constructicon-mcp<br/>(mcp_server/server.py)"]
    Web --> DB[("SQLite — imagerepo.db<br/>core/db.py, raw SQL")]
    MCP --> DB
    Web --> STORE[["storage/ — files + thumbnails"]]
    Web --> EXP[["exports/ — site builds (keep last 2)"]]
    Web -->|"git push (token)"| GH[("GitHub Pages repo")]
```

- **`web/app.py`** — every HTTP route: page routes (Jinja2 templates in
  `web/templates/`), the `/api/*` JSON+form API the templates' JS calls, the
  public `/f/<slug>` hotlink/thumbnail routes, and the `/preview/` static mount.
- **`core/db.py`** — the schema source of truth and all SQLite access (no ORM).
- **`core/object_types/`** — a registry of per-type specs (thumbnailing, OCR
  eligibility, metadata). Adding a file type is one spec, not edits across the app.
- **`core/site_export.py`** — the export engine (gather → render the Jinja
  templates in `web/export_templates/` → bundle media → build report; plus the
  git publish).
- **`core/ocr.py` / `core/similarity.py` / `core/captions.py`** — text
  extraction, "related items", and local-vision auto-captions.
- **`mcp_server/server.py`** — the live `constructicon-mcp` tool surface for
  driving the app from an agent.

## The content model

| Table | What it is |
|---|---|
| `capture_events` | **Every object** — one row per image/video/STL/PDF/link/etc. `slug` is the unguessable URL id; `media_type` classifies it; files live in `storage/` (or `external_url` for links). |
| `blog_tags` / `post_tags` | A tag tree (nestable to any depth) and its many-to-many join to objects. |
| `projects` / `project_items` | Hand-curated collections of objects with manual ordering, plus an optional `writeup_slug` pointing at a write-up document. |
| `blog_entries` / `blog_entry_projects` / `blog_entry_items` | Dated narratives that attach **both projects and objects**, each with a per-attachment note and sort order. The unit the export turns into blog pages. |
| `app_settings` | Generic key/value store for secrets/config (API keys, the Pages publish token + targets) — surfaced presence-only, never echoed back. |

```mermaid
erDiagram
    capture_events ||--o{ post_tags : "tagged"
    blog_tags ||--o{ post_tags : "in"
    blog_tags ||--o{ blog_tags : "parent_id"
    projects ||--o{ project_items : "contains"
    capture_events ||--o{ project_items : "post_slug"
    projects ||--o{ projects : "parent_id"
    blog_entries ||--o{ blog_entry_projects : "references"
    projects ||--o{ blog_entry_projects : "in"
    blog_entries ||--o{ blog_entry_items : "references"
    capture_events ||--o{ blog_entry_items : "post_slug"
```

## Features

- **Ingest** — drag-and-drop upload (a folder drop becomes a Project) and pasting any URL
  (YouTube / generic web page, classified server-side).
- **OCR & search** — images are OCR'd (`tesseract`), PDFs use their text layer;
  everything is searchable.
- **Related items** — perceptual-hash + sentence-transformer similarity surface
  related objects on a detail page.
- **Auto-captions** — optional local vision model (`moondream` via Ollama) drafts
  caption *suggestions*.
- **Projects & write-ups** — curate objects into a Project, then point Claude at
  its files and it reconstructs a plausible build chronology (from timestamps,
  renders, OCR text, video frames) and drafts a write-up for a quick correction
  pass. Turning a pile of old files into a readable history is the standout
  feature.
- **Blog entries** — dated narratives that pull together one or more projects
  *and* loose objects, each attachment carrying its own note.
- **Export → preview → publish** — build a self-contained static site from any
  selection of projects/blog entries; preview it verbatim at `/preview/`; publish
  it to a **test** or **live** GitHub Pages repo with one click (fine-grained PAT
  in admin settings; CNAME preserved, `.nojekyll` written, builds kept for
  rollback).
- **A live MCP server** — list/search/tag/project/blog tools for driving the app
  from an agent session, plus a per-item `agent_notes` field.
- **Backup** — `POST /api/backup` snapshots the DB + storage to a zip on demand.

## Supported file types

Each type is a self-contained module in `core/object_types/` (thumbnailing,
OCR-eligibility, metadata) registered into a shared dispatch table.

| Type | Extensions |
|---|---|
| Image | `.png` `.jpg` `.jpeg` `.ico` `.bmp` `.tiff` `.tif` `.webp` |
| Animated GIF | `.gif` |
| Video | `.mov` `.mp4` |
| Audio | `.mp3` `.m4a` `.ogg` `.wav` |
| PDF document | `.pdf` |
| 3D printing file | `.stl` |
| Photoshop document | `.psd` |
| Illustrator file | `.ai` |
| Vector graphic | `.svg` `.eps` |
| Font | `.ttf` `.otf` |
| Data file | `.csv` |
| Source code | `.php` `.py` `.js` `.sh` `.json` `.yaml` `.yml` `.html` `.css` `.sql` |
| Archive | `.zip` `.7z` |

Plus non-file content types (a URL, no upload): **YouTube video**, generic
**web page**, **live stream**, and a plain **written post** (the type project
write-up documents use). Unrecognized extensions still store fine — they just
get no thumbnail/OCR until a spec is added.

## Export & publish, in detail

1. **Build** — the export builder (a drawer in the app) lets you pick which
   projects and blog entries to include plus site title/tagline, then
   `build_site()` gathers them, renders the `web/export_templates/` Jinja
   templates to flat HTML with **all-relative links**, and bundles each object's
   original file *and* its rendered thumbnail into `media/`. Output goes to
   `exports/<timestamp>/`, mirrored to `exports/current/`, pruned to the last 2.
2. **Preview** — `/preview/` is a `StaticFiles` mount over `exports/current/`, so
   you're looking at the exact bytes that would ship — no DB, no re-render.
3. **Publish** — pushes `exports/current/` to a configured GitHub Pages repo
   (a **test** target and a **live** target), preserving the target's `CNAME`
   and adding `.nojekyll`. Auth is a fine-grained PAT stored in admin settings;
   it's used only as an ephemeral push credential, never written to `.git/config`
   or logged.

## Deployment

Runs on a shared home server as two plain `docker compose` containers —
`constructicon-web` and `constructicon-mcp` — running one image with the app
code baked in and tagged with the release version (`scripts/deploy.sh` builds it;
rollback = run the previous version's image). Isolated
`constructicon-test` / `constructicon-test-mcp` siblings mirror it for testing
changes before production; they bind-mount the checkout's code over the image
through a compose override, so a test deploy is a file copy + restart. See [`CLAUDE.md`](CLAUDE.md) for the full deployment
runbook, data-model notes, and gotchas.

## Install on a new host

Any Linux box with Docker works; the home server's ZFS and `sudo` are not needed (#698, #699).

**Requirements**

- An **x86-64** Linux host (the only architecture built and tested so far).
- **Docker Engine with the Compose v2 plugin.** `docker compose version` must work: compose is
  required, and `scripts/deploy.sh` stops with the install link if it's missing (the old standalone
  `docker-compose` v1 isn't supported). Debian/Ubuntu: `apt install docker-compose-plugin`.
- `bash`, `git` and `python3` on the host (deploy.sh and the token tool run there).
- About 4 GB of disk for the image, plus your data.
- Optional: `sudo`. deploy.sh uses it only when your user can't reach the Docker socket; a user in
  the `docker` group (or root) deploys without it.
- Optional: ZFS. With the data in a dataset of its own, each deploy takes a ZFS snapshot first;
  without it, each deploy copies the database folder first (see "Backups" below).

**1. Get the code and a compose file**

```bash
git clone https://github.com/hooptiej/Constructicon.git constructicon && cd constructicon
cp docker-compose.yml.example docker-compose.yml
```

Edit `docker-compose.yml`: delete the `constructicon-test` service (a second test instance you
don't need), set `IMAGEREPO_BASE_URL` to this host's address, and either run captions (`docker
compose --profile captions ...`, or `COMPOSE_PROFILES=captions` in `.env`) or set
`CAPTION_DISABLED=1` on the web service. Per-install settings live here and in `.env`; nothing about
your host is baked into the image.

**2. Secrets**

```bash
mkdir -p secrets && chmod 700 secrets
python3 -m core.manage.mcp_token generate secrets/constructicon_token     # the install token
python3 -m core.manage.mcp_token generate secrets/ollama_restart_token    # the compose file mounts it
```

The install token is what claims the install at first sign-in, and what scripts and MCP clients send
as `Authorization: Bearer`. Never put it in git or chat.

**3. Data folders, owned by uid 1000 (do this before the first start)**

The app runs as **uid/gid 1000** inside its container, and every folder it writes must be owned by
that uid on the host. If your user isn't uid 1000 (`id -u`), the app can't write them, and it
refuses to start with a message naming the folder, its owner and this fix:

```bash
mkdir -p storage db backups exports import
sudo chown -R 1000:1000 storage db backups exports secrets
chmod 0750 storage db backups exports
```

No sudo? Once the image exists (`docker compose build constructicon-web`), the image can do it:
`docker run --rm --user 0 --entrypoint chown -v "$PWD/db":/d constructicon:latest -R 1000:1000 /d`
(once per folder). The example compose file mounts `storage` and `db`; create and chown `backups`
and `exports` too if you mount them.

**4. First deploy**

```bash
./scripts/deploy.sh
```

It fetches `origin/main`, takes the pre-deploy backup (nothing to back up yet on a new install),
builds the image tagged with the release version (and `constructicon:latest`), and starts it with
`docker compose up -d`. Then open `http://<host>:8000`: the first visit goes to `/setup`, which
asks for the install token (`sudo cat secrets/constructicon_token` on the host) and creates the first
admin. If a container keeps restarting, `docker compose logs constructicon-web` says why.

**Upgrade:** `./scripts/deploy.sh` again. **Roll back** to an earlier version's image:
`./scripts/deploy.sh --rollback <version>` (`docker image ls constructicon` lists them; the newest 5
are kept). Database migrations only go forward, so a rollback across a migration also needs the
database restored from the pre-deploy backup.

**Backups.** Before every deploy and rollback, deploy.sh backs up the data
(`./scripts/deploy.sh --backup-plan` says which kind, and changes nothing):
- **ZFS snapshot**, when the data folder's parent is a ZFS dataset of its own (the home server).
- **Otherwise, a copy of the database folder**, made with SQLite's backup API (consistent even with
  the app running; never a raw copy of a live database), into `predeploy-backups/<name>/` next to
  `db/`, newest 10 kept. To restore: stop the app, copy the files back into `db/`, start it.
  Uploaded files (`storage/`) are not in it: back them up your own way, or with Admin's "Back up now" (a zip of the database and storage).

`--no-snapshot` deploys without a backup.

## Running locally

```bash
pip install -r requirements.txt
uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
```

Needs `tesseract-ocr`, `libcairo2`, and `ghostscript` for OCR / SVG / EPS
thumbnailing (missing them degrades those features rather than crashing), and
`git` for the publish step. `imagerepo.db` and `storage/` are created at the repo
root on first run (both gitignored).

## Palette

`web/static/style.css`'s `:root` custom properties are a Transformers-
Constructicons palette (construction-vehicle yellow-green, deep purple accents,
dark chassis, hazard-stripe yellow/black) — swap `--accent` / `--purple*` /
`--hazard-*` to retheme; nothing else should need to change.
