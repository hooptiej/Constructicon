# Constructicon

Named after the Transformers Decepticon that assembles itself out of
smaller robots — this project is exactly that: built by pulling pieces
out of other codebases (starting with
[imagerepo](https://github.com/ComputerCats-Jason/imagerepo)) rather
than starting from scratch.

## Goal

Build a data-driven website — real backend, real database, editable
content — that runs on the local dev server (currently the TrueNAS box
at `10.0.1.78`). From that live site, generate a static export/snapshot
and publish it to [hooptiej.com](https://hooptiej.com)
(`hooptiej/hooptiej.github.io`), which is GitHub Pages and can only
ever serve static files.

So the split is:
- **Constructicon (here):** the dynamic app — auth, uploads, database,
  whatever admin/editing tooling the site needs. Lives and runs on the
  home server, never directly exposed as the public site.
- **hooptiej.github.io:** the static output — what actually gets
  pulled/rendered out of Constructicon and pushed as plain HTML/CSS/JS
  for GitHub Pages to serve.

The static-export step (how content gets frozen into publishable HTML)
isn't designed yet — that's a later piece of work here.

## Next step: blog-driven site on the imagerepo framework

The first real build target is a blog, reusing imagerepo's existing
pieces rather than writing a new backend:

- `core/db.py`'s `capture_events` table becomes the post store (already
  has description, tags, timestamp, extracted text, related-item links —
  everything a blog post needs except a title/body split, which post
  authoring will add).
- `capture_events` isn't image-only: `media_type` (`'image' | 'video' |
  'youtube' | 'document' | 'any'`, deliberately no CHECK constraint — a
  loose classifier, not a rigid enum) says what kind of content the row
  actually is, independent of `source` (upload-pipeline metadata like
  `'screenshot'`). Rows for content that lives elsewhere rather than an
  uploaded file (e.g. a YouTube video) use `external_url` instead of
  `filename`/`stored_filename`, `content_description` for the content's
  own description (distinct from `description`, which is
  uploader/tagging metadata), and `content_date` for the content's own
  real-world date (distinct from `timestamp`, which is capture/upload
  time). `db.insert_content()` creates this kind of row without
  requiring an uploaded file; `db.insert_upload()` still handles the
  file-upload path and defaults `media_type` to `'image'`.
- `core/storage.py`, `core/ocr.py`, `core/similarity.py` carry over
  as-is for image handling, text extraction, and "related posts."
- No login gate — Constructicon is a single-owner personal tool on a
  LAN-only dev server (no port forward), so the network perimeter is
  the security boundary, not an auth system. (imagerepo's original
  multi-tech OTP login/session/presence machinery was stripped out;
  see `capture_events.tech`, which is now just freeform attribution
  text rather than a real user identity.)

**Tag taxonomy — loose and nestable, not fixed columns.** Flat tags
(imagerepo's original `tags TEXT` JSON array) aren't enough to
reproduce the site's existing structure, but a rigid 3-column
Section/Category/Tag split turned out to be the wrong shape too — a
post should be able to attach to *any* tag, at *any* depth, and to
*more than one at once* (e.g. a build post tagged under both
`FPV and Flight` and `3D Modeling and Printing`). So instead:

- `core/db.py` has a `blog_tags` table — each row is `{name, slug,
  parent_id}`, nestable to any depth (not just 3 fixed levels).
  `get_or_create_tag(name, parent_id)` makes new tags on the fly,
  deduped per-parent so the same name can exist under different
  parents without colliding.
- `post_tags` is a plain many-to-many join between posts
  (`capture_events.slug`) and `blog_tags.id` — a post can carry as
  many tags as make sense, spanning multiple branches of the tree.
- Modeled loosely on [hooptiej.github.io](https://hooptiej.github.io)'s
  existing category breakdown (`FPV and Flight`, `AlienWhoop &
  TinyShark`, `Kerbal Space Program Builds`, `3D Modeling and
  Printing`, `Other Builds`) as the starting set of top-level tags,
  not as a fixed schema.

**How the three pages use this:**
- **Blog** — plain reverse-chronological feed of every post
  (`db.list_recent_posts()` with a high limit), tags irrelevant to the
  ordering.
- **Home** — a highlights strip: the same feed, just a small limit
  (`db.list_recent_posts(n)`).
- **Projects** — a table of contents built straight from the tag tree
  (`db.list_tag_tree()`), nested to match; picking any tag (root or
  child) lists every post filed under it *or any of its descendants*
  (`db.list_posts_for_tag(tag_id)`), so a top-level category page
  doesn't require posts to be tagged with the category itself.

**Projects — curated collections, distinct from the tag tree.** Don't
confuse this with the "Projects" *page* above (the tag-tree table of
contents) — `core/db.py` also has a separate `projects` table for
hand-assembled portfolio cards: a title, description, optional
`cover_slug` (a `capture_events.slug` to use as the card's cover
image, no FK constraint since capture_events rows can be deleted
independently), and a freeform `status` (`'active'`, `'archived'`,
whatever — no CHECK constraint). Where the tag tree groups posts
automatically by whatever tags they carry, a project is a deliberate
curated set an owner assembles by hand — "projects are how the other
objects come together." `project_items` is the many-to-many join
(`project_id`, `post_slug`, `sort_order`) with a manual `sort_order`
so items within a project can be arranged on purpose rather than just
falling out in chronological order.

- `db.create_project(title, ...)` auto-generates a unique slug from
  the title, same dedup-with-numeric-suffix pattern as
  `get_or_create_tag`; `db.get_project(id_or_slug)` looks up either
  way since a project detail page will likely be reached by slug in a
  URL; `db.list_projects(status=None)` and `db.update_project(...)`
  (partial update, bumps `updated_at`) round out the card itself.
- `db.add_item_to_project(project_id, post_slug, sort_order=None)`
  appends at the end when no explicit order is given;
  `db.remove_item_from_project(...)` detaches one;
  `db.list_project_items(project_id)` returns full post data (joined
  against `capture_events`, mirroring `list_posts_for_tag`) in
  `sort_order`; `db.list_projects_for_post(post_slug)` is the reverse
  lookup, backed by `idx_project_items_slug`.
- No routes/templates yet — this phase is data-layer only, same as
  the `media_type` work above.

**Design guide:** [hooptiej.github.io](https://hooptiej.github.io) (the
already-migrated static site) is the primary structural reference —
its nav, its Projects category breakdown, its per-post layout. The old
Wix site (`hooptiej.wixsite.com/hooptiej`) is secondary/historical, since
its content is already folded into the GitHub Pages version.

## Retained art assets

Pulled from [hooptiej-site](../hooptiej-site) into
[`assets/brand/`](assets/brand/) so the branding (dragon logo, wordmarks,
favicons, boot logos) is available here independent of that repo:
`logo.png`, `hooptiej-wordmark.png`, `alienwhoop-wordmark.png`,
`hero-alien-icon.png`, `favicon-16.png`, `favicon-32.png`,
`apple-touch-icon.png`, `bootlogo-1.png`–`bootlogo-5.png`,
`bootlogos-bg.jpg`. Photos (desk shots, build photos) were left out —
those are post *content*, not brand assets, and belong in imagerepo's
own storage once the blog is actually ingesting them.
