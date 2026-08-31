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
