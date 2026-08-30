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
- `core/auth.py` (OTP login, already de-scoped from the
  `computercats.net` domain lock) carries over for the authoring side.

**Tag taxonomy — 3 levels deep.** Flat tags (imagerepo's current
`tags TEXT` JSON array) aren't enough to reproduce the site's existing
structure. Modeled on [hooptiej.github.io](https://hooptiej.github.io)'s
own nav (`Home / Blog / Projects`) and Projects' category pages:

1. **Section** — e.g. `Blog`, `Projects`
2. **Category** — e.g. `FPV and Flight`, `AlienWhoop & TinyShark`,
   `Kerbal Space Program Builds`, `3D Modeling and Printing`,
   `Other Builds`
3. **Tag** — free-form, specific to a post (e.g. `camera-mount`,
   `flight-time`, a particular build name)

Schema for this isn't finalized yet — options are three real columns
vs. a slash-delimited path vs. a proper parent-linked tag table. Decide
once post authoring is actually being built, not before.

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
