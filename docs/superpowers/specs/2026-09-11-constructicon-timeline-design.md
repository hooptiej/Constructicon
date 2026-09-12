# Constructicon: Timeline feature

Status: draft, pending owner review
Date: 2026-09-11
Tracked in quest-log: `constructicon-timeline-feature-gallery-p-mtwybyjy`

## Summary

Constructicon already answers "what" (content items, via `capture_events`)
and "who" (the `tech`/Source field, tags). This feature adds "when": a
chronological timeline on two surfaces —

1. **Gallery page** (`web/templates/home.html`): a sticky rail down the
   left side showing every project in date order, alongside the existing
   project grid (grid is unchanged).
2. **Project detail page** (`web/templates/project_detail.html`): a sticky
   rail showing that project's own content items in date order, alongside
   the existing manually-ordered (`project_items.sort_order`) item grid
   (grid is unchanged).

Both rails share one component and interaction model. "How" and "why" are
explicitly out of scope for this feature — this only correlates *when*.

## Non-goals

- No change to `project_items.sort_order` or the existing grid layouts —
  the timeline is an additional lens, not a replacement.
- No people-tagging system. The owner's "who" reference is the existing
  `tech`/Source field and tag tree; this feature doesn't touch either.
- No "how"/"why" — narrative/write-up features are separate, future work.
- No new relationship table for timeline membership — a project's own
  `project_items` rows are the timeline's item source; the gallery
  timeline's source is just `projects`.

## Data model

### Objects (`capture_events`) — single point in time

A content item is a moment, not a span. Its effective display date:

```
effective_date(item) = item.display_date_override
                     ?? item.content_date
                     ?? item.timestamp   # NOT NULL, always available
```

New column, added via the existing idempotent migration pattern in
`core/db.py`'s `init_db()` (see the `ALTER TABLE capture_events ADD
COLUMN` loops around line 271 and 282 — same pattern, new tuple):

```sql
ALTER TABLE capture_events ADD COLUMN display_date_override REAL
```

`NULL` means "no override, use the computed default." Resetting is
setting it back to `NULL`, not deleting a row or special-casing.

### Projects (`projects`) — a span, not a moment

The owner's framing: projects take time, they aren't moments. A project
gets a start and an end, each independently overridable:

```
effective_start(project) = project.start_date_override
                         ?? min(effective_date(item) for item in project.items)
                         ?? project.created_at   # empty-project fallback

effective_end(project)   = project.end_date_override
                         ?? max(effective_date(item) for item in project.items)
                         ?? project.created_at   # empty-project fallback
```

An empty or single-moment project naturally has `effective_start ==
effective_end` — it renders as a point, not a span, with no special-case
branch required (see "Span rendering" below).

Two new columns, same migration pattern:

```sql
ALTER TABLE projects ADD COLUMN start_date_override REAL
ALTER TABLE projects ADD COLUMN end_date_override REAL
```

### Effective-date helper

One new pure function in `core/db.py` (or a new small `core/timeline.py`
if `db.py` is getting crowded — implementer's call), e.g.
`resolve_item_date(row)` / `resolve_project_span(project_id)`, used by
every call site below rather than duplicating the override-chain logic
per caller. This is the one function unit-tests target directly (see
Testing).

## Backend: page data

- **Gallery** (`home.html`'s data source in `web/app.py`): extend the
  existing project-list query to include `effective_start`,
  `effective_end`, and whether `parent_id is None` (top-level) or not
  (child) for each project, alongside what's already passed to the
  template today.
- **Project detail** (`project_detail.html`'s data source): extend the
  existing item-list query (the one already producing
  `.project-item-grid`'s rows, ordered by `sort_order`) to also include
  each item's `effective_date`. The grid keeps using `sort_order`; the new
  rail uses `effective_date`.

No new page routes — this rides along with the existing `/` and
`/project/{slug}` routes' template context.

## MCP tool changes (`mcp_server/server.py`)

The owner explicitly wants to be able to tinker with these dates via MCP,
not just the web UI. Two gaps exist today:

**Read side** — neither `_to_public` (line 29-49) nor `_to_public_project`
(line 52-62) exposes any date field at all. Add:
- `_to_public`: `timestamp`, `content_date`, `display_date_override`,
  and the resolved `effective_date`.
- `_to_public_project`: `created_at`, `start_date_override`,
  `end_date_override`, and resolved `effective_start`/`effective_end`.

**Write side** — `constructicon_update` and `constructicon_update_project`
follow a `None` = "don't change" convention for every existing field
(see `constructicon_update`'s `if description is not None or tags is not
None:` dispatch, line 183). A single nullable date param can't distinguish
"leave unchanged" from "clear the override" under that convention, so each
tool gets a companion boolean:

```python
def constructicon_update(..., display_date: float | None = None,
                          reset_display_date: bool = False) -> dict | None:
    ...
    if reset_display_date:
        db.set_display_date_override(slug, None)
    elif display_date is not None:
        db.set_display_date_override(slug, display_date)
    ...

def constructicon_update_project(..., start_date: float | None = None,
                                  reset_start_date: bool = False,
                                  end_date: float | None = None,
                                  reset_end_date: bool = False) -> dict | None:
    ...
```

`reset_*=True` wins regardless of what's in the corresponding date param
— same "explicit reset beats a stray value" precedent as the rest of the
tool surface. New small `db.py` setters (`set_display_date_override`,
`set_project_date_overrides`) rather than overloading `update_tags`/
`update_project` with unrelated concerns.

## Web UI: override fields

A "Timeline date" input + "Reset" button added to:
- The object edit UI (`object_detail.html`, alongside the existing
  description/tags editing) — one field, backed by
  `display_date_override`.
- The project edit UI (`project_detail.html`, alongside the existing
  title/description/status editing) — two fields (start, end), same
  reset-to-computed-default behavior.

These call the same new `db.py` setters the MCP tools use — one source of
truth for the override-vs-reset semantics, web and MCP paths converge on
the same functions.

## Shared component: `timeline-rail.js`

One reusable module, instantiated twice with different data:
- Gallery page: fed each project's `{id, thumbUrl (cover_slug), label
  (title), effective_start, effective_end, isChild}`.
- Project detail page: fed each item's `{id, thumbUrl, label,
  effective_date}` (single point, no span).

### Layout

`position: sticky` column, shifting the main content area over (not an
overlay) — matches the existing page shell's flex/grid structure. Own
internal scroll, independent of the page's scroll (a long project or item
list scrolls inside the rail rather than growing the page).

### Two states

**Dormant** (default): small, evenly-spaced thumbnails. A project span
renders as its thumbnail (anchored at `effective_start`'s position in the
list order) with a short fixed-length trailing bracket/line to an
end-cap — a compact Gantt-bar, but at a *token* length in this state, not
time-accurate. Child projects get a small indent/icon distinguishing them
from top-level. Object-timeline entries (project detail page) are always
single points — no bracket.

**Interactive** (on hover/scroll engagement within the rail): two things
happen together —
1. *Dock magnification* — thumbnails scale by cursor proximity (classic
   dock algorithm: closer to cursor = larger, falling off with distance),
   via CSS `transform: scale()` + transition.
2. *Spacing morph* — vertical positions recompute from even-spacing to
   real time-proportional spacing, using the full min/max date range of
   the rendered set mapped to the rail's available height. Project
   brackets stretch to their real start→end proportional length in this
   state.

Reverts to dormant on mouse-leave / scroll-idle timeout.

### Interaction

Click a thumbnail → a card-preview modal, a new variant of the existing
`.modal-backdrop`/lightbox pattern in `object_detail.html` (lines
291-296, 1074-1087 — currently only does a full-size image pop-out; this
adds a second modal body showing cover/title/effective date(s)/description
snippet instead of a bare image). A button inside that modal navigates to
the real project or object detail page — two clicks to the full page,
matching the owner's stated preference.

## Edge cases

- `timestamp` is `NOT NULL`, so an object's effective date is always
  resolvable.
- Empty project (no items): `effective_start == effective_end ==
  created_at`, renders as a point (see "Span rendering" — no special case
  needed).
- Project with exactly one item: same collapse-to-point behavior, for the
  same reason.
- Very long project/item lists: rail scrolls internally; dock/spacing math
  operates on the currently-rendered set, not the whole page.

## Testing

No test suite exists in this repo today (per `CLAUDE.md`) — verification
is manual against a running instance. For this feature:

- **Unit-testable in isolation**: the effective-date/span resolution logic
  (`resolve_item_date`/`resolve_project_span`) is pure — override
  precedence, empty-project fallback, span collapse-to-point. Worth a
  small standalone test script even though the repo has no test runner
  convention yet (see e.g. `scripts/test-adversarial-notes.mjs`'s
  equivalent role in the quest-log repo as precedent for a repo without a
  formal suite).
- **Not unit-testable**: dock magnification, spacing morph, sticky layout
  — inherently visual/interactive. Verify live on `constructicon-test`
  per the repo's documented recipe (tar-over-ssh deploy, `docker restart`,
  verify via `docker exec ... python3 -c "import urllib.request; ..."`
  since the container has no `curl`). Follow the existing serialization
  rule — don't run this alongside another agent's own
  `constructicon-test` deploy.

## Open implementation details (not blocking, resolve during implementation)

- Exact rail width, thumbnail size tokens, and magnification falloff curve
  — visual tuning, done live against `constructicon-test` rather than
  guessed here (owner explicitly wants no Artifact-mockup detour).
- Whether the effective-date helper lives in `db.py` or a new
  `core/timeline.py` — implementer's call based on how large `db.py` gets.
