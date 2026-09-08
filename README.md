# Constructicon

Named after the Transformers Decepticon that assembles itself out of
smaller robots — not because this app itself was assembled from other
codebases (it was forked and repurposed from
[imagerepo](https://github.com/ComputerCats-Jason/imagerepo), but that's
incidental), but because of what it's *for*: pulling in individual pieces
of content (uploads, posts, links) and assembling them into something
bigger — a **Project** — out of the smaller parts.

## What it does right now

A self-hosted media gallery and personal content store, running as a
FastAPI/Starlette app (`web/app.py`) backed by SQLite (`core/db.py`), on a
LAN-only home server (`10.0.1.78`) with no port forward and no login —
the network perimeter is the security boundary.

- **Upload and browse** — drag-and-drop upload (`/upload`, `/api/upload`),
  a gallery view (`/`, `/gallery`), per-item detail pages
  (`/object/<slug>`), and per-uploader views (`/gallery/user/<uploader>`).
- **A desktop uploader app** (`desktop_app/`) — a separate downloadable
  app that talks to the same `/api/upload`/`/api/content` HTTP API,
  distributed as a zip from `/downloads/...`.
- **OCR and text extraction** — screenshots and other images get OCR'd
  (`tesseract`); PDFs try their embedded text layer first. Extracted text
  is searchable (`/api/search`).
- **"Related items"** — perceptual-hash and embedding similarity
  (`sentence-transformers`) surface related uploads on an item's detail
  page.
- **Tags** — a nestable tag tree (`blog_tags`/`post_tags`) items can
  attach to at any depth, any number of tags at once.
- **Projects** — hand-curated portfolio collections (`projects`/
  `project_items`), distinct from tags: a project is a deliberately
  assembled set of items with manual ordering, shown on the home page's
  Projects column (`/project/<slug>`).
- **Backup** — `POST /api/backup` snapshots the DB and file storage to a
  zip on demand.

**Not yet built:** the blog itself. `capture_events` (the item table) has
everything a blog post needs — description, tags, timestamp, extracted
text — but there's no `/blog` route, no post authoring UI, and no
title/body split yet. Posting today means using the gallery/upload flow;
"blog" is still just a data-model plan, not a page you can visit.

## Supported file types

Whatever a file's extension maps to in `core/object_types/` — each type
is a self-contained module (thumbnailing, OCR-eligibility, metadata)
registered into a shared dispatch table, so adding a new type doesn't
touch the rest of the app. Currently registered:

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

Plus non-file content types with no upload: **YouTube video** (a URL,
classified automatically), generic **web page** (any other URL), **live
stream**, and a plain **written post** (no attached file).

Anything with an unrecognized extension still stores fine — it just gets
no thumbnail and no OCR (`DEFAULT_SPEC`) until a real type spec is added
for it.

## Roadmap: publishing to hooptiej.com

The long-term goal is a data-driven site — this app, with a real backend
and editable content — that generates a static export publishable to
[hooptiej.com](https://hooptiej.com) (`hooptiej/hooptiej.github.io`),
which is GitHub Pages and can only serve static files. So:

- **Constructicon (here):** the dynamic app — uploads, database, editing
  tooling. Runs on the home server, never exposed directly as the public
  site.
- **hooptiej.github.io:** the static output — plain HTML/CSS/JS generated
  from Constructicon's content and pushed there.

**Status: the static-export step doesn't exist yet.** No code in this
repo generates a static site today — that's the next major piece of
unbuilt work, after the blog UI itself (post authoring, a `/blog` route)
lands.

## Palette

`web/static/style.css`'s `:root` custom properties are a
Transformers-Constructicons palette (construction-vehicle yellow-green
body color, deep purple accents, dark chassis, sparing yellow/black
hazard-stripe accents) rather than a generic dark theme. Swap the
`--accent`/`--purple*`/`--hazard-*` variables there to retheme; nothing
else in the CSS should need to change.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture/data-model
writeup and deployment details.
