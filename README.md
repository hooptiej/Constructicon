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
  Projects column (`/project/<slug>`). Dropping a folder onto the upload
  drawer creates one flattened project named after the folder — every
  file inside (subfolders included) lands in that single project.
- **Project write-ups, drafted by Claude** — a project can carry a
  write-up document (`projects.writeup_slug`). Point Claude at a
  project's files (photos, STLs, videos, whatever's there) and it
  reconstructs a plausible build chronology from what's actually
  in the evidence — file timestamps, thumbnails/renders, OCR'd text,
  video frames — then drafts the write-up, flagging its own guesses for
  a quick correction pass before anything's finalized. This is the
  standout feature for turning a pile of old project files into an
  actual readable history, not just a sorted gallery.
- **Imgur import** — pull in the owner's own public Imgur gallery
  (Client-ID auth, no OAuth, so only ever public content), or paste a
  single Imgur post/album URL into the same link field the YouTube/any
  URL field already uses.
- **A live MCP server** (`constructicon-mcp`) — list/search/tag/project
  tools for driving the app from an agent session, including a
  one-call `get_project` (items + tags + cover + write-up body) and a
  reserved `agent_notes` field per item for an agent's own working
  notes, separate from the owner's actual content.
- **Project export** — `GET /api/projects/{id}/export.zip` bundles one
  project's metadata and files for offline/agent analysis, without a
  round trip per file.
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

Plus non-file content types with no upload: **YouTube video** and
**Imgur upload** (both a URL, classified automatically), generic **web
page** (any other URL), **live stream**, and a plain **written post**
(no attached file — this is also the type project write-up documents
use).

Anything with an unrecognized extension still stores fine — it just gets
no thumbnail and no OCR (`DEFAULT_SPEC`) until a real type spec is added
for it.

## Roadmap

The pipeline, as it's actually shaping up:

1. **Sort content into Projects.** Already works — uploads get grouped by
   hand, or in bulk via a folder drop.
2. **Draft a write-up per project.** Already works — Claude reconstructs
   a chronology from a project's own files and drafts the write-up,
   saved to that project's `writeup_slug` document.
3. **Blog UI** — post authoring, a `/blog` route, a title/body split on
   `capture_events`. **Not built yet.** Once it exists, project
   write-ups are the natural source material to backfill posts from,
   rather than starting blog content from scratch.
4. **Static export** — a template that funnels Projects + blog posts
   into a static site, since Constructicon itself always stays
   private/internal (the dynamic app with the database and editing
   tooling, never exposed directly). **Not built yet** — no code in
   this repo generates a static site today. Destination is a personal
   static site elsewhere (GitHub Pages).

Steps 1 and 2 are real and already load-bearing; 3 and 4 are the
remaining unbuilt work, in that order.

## Palette

`web/static/style.css`'s `:root` custom properties are a
Transformers-Constructicons palette (construction-vehicle yellow-green
body color, deep purple accents, dark chassis, sparing yellow/black
hazard-stripe accents) rather than a generic dark theme. Swap the
`--accent`/`--purple*`/`--hazard-*` variables there to retheme; nothing
else in the CSS should need to change.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture/data-model
writeup and deployment details.
