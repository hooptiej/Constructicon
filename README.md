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
isn't designed yet — that's the next real piece of work here.
