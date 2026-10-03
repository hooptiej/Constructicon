# V2 card model: design spec

Status: draft for the overnight build (2026-10-02). Issue: #482. Source decisions: #428 (every comment dated
2026-10-02; later comments override earlier ones), #480 (reorganizing MCP), #481 (handoff + "Overnight run plan").

This is the one document every build piece (1-8, section 9) is checked against. It is written against the **actual v1
schema in `core/db.py`**, not against a description of it. Where the issues and the real schema disagreed, section 11
records what was chosen and why.

Ground rules carried over from the owner's decisions:

- **Evolve v1.** New columns and tables are added to the existing SQLite DB; the real archive migrates in place. No
  separate V2 codebase.
- **No game vocabulary in the UI.** The card *look* (frames, pips, stacks, fans) is kept; the labels are plain.
- **Never guess on a judgment call.** Ambiguous migration cases are queued as `pending_decisions` with a suggested
  answer, not decided.
- **Validation lives in `core/`**, so the web UI and the MCP cannot disagree.
- **Static site export is untouched** by this build (follow-up issue). Nothing here may break it; see 4.1 on the legacy
  `projects.status` column.

---

## 1. Vocabulary

User-facing labels (pages, MCP result text, card faces). Internal enum values are `snake_case`.

| Concept | UI label | Internal |
|---|---|---|
| A card kind | Project, Thing, Action, Family, Collection, Event | `projects.kind` |
| Hobby | Hobby | `blog_tags.is_hobby = 1` (card kind is a *rendering*, not a column) |
| File | File (asset card) | `capture_events` row |
| Activity | Active / Inactive | `activity` |
| Stage | In progress, In use, Idea, Paused, Done, Stopped | `stage` |
| Stop reason | Failed, Abandoned | `stop_reason` |
| Whereabouts | Have it, Partial, Parted out, Sold, Gifted, Lost, Never built | `whereabouts` |
| Origin | Created, Found, Collected, Referenced, Client-owned | `provenance` |
| Membership | "In family", "In hobby" | `family_members`, `project_hobbies` |
| Nesting | "Part of" | `projects.parent_id` |
| Typed link | Built for, Applies to, Used in, Inspired by, Related | `project_relations.type` |
| Group code | 2-4 letter hobby code on a card (COL, 3DP, RCA) | `blog_tags.group_code` |
| Curation level | five pips | computed |
| Highlight | highlight dot | `highlight` |

Words that must **not** appear in UI strings, tool descriptions or CSS class names: deck, hand, field, graveyard,
banished, monster, spell, trap, ritual, equip, xyz, mana, rarity, foil, summon. (Frames, "stack" and "fan" are layout
words, not game rules, and stay.) The trading-card games remain a mental model for the schema only.

Kind meanings, one line each:

- **Project:** an effort with a story that is not a single object: a design, a system, a multi-part build, a container
  for nested parts.
- **Thing:** one physical object (a truck, a radio, a car on a shelf). A one-object build is a Thing, not a Project.
- **Action:** work done *on* something (code that runs on one truck, a repair, a mod). Usually has an `applies_to` link.
- **Family:** an umbrella over related, independently-standing members (AlienWhoop). Membership, not nesting.
- **Collection:** a static set kept together (a shelf, reference material). Membership, like a family.
- **Event:** a one-off occurrence (a field trip). Files and notes hang off it.

---

## 2. Data model: what v1 already has (verified in `core/db.py`)

Everything below was checked against the `SCHEMA` string and `init_db()`; v2 builds on it.

- `projects(id, slug, title, description, cover_slug, cover_project_id, status TEXT DEFAULT 'active', created_at,
  updated_at, tag_id, parent_id, writeup_slug, start_date_override, end_date_override)`. `status` has **no CHECK**;
  `PROJECT_STATUSES = wip, complete, shelved, means-to-an-end, abandoned, failed, idea, published, reference-only`.
  Legacy values `active` (the column default, treated as `wip`) and `archived` (treated as `complete`) also occur
  (`core/curator.py` `_normalize_status`). Real DB counts (71 projects): complete 41, abandoned 11, shelved 6, wip 5,
  means-to-an-end 4, failed 1, archived 1, active 1, reference-only 1.
- `project_items(project_id, post_slug, sort_order)`: files in a card, many-to-many (a file can be in several cards).
- `project_hobbies(project_id, hobby_tag_id)`: many-to-many, composite PK, ordinary rowid table (insertion order is
  recoverable via `rowid`).
- `project_relations(slug_a, slug_b, created_at)`, PK `(slug_a, slug_b)`, keyed by **project slug**, stored **in both
  directions** (symmetric, untyped). Object-level `capture_event_relations` is separate and is **not** changed.
- `blog_tags(id, name, slug, parent_id, is_hobby, hobby_status)` with `HOBBY_STATUSES = active, dormant, abandoned`.
- `capture_events.provenance` (per file; `PROVENANCE_TYPES = found, created, documented, result, reference, design`),
  `capture_events.highlight INTEGER` (per file), `display_date_override`, `content_date`, `source_modified_at`.
- `audit_log(id, method, path, form_body, affected_slugs, status_code, error_detail, timestamp)`: an **HTTP request
  log** written by the web middleware for mutating `/api/*` calls only. It has no before/after values, and MCP tool calls
  do not pass through it (the MCP server calls `core.db` directly).
- `pending_decisions(id, kind, post_slug NOT NULL, payload JSON, created_at, resolved_at)`. Kinds today: `project_match`
  (`automatch.KIND_PROJECT_MATCH`) and `retype`. `post_slug` is assumed to be a `capture_events.slug`: see 6.3.
- Migration mechanism: **`core.db.init_db()`**, idempotent by construction: `CREATE TABLE IF NOT EXISTS` in `SCHEMA`
  plus `PRAGMA table_info` guards around `ALTER TABLE ... ADD COLUMN`, plus unconditional idempotent `UPDATE`s. It runs
  on web startup (`web/app.py`) and MCP startup (`mcp_server/server.py`). Every migration in this spec uses that
  mechanism.

---

## 3. Data model changes

New code layout:

- `core/card_rules.py`: pure (no DB). Enums, labels, `validate_*` functions, the stage/activity table, legacy-status
  adapters. Unit-testable with plain dicts.
- `core/cards.py`: the operations (section 6). Reads and writes through `core/db.py` helpers, enforces `card_rules`,
  records every write in the change log (section 3.13).
- `core/card_level.py`: the five-pip computation (3.11).
- `core/db.py` keeps **all SQL**; it gains the new columns/tables in `SCHEMA` + `init_db()` and thin helper functions.

### 3.1 Kind

```
ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'project'
```

`card_rules.KINDS = ("project", "thing", "action", "family", "collection", "event")`. No DB CHECK (consistent with
`media_type` and `status`); `card_rules.validate_kind` is the gate. Existing rows land on `'project'`; section 4 queues
the ones that need a judgment.

Hobbies stay `blog_tags` rows with `is_hobby = 1` (no `kind` column; they are not in `projects`). Files stay
`capture_events` rows and are the "asset" cards. The card renderer treats three row sources uniformly via a
`card_type` in its JSON: `project|thing|action|family|collection|event` (from `projects.kind`), `hobby`, `asset`.

Kind rules (enforced in `cards.set_kind` and on create):

- `family` and `collection` ("group kinds") must not have a `parent_id`, and cannot be a nesting parent. Use
  membership.
- Leaving a group kind with members present is refused unless `force=True`, which drops the memberships (logged, undoable).
- `action`, `event`, `family`, `collection` cannot carry `whereabouts` (3.4); changing to one of those kinds with
  whereabouts set is refused unless the caller also clears it.

### 3.2 Status: activity + stage + stop reason

```
ALTER TABLE projects ADD COLUMN activity    TEXT        -- 'active' | 'inactive'
ALTER TABLE projects ADD COLUMN stage       TEXT        -- see table
ALTER TABLE projects ADD COLUMN stop_reason TEXT        -- 'failed' | 'abandoned', only when stage='stopped'
```

| Stage | Activity | Meaning |
|---|---|---|
| `in_progress` | active | being worked on |
| `in_use` | active | finished and still running or used |
| `idea` | inactive | may never become active |
| `paused` | inactive | may return |
| `done` | inactive | finished, no longer in use |
| `stopped` | inactive | ended early; `stop_reason` is `failed` or `abandoned` |

`activity` is **redundant by design** (it is a pure function of `stage`) so SQL can filter on it cheaply and hobbies
share the same switch. It is written only by `cards.set_status`, which derives it. A new `activity` value never enters
the DB any other way.

Validation (`card_rules.validate_status(kind, stage, stop_reason, activity=None, whereabouts=None)`), all errors raise
`CardError("bad_status", ...)`:

1. `stage` must be one of the six. `activity`, if supplied, must equal `ACTIVITY_OF[stage]` (mismatch is an error,
   not a silent fix). If only `activity` is supplied with no stage: error "pick a stage".
2. `stop_reason` is required iff `stage == 'stopped'`, must be `failed` or `abandoned`; supplying it with any other
   stage is an error. Setting a non-`stopped` stage with no `stop_reason` clears the stored one.
3. **Ideas are never active:** `idea` is inactive by the table, and an `idea` card cannot be given
   `activity='active'` through any path (rule 1 already rejects the mismatch). Provenance is not a gate on any stage.
4. Kind constraints: `event` allows `idea, in_progress, done, stopped` only (not `in_use`, not `paused`).
5. Cross-field with whereabouts: `in_use` requires whereabouts in `{NULL, have_it, partial}` (you cannot be using a
   thing you sold, gifted or lost); `never_built` forbids `in_progress` and `in_use`.
6. **Warning, not error** (returned in `warnings`): an inactive card nested under an active parent or the reverse;
   `done` while a nested child is `in_progress`.

There is no transition state machine: any stage can follow any other, subject to the rules above.

Default for new cards: `kind='project'`, `stage='in_progress'`, `activity='active'` (matches v1's default
`status='active'`). `create_project` gains optional `kind`, `stage`, `stop_reason`.

### 3.3 Hobby activity

Hobbies reuse **`blog_tags.hobby_status`** (no new column) narrowed to two values:
`HOBBY_ACTIVITIES = ("active", "inactive")`. Set **by hand**, never computed. Migration: `dormant` and `abandoned` ->
`inactive`, `NULL` on an `is_hobby=1` row -> `active`. The old value is preserved in the change log row the migration
writes (3.13), not in a column. `core.db.HOBBY_STATUSES` becomes `("active", "inactive")`; `set_hobby_status` and the
MCP tool accept `dormant`/`abandoned` as deprecated aliases for `inactive` (result carries a `warnings` entry) for one
release, so older scripts do not break.

Mismatch flags are **computed, never stored** (`db.hobby_flags(tag_id) -> list[{code, detail}]`):

- `inactive_with_active_work`: hobby is `inactive` and at least one member project (via `project_hobbies`) has
  `activity='active'`. `detail` lists up to 5 titles.
- `active_untouched`: hobby is `active` and its **last touched** date is older than 730 days. Last touched = max over
  (a) the real-world end of each member project (`timeline.resolve_project_span` end, so curation edits that bump
  `updated_at` cannot mask staleness) and (b) the effective date of any file tagged with the hobby tag
  (`timeline.resolve_item_date`). A hobby with no dated members and no members is **not** flagged (nothing to measure).

Flags surface on the hobby card/page and in `list_needs_decision` (section 6). The 730-day threshold is a module
constant (`HOBBY_STALE_DAYS`).

### 3.4 Whereabouts

```
ALTER TABLE projects ADD COLUMN whereabouts      TEXT   -- NULL = not recorded / not applicable
ALTER TABLE projects ADD COLUMN whereabouts_note TEXT
```

`WHEREABOUTS = (have_it, partial, parted_out, sold, gifted, lost, never_built)`. Applicable kinds: `thing`,
`project`, `collection` (a nested *project* such as the Skyhawk conversion parts is legitimately `partial`). Not
applicable to `action`, `event`, `family`. `NULL` is the default and means "not recorded"; `list_needs_decision`
reports a `thing` with `NULL` whereabouts as a computed need, but nothing blocks on it. Cross-rules are in 3.2 (5).

### 3.5 Card-level provenance

```
ALTER TABLE projects ADD COLUMN provenance        TEXT   -- created|found|collected|referenced|client_owned, or NULL
ALTER TABLE projects ADD COLUMN provenance_credit TEXT   -- who designed it / where it came from
```

One value per card. **Mixed origins are split into separate cards** (`split_card`, worked example in 7.4); a card is
never "found + own".

Relation to the existing per-file value (`capture_events.provenance`, unchanged, no data migration):

| File value (v1) | Meaning on an asset card |
|---|---|
| `found` | same as card `found` |
| `created` | same as card `created` |
| `reference` | shown as "Referenced" (same meaning as card `referenced`) |
| `result`, `design` | shown as "Created" (output of / planning for the owner's own work) |
| `documented` | **file-only**: the file is a record (photo, scan) *of* something, not an origin. Shown as "Documented". The Skyhawk photos are all `documented` |
| `NULL` | the asset card **inherits the owning card's provenance for display only**; nothing is written to the file |

`card_rules.file_provenance_label(value)` implements the table. The card vocabulary is deliberately smaller than the
file vocabulary (a card has an origin; a file may be evidence). `collected` and `client_owned` exist only at card level.
`PROVENANCE_TYPES` (file) is not changed, so `constructicon_set_provenance`, the object-detail dropdown, the Curator
nudges and the static export keep working. The new card tool is `constructicon_set_card_provenance` to avoid
overloading the file tool.

A computed suggestion for an unset card provenance is the majority file provenance mapped through the table (>50% of
non-write-up files); it is offered in `list_needs_decision`, never auto-applied.

### 3.6 Families and collections

```
CREATE TABLE IF NOT EXISTS family_members (
    family_id  INTEGER NOT NULL REFERENCES projects(id),
    member_id  INTEGER NOT NULL REFERENCES projects(id),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    PRIMARY KEY (family_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_family_members_member ON family_members(member_id);
```

A family is a `projects` row with `kind='family'`. The same table serves `kind='collection'` ("group kinds"); the name
stays `family_members` to match #480's tool names. Rules (`card_rules.validate_membership`):

- `family_id` must have a group kind; `member_id` must exist, differ from `family_id`, and **not** be a group kind
  (flat, one level; no family-in-family in v2).
- Many-to-many: a member can be in several families; a family has many members. Idempotent insert.
- Membership does not move or copy files; it is not nesting. Nesting parents and family membership are independent.
- Deleting a card removes its membership rows (extend `db.delete_project`); deleting a family leaves members intact.

### 3.7 Nesting ("part of")

Existing `projects.parent_id`, semantics narrowed to **"part of" only**: the child only makes sense inside its parent
(Clodapede Lua inside Clod-a-Pede). Rules in `card_rules.validate_nest(child, parent, descendants_of_child)`:

1. Single parent (the column already guarantees it; `nest` on a child that has one is an error unless `replace=True`).
2. No self-parent; no cycles (reuse `db._descendant_project_ids`; today only `update_project` checks this, and
   `create_project(parent_id=...)` does not).
3. Parent kind must not be a group kind (`family`, `collection`); child kind must not be a group kind.
4. Parent may be project, thing, action or event.

`db.update_project(parent_id=...)` and `db.create_project(parent_id=...)` are changed to call these rules, so the
existing web routes (`/api/projects`, `/api/projects/{id}`) inherit them. v1's `list_child_projects`,
`list_project_ancestors` and `cover_project_id` borrowing keep working unchanged.

### 3.8 Typed links

`project_relations` currently has PK `(slug_a, slug_b)` and no type. SQLite cannot change a PK in place, so the
migration **rebuilds the table** (idempotent: skipped when `PRAGMA table_info(project_relations)` already has `type`):

```
CREATE TABLE project_relations_new (
    slug_a TEXT NOT NULL, slug_b TEXT NOT NULL,
    type   TEXT NOT NULL DEFAULT 'related',
    note   TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (slug_a, slug_b, type)
);
INSERT INTO project_relations_new (slug_a, slug_b, type, created_at)
    SELECT slug_a, slug_b, 'related', created_at FROM project_relations;
DROP TABLE project_relations; ALTER TABLE project_relations_new RENAME TO project_relations;
```

(single transaction; existing rows become `related`, both directions preserved so `list_related_projects` is unchanged.)

Types and **directionality**. A row `(a, b, type)` reads "**a** `<type>` **b**":

| Type | Reads | Example | Stored as |
|---|---|---|---|
| `built_for` | a was built for b | canopy `built_for` The Queen | one row |
| `applies_to` | a is applied to b | Clodapede Lua `applies_to` Clod-a-Pede | one row |
| `used_in` | a is used in b | a motor `used_in` a quad | one row |
| `inspired_by` | a was inspired by b | a clone `inspired_by` the original | one row |
| `related` | symmetric | | **two rows**, (a,b) and (b,a), exactly as v1 |

From the target's side, directed links read in reverse (labels in `card_rules.LINK_LABELS`: forward / reverse):
Built for / Made for this; Applies to / Applied here; Used in / Uses; Inspired by / Inspired; Related / Related.
`list_links(card)` returns both directions with a `direction` field (`out` | `in` | `both`).

Rules (`card_rules.validate_link`): no self-link; both slugs exist; neither end is a `family`/`collection` for
`built_for`/`applies_to`/`used_in` as the *source*; two cards may carry several links of different types, but
**`related` cannot coexist with a typed link on the same pair**: adding a typed link deletes the pair's `related`
rows (an upgrade), and adding `related` where a typed link exists is refused. `retype_link(a, b, from_type, to_type,
direction)` is the single upgrade/downgrade path; it deletes the old rows and inserts the new in one batch. Bulk retype
(7.3) is the planned way to clear v1's untyped `related` links.

Object-level `capture_event_relations` (file-to-file) is **not** typed in this build.

### 3.9 Hobby membership and group codes

`project_hobbies` is already many-to-many; nothing changes structurally. Add the card's visible hobby code:

```
ALTER TABLE blog_tags ADD COLUMN group_code TEXT   -- 2-4 chars, hobbies only
```

Default derivation (run in migration for `is_hobby=1 AND group_code IS NULL`, and in `mark_tag_as_hobby`): multi-word
name -> initials, uppercased, max 4 ("R/C Adventures" -> RCA, "3D Printing" -> 3DP); single word -> first 3 letters
uppercased (Collecting -> COL). Collisions get the next letters of the first word, then a digit. Owner-editable.
A card shows its hobbies' codes in `project_hobbies.rowid` order (so "COL · 3DP · RCA" follows attach order).

### 3.10 Home (breadcrumb / export default plus manual override)

```
ALTER TABLE projects ADD COLUMN home_kind TEXT      -- 'card' | 'hobby' | NULL (automatic)
ALTER TABLE projects ADD COLUMN home_ref  INTEGER   -- projects.id or blog_tags.id
```

`cards.resolve_home(card)` returns `{type, id, slug, title, source: 'override'|'parent'|'family'|'hobby'|'none'}`:

1. valid manual override (`home_kind`/`home_ref` set and target exists) -> that;
2. else `parent_id`;
3. else the card's earliest `family_members` row (`created_at`, then `rowid`);
4. else the card's earliest `project_hobbies` row (`rowid`);
5. else none (the home page).

Used for the detail breadcrumb (home chain, then up through its own home) and as the export link target (follow-up).
A dangling override (target deleted) silently falls through to the automatic default and is reported in `explain_card`.
Home is "not something to curate": nothing prompts for it; `set_home` exists for the rare correction.

### 3.11 Curation level (pips 0-5), computed, not stored

`core/card_level.py: card_level(card, items, ...) -> {score: 0..5, checks: {cover, dates, writeup, owner_words, stack}}`.
One point each:

| Pip | True when |
|---|---|
| cover | `db.resolve_project_cover_slug(card)` is not None |
| dates | the card has **both** `start_date_override` and `end_date_override`, **or** it has >=1 non-write-up file and >=80% of non-write-up files satisfy `timeline.has_real_date` (so `source_modified_at` counts, per #406) |
| write-up | `writeup_slug` set **and** the document's `type_metadata.body` has >=200 non-whitespace characters. (`create_project` auto-creates a *blank* write-up for every project, #423, so existence alone proves nothing) |
| owner words | the write-up document's `type_metadata.owner_words` is true. This is an **explicit marker** set by `constructicon_set_project_writeup(..., owner_words=True)` / a checkbox on the write-up editor when the owner's own wording went in (the oral-history flow). Pragmatic choice: nothing in v1 can tell the owner's words from Claude's, and heuristics (blockquotes, description length) would false-positive. **Open to revision**, see 11 |
| full stack | by kind: project/thing/action: non-write-up files span >=3 distinct `media_type`s; family/collection: >=3 members; event: >=3 files |

Not stored; computed per request (the home page loads <100 cards). The existing Curator score (`core/curator.py`,
story/presentation/timeline/connections) is a separate, unchanged system; pips are not derived from it. Hobby cards and
asset cards show no pips.

### 3.12 Highlight

v1's highlight lives **only on files**: `capture_events.highlight INTEGER NOT NULL DEFAULT 0`, written by
`db.set_highlight`, the object-detail checkbox (`/api/image/{slug}` form field `highlight`), the MCP
`constructicon_set_highlight`, and nudged by the Curator `add_highlight` need ("cool/unique objects, featured in
export"). Cards reuse the same concept and name:

```
ALTER TABLE projects ADD COLUMN highlight INTEGER NOT NULL DEFAULT 0
```

Semantics are identical (a manual 0/1 "this one is special" flag; it also biases the featured-card choice on home,
section 8.2). File highlight is untouched. New tool `constructicon_set_card_highlight(card, on)` (the existing tool's
`slug` is a file slug; keeping them separate avoids ambiguity). The card face shows a dot in the group-code row.

### 3.13 Change log (audit + undo)

#480 asks that every write "go to the existing audit log with before/after values" and that `undo` takes an audit
entry id. The existing `audit_log` cannot do that as it stands (HTTP-only, no values; MCP bypasses it). Decision:
**extend `audit_log`** with nullable columns, keep one log:

```
ALTER TABLE audit_log ADD COLUMN op         TEXT      -- 'set_status', 'split_card', 'migration', ...
ALTER TABLE audit_log ADD COLUMN actor      TEXT      -- 'owner-ui' | 'mcp' | 'migration'
ALTER TABLE audit_log ADD COLUMN batch_id   TEXT      -- groups the rows of one operation or one bulk call
ALTER TABLE audit_log ADD COLUMN mutations  TEXT      -- JSON, see below
ALTER TABLE audit_log ADD COLUMN undone_by  INTEGER   -- audit id of the undo that reversed this row
```

`mutations` is a list of **row images**: `{table, key, before: {...}|null, after: {...}|null}` (`before` null = insert,
`after` null = delete). Row images make undo generic across `projects`, `family_members`, `project_relations`,
`project_hobbies`, `project_items`, `blog_tags` and `pending_decisions`; `BLOB`/large columns (`embedding`) are never
imaged. Core operations write their rows through `core.changes.record(op, actor, mutations, batch_id)`;
direct HTTP callers keep getting the old request-log row (the new columns are NULL there). `list_recent_audit_logs`
already spreads `dict(row)`, so the new keys flow through unchanged.

Undo (`cards.undo(audit_id | batch_id, force=False)`): re-applies the inverse of each mutation in reverse order inside
one transaction, writing its own log row (so an undo is itself undoable). It refuses if any current row no longer
equals the recorded `after` (someone changed it since) unless `force=True`; refuses rows with `actor='migration'`; and a
created card's auto-made blank write-up document is deleted with it only if still blank. The migration steps in section
4 write `actor='migration'` rows so the log shows what was changed, but they are **not undoable** (restore from the
ZFS pre-deploy snapshot instead, see CLAUDE.md).

---

## 4. Migration from v1

### 4.1 Principles

- **Where**: `core.db.init_db()`; each piece adds one `_migrate_v2c_N(conn)` called after the column guards. Runs on
  constructicon-test only until the owner decides otherwise ("migrations run only there").
- **Idempotent**: columns via `PRAGMA table_info`; data steps guarded by `stage IS NULL` (cards), `type` column present
  (links), `group_code IS NULL` (hobbies), and an `app_settings['card_migrations']` JSON list of completed step ids for
  steps with no natural marker. Decisions are queued with `db.queue_decision_once(kind, post_slug, payload)`, which
  skips if **any** row (open or resolved) of that kind+slug exists, so re-running never re-asks a question the owner
  already answered. (The existing `add_pending_decision` only dedupes *unresolved* rows.)
- **Legacy `projects.status` is frozen, not rewritten.** `core/site_export.py` calls `db.list_projects(status="active")`
  and the export must keep working untouched, so the column and `PROJECT_STATUSES` stay as they are, kept as the
  pre-migration record. New cards still get `status='active'` from `create_project`'s default (the export's current
  behaviour). The *live* consumers move to `stage`: the home partition, `core/curator.py`, `core/curator_needs.py`
  (via `card_rules.curator_status(card)`: in_progress->wip, in_use/done->complete, paused->shelved, idea->idea,
  stopped+failed->failed, stopped+abandoned->abandoned, collection+referenced->reference-only), `web/app.py`
  (reference filter), the project-detail status control, `_project_tile.html`, and the MCP `set_project_status`
  (kept as an alias of `set_status` with legacy-value translation).
- **Provisional values.** A queued decision never leaves the card blank: the card gets the **least-claiming** valid
  value immediately (so filters and pages work), the decision records exactly what was applied, and the card is
  visibly marked "needs your input" until answered. Provisional is not "the guess": the suggested answer is separate
  and may differ.
- Every migration write is logged as an `actor='migration'` audit row (3.13).

### 4.2 Status mapping (exact)

| v1 `status` | Count | `kind` | activity / stage / reason | How |
|---|---:|---|---|---|
| `wip` | 5 | project | active / `in_progress` | automatic |
| `active` | 1 | project | active / `in_progress` | automatic (treated as `wip` everywhere in v1) |
| `abandoned` | 11 | project | inactive / `stopped` / `abandoned` | automatic |
| `failed` | 1 | project | inactive / `stopped` / `failed` | automatic |
| `idea` | 0 | project | inactive / `idea` | automatic |
| `published` | 0 | project | inactive / `done` | automatic (v1 treats it like `complete`; the Curator counts it as live) |
| `reference-only` | 1 | **collection**, `provenance='referenced'` | active / `in_use` | automatic (owner decision on #428: reference-only becomes a Collection) |
| `complete` | 41 | project | **provisional** inactive / `done` | **queued** `card_status`: done or in use? |
| `archived` | 1 | project | **provisional** inactive / `done` | **queued** `card_status` (v1 already treats `archived` as `complete`) |
| `shelved` | 6 | project | **provisional** inactive / `paused` | **queued** `card_status`: paused build, or a collection? |
| `means-to-an-end` | 4 | project | **provisional** inactive / `done` | **queued** `card_built_for`: which card was it built for, or is it an event? |
| any other value | 0 now | project | **provisional** inactive / `paused` | **queued** `card_status` with `legacy_status` shown and a free choice |

Plus, independent of status: **kind** review (4.4) and **family** review (4.5). Hobby and link migrations are 4.6, 4.7.

### 4.3 New `pending_decisions.kind` values and payload shapes

Existing pattern (read from `core/decisions.py` / `core/automatch.py`): `kind` string, `post_slug`, a JSON `payload`,
`resolved_at`, and the owner's answer stored in `payload.resolution`. `project_match` carries
`{"candidate_project_ids": [...], "matched_text": "..."}`; `retype` carries `{"question", "options": [{"key","label"}]}`.
The card kinds follow the `retype` shape (options with keys) so the existing "Needs your input" UI and resolve route
can render them with one new branch, plus declarative `patch` lists so resolving applies the **same validated core
operations** a manual edit would.

`post_slug` convention: **`card:<project slug>`** (the column is NOT NULL and today means "a capture_events slug").
Gotcha, and a required change in piece 1: `core.decisions.list_open()` calls `db.get_by_slug(post_slug)` and marks the
decision **stale and resolved** when no file row exists, which would silently destroy every card decision on first
page load. `list_open` must branch on the `card:` prefix and validate against `projects` instead. Same for the web
route `web/app.py` (`api_resolve_pending_decision`) and `constructicon_list_pending_decisions` / `_resolve_`.

Common envelope (schema version 1):

```json
{
  "schema": 1,
  "card_id": 12, "card_slug": "alienwhoop", "title": "AlienWhoop",
  "field": "stage",
  "question": "Is this finished and still in use, or finished and done?",
  "legacy_status": "complete",
  "provisional": {"op": "set_status", "stage": "done"},
  "suggested": "done", "suggested_reason": "No owned-object signals found", "confidence": "low",
  "options": [
    {"key": "in_use", "label": "In use", "patch": [{"op": "set_status", "stage": "in_use"}]},
    {"key": "done",   "label": "Done",   "patch": [{"op": "set_status", "stage": "done"}]}
  ]
}
```

Resolution (`payload.resolution`): `{"choice": "in_use" | ["slug", ...], "applied_batch": "<audit batch_id>",
"by": "owner" | "claude", "at": <epoch>}`. Resolving through `cards.resolve_decision(id, choice|choices, actor)` runs each
option's `patch` ops through the normal validators, in one batch; if a patch is invalid on today's data, the decision
stays open and the error is returned. Resolved rows are kept.

| `kind` | Question | Options | Choice type |
|---|---|---|---|
| `card_status` | complete/archived: done vs in use. shelved: paused vs collection. unknown legacy: free stage pick | `done`, `in_use` / `paused`, `collection` (patch: `set_kind collection` + `set_status in_use`) / the six stages | single |
| `card_built_for` | means-to-an-end: which card(s) was it built for | one option per candidate `{key: <slug>, patch: [link built_for -> slug]}`, plus `is_event` (patch `set_kind event`), `none` | multi (candidates), single (`is_event`/`none`) |
| `card_kind` | one-object project: Thing or Project (also action, collection, event) | the six kinds | single |
| `card_family_members` | AlienWhoop-style family: which cards belong | one option per candidate `{key: <slug>, patch: [add_to_family; and unnest if currently nested under the family]}` | multi |

Suggestion heuristics (low-cost, deterministic, logged in `suggested_reason`; never applied):

- `card_status` complete: suggest `in_use` when the card has >=1 `capture_events.provenance in ('found','collected')`
  file **or** its title/description matches `\b(own|owned|bought|using|use|carry|daily)\b`, or it sits in a
  hobby named like a collecting hobby; else `done`. Confidence `low`/`medium`; the owner described the in-use cases
  (knives, AT9 radio, arcade cabinets) as "owned/bought", which is what these signals approximate.
- `card_status` shelved: suggest `collection` when the card's newest file is older than 2 years **and** it has no
  nested children in progress (nothing suggests it will resume); otherwise `paused`. Always low confidence (6 rows).
- `card_built_for` candidates, ranked: existing `project_relations` neighbours of the card; cards sharing a hobby with
  it; cards whose title appears in its description/write-up; each with a `reason`. Always offers `is_event` (one of the
  four is a field trip) and `none`.
- `card_kind`: suggest `thing` for a leaf card (no children, not a family parent) whose title/description contain no
  software/system words (`code|script|software|firmware|app|site|system|design|tool|lua|library`); `project`
  otherwise. **One decision per leaf project not already resolved** by 4.2/4.5; non-leaf cards are left `project`
  silently (they are containers). Provisional value: `project`.

Expected queue on a fresh prod copy: ~41+1 `card_status` (complete/archived) + 6 (shelved) + 4 `card_built_for` +
up to ~60 `card_kind` + the family set. `constructicon_resolve_decisions` (7.3) accepts suggested answers in bulk with a
dry-run so the owner can approve in sweeps.

### 4.4 Kind migration

All rows get `kind='project'` from the column default, except `reference-only` -> `collection` (4.2). The rest of the
kind assignment is the `card_kind` decisions above (one-object builds are **Things**, per the owner decision; purple
Project stays for families' parents-of-nested, design-only and system work).

### 4.5 Families (AlienWhoop)

Nesting that was faking a family becomes a real family. The migration does **not** hard-code ids. Step
`v2c_3_alienwhoop`: find the project titled exactly `AlienWhoop` (case-insensitive); if absent the step is a no-op
(re-runs once it exists). If found, queue **one** `card_family_members` decision on it with candidates:

- every current child (`parent_id = AlienWhoop.id`), suggested **yes**, reason "currently nested";
- any project titled `TinyWhoop`, `AlienWhoop V2 F4`, `AlienWhoop Zer0` that exists and is not already a child,
  suggested **yes**, confidence low, reason "named like a sibling; the archive suggests separate builds". (#428 flagged
  these three as candidates for owner confirmation.)

Provisional state: unchanged (nesting stays until answered). On resolve, `AlienWhoop` becomes `kind='family'` if it is
not already, each chosen candidate is added to `family_members`, and chosen candidates currently nested under it are
**unnested** (their `parent_id` cleared) in the same batch. The canopy's `built_for` link to the quad(s) is a separate
`card_built_for` question queued for the card titled like `AW canopy*` if present. **Clod-a-Pede -> Clodapede Lua stays
nested** ("part of"); no decision is queued for it, but it is also queued **no** `card_family_members`. The
`applies_to` link Lua -> Clod-a-Pede is a *suggested* typed link surfaced by `list_needs_decision` once piece 4 exists.

### 4.6 Hobbies

`blog_tags` rows with `is_hobby=1`: `hobby_status` `dormant`/`abandoned` -> `inactive`; `NULL` -> `active`;
`group_code` filled (3.9). Automatic; no decisions (the owner confirmed "definitely not FPV'ing anymore" is a manual
switch, so nothing is flipped for them; the mismatch flags in 3.3 will point at the obvious ones).

### 4.7 Links, provenance, whereabouts, home, highlight

- Links: rebuilt table, all existing rows `related` (3.8). No auto-retype. The `means-to-an-end` questions (4.3) are the
  only queued link work; the rest is offered as `untyped_link` needs and the bulk retype tool.
- Card provenance, whereabouts, home, highlight, credit: new nullable/default columns, **empty** after migration (only
  `reference-only`'s collection gets `provenance='referenced'`). Nothing is guessed; computed suggestions appear in
  `list_needs_decision`.

### 4.8 What happens to code that reads v1 fields

| Consumer | Change |
|---|---|
| `home.html` active/done partition (`p.status === 'wip' \|\| 'active'`) | partition on `activity` |
| `_project_tile.html`, `project_detail.html` status pill/select | read `stage` + `stop_reason`; select replaced by kind/activity/stage/reason controls |
| `core/curator.py`, `core/curator_needs.py` | via `curator_status` adapter (4.1) |
| `web/app.py` reference filter (`status == 'reference-only'`) | `kind == 'collection' and provenance == 'referenced'` |
| `core/site_export.py`, `core/project_export.py` | **unchanged**; read frozen legacy `status` |
| `constructicon_set_project_status` | alias of `set_status`, accepts legacy words, returns `warnings` when translating |
| `HOBBY_STATUSES` consumers (`hobby.html`, `hobbies.html`, `_hobbies_drawer.html`, MCP) | two-value switch, deprecated aliases (3.3) |

---

## 5. Validation summary (all in `core/card_rules.py`)

| Function | Raises `CardError` code |
|---|---|
| `validate_kind(kind)` | `bad_kind` |
| `validate_status(...)` (3.2) | `bad_status` |
| `validate_whereabouts(kind, value, stage)` (3.4, 3.2 rule 5) | `bad_whereabouts` |
| `validate_provenance(value)` | `bad_provenance` |
| `validate_nest(child, parent, descendants)` (3.7) | `nest_cycle`, `nest_second_parent`, `nest_group_kind`, `nest_self` |
| `validate_membership(family, member)` (3.6) | `bad_membership` |
| `validate_link(a, b, type, existing)` (3.8) | `bad_link`, `link_conflict` |
| `validate_hobby_activity(value)` | `bad_hobby_activity` |

`CardError(code, message, details)` is the only exception type core operations raise for rule violations. The web
routes map it to HTTP 422 (409 for `*_conflict`/`nest_*`); the MCP maps it to `{"ok": false, "error": {code, message}}`.
No rule is implemented only in `mcp_server/` or only in a template.

---

## 6. Core operations (`core/cards.py`)

Every function takes `card` as a project id or slug, runs the validators, writes inside one transaction, records a
change-log row (3.13), and returns `Result(ok, changes, warnings, batch_id)` where `changes` is a list of
`{card, field, before, after}`. All accept `dry_run: bool = False` (compute and return `changes`, write nothing).
`actor` identifies the caller (`'owner-ui'`, `'mcp'`).

```
set_kind(card, kind, *, force=False)
set_status(card, stage, stop_reason=None, *, activity=None)
set_whereabouts(card, value | None, note=None)
set_card_provenance(card, value | None, credit=None)
set_home(card, target | None)                         # target: card ref, hobby ref, or None to go automatic
set_card_highlight(card, on)
nest(child, parent, *, replace=False)   unnest(child)
add_to_family(family, member)           remove_from_family(family, member)
add_to_hobby(card, hobby)               remove_from_hobby(card, hobby)
set_hobby_activity(hobby, value)
link(a, b, type, note="")               unlink(a, b, type=None)             retype_link(a, b, from_type, to_type)
move_files(slugs, from_card, to_card)   copy_files(slugs, from_card, to_card)   # files are many-to-many
split_card(source, parts, *, keep_in_source=False)
merge_cards(keep, absorb: list)
resolve_decision(decision_id, choice | choices)
explain_card(card) -> dict
list_needs_decision(filters) -> list[dict]
hobby_flags(hobby) -> list[dict]
bulk(op, items: list[{card, args}], *, dry_run=True)
undo(audit_or_batch_id, *, force=False)
```

`split_card(source, parts)`: each part is
`{title, kind, provenance, provenance_credit, stage, stop_reason, whereabouts, relation: 'child'|'sibling',
file_slugs: [...], link_to_source: {type}|None, hobbies: 'inherit'|[...]}`. A `child` part is created nested under the
source (parent rules apply); a `sibling` part gets the **source's hobbies and family memberships copied** (many-to-many
makes this natural) and no parent. Files named in `file_slugs` are **moved** out of the source (removed from its
`project_items`, added to the part) unless `keep_in_source=True` (then copied). Every part is a full card, including
the blank write-up `create_project` always makes. One transaction, one `batch_id`; undo removes the parts and restores
the source's items.

`merge_cards(keep, absorb)`: files unioned into `keep` (deduped); hobbies and family memberships unioned; children of
absorbed cards re-parented to `keep` (validated); links re-pointed to `keep`, self-links and duplicates dropped; blog
entry attachments (`blog_entry_projects`) re-pointed; `keep`'s cover and write-up win; an absorbed card's non-blank
write-up document is kept as an ordinary file in `keep`; open decisions on absorbed cards are resolved as stale;
absorbed rows deleted (row images in the log make this undoable). Refuses when it would create a nest cycle or merge a
group kind with a non-group kind.

`bulk(op, items, dry_run=True)`: `op` is allow-listed to `set_status, set_kind, set_whereabouts, set_card_provenance,
set_home, set_card_highlight, add_to_hobby, remove_from_hobby, add_to_family, remove_from_family, nest, unnest, link,
unlink, retype_link, set_hobby_activity`. **Dry-run by default**; returns every item's `changes`/`error`, validates all
before writing any (all-or-nothing in one transaction, one `batch_id`; `partial_ok=True` applies the valid ones and
reports the rest).

`explain_card(card)` returns, in one call: identity (`id, slug, title, kind`), `activity/stage/stop_reason`,
`provisional` (bool, from open decisions), `whereabouts(+note)`, `provenance(+credit)`, `highlight`, `hobbies` (with
codes and activity), `families`, `members` (group kinds), `parent`, `children`, `links` (typed, with direction),
`home` (resolved + source + dangling-override flag), `files` (counts per `media_type`, date span), `level`
(pips + reasons), `open_decisions`, `warnings` (e.g. active child under inactive parent), `suggestions` (computed
provenance, suggested typed links), and `recent_changes` (last 5 audit rows). This is the call Claude reads before
proposing an edit.

`list_needs_decision(filters)` merges two sources into one list sorted by card:

1. **stored**: open `pending_decisions` of kinds `card_*` (4.3), with their options and suggested answer;
2. **computed** (never stored, always current): `missing_provenance` (suggestion from file majority),
   `missing_whereabouts` (kind thing), `untyped_link` (a `related` pair with a plausible typed reading),
   `hobby_inactive_with_active_work`, `hobby_active_untouched`, `status_conflict` (3.2 warnings),
   `blank_writeup_with_files`.

Each row: `{need, card_slug, title, detail, suggested|null, decision_id|null}`. Filters: `kind`, `need`, `hobby`,
`limit`. (The existing `constructicon_list_needs` is the Curator's file/project nudge list and is unrelated; the
existing `constructicon_list_pending_decisions` keeps listing stored decisions and learns the `card_*` kinds.)

---

## 7. MCP tools

Names keep the `constructicon_*` prefix (#481 default); new tools are **added** to the existing server, nothing
removed. Every tool is a thin wrapper over the `core/cards.py` function of the same name; none contains a rule.
Single-card tools **apply by default** and accept an optional `dry_run: bool = False`. **Bulk tools default to dry-run**
(answers #480's open question: dry-run default for bulk only). Return shape: `{"ok", "dry_run", "changes", "warnings",
"batch_id", "error"?}`.

### 7.1 Reshaping

| Tool | Core fn |
|---|---|
| `constructicon_split_card(source, parts, keep_in_source=False, dry_run=False)` | `split_card` |
| `constructicon_merge_cards(keep, absorb, dry_run=False)` | `merge_cards` |
| `constructicon_set_kind(card, kind, force=False)` | `set_kind` |
| `constructicon_nest(child, parent, replace=False)` / `constructicon_unnest(child)` | `nest` / `unnest` |
| `constructicon_move_files(slugs, from_card, to_card)` / `constructicon_copy_files(...)` | `move_files` / `copy_files` |

### 7.2 Classifying

| Tool | Core fn |
|---|---|
| `constructicon_set_status(card, stage, stop_reason=None)` | `set_status` |
| `constructicon_set_whereabouts(card, value, note=None)` | `set_whereabouts` |
| `constructicon_set_card_provenance(card, value, credit=None)` | `set_card_provenance` |
| `constructicon_set_home(card, target=None)` | `set_home` |
| `constructicon_set_card_highlight(card, on)` | `set_card_highlight` |
| `constructicon_set_hobby_status(hobby, status)` (existing; now `active`/`inactive`) | `set_hobby_activity` |

### 7.3 Membership, links, curation at scale, safety

| Tool | Core fn |
|---|---|
| `constructicon_add_to_hobby` / `constructicon_remove_from_hobby` | `add_to_hobby` / `remove_from_hobby` (existing `constructicon_add_project_to_hobby` stays as a deprecated alias) |
| `constructicon_add_to_family` / `constructicon_remove_from_family` | same-named |
| `constructicon_link(a, b, type, note)` / `constructicon_unlink(a, b, type)` / `constructicon_retype_link(a, b, from_type, to_type)` | same-named |
| `constructicon_retype_links(mapping, dry_run=True)` | bulk retype of v1 `related` links: `mapping` is a list of `{a, b, to_type}` |
| `constructicon_list_needs_decision(kind, need, hobby, limit)` | `list_needs_decision` |
| `constructicon_explain_card(card)` | `explain_card` |
| `constructicon_resolve_decisions(items, accept_suggested=False, dry_run=True)` | `resolve_decision`, bulk; `items` is `[{decision_id, choice}]` or, with `accept_suggested`, a list of ids |
| `constructicon_bulk_edit(op, items, dry_run=True, partial_ok=False)` | `bulk`; one allow-listed dispatcher instead of ~15 near-identical `bulk_*` tools, so there is one code path to keep correct |
| `constructicon_undo(audit_id, force=False)` | `undo` (accepts a `batch_id` too) |
| `constructicon_list_changes(card=None, batch_id=None, limit=50)` | reads `audit_log` rows with `mutations` |

Existing tools extended (additive params, same names): `constructicon_create_project(kind, stage, ...)`,
`constructicon_update_project`, `constructicon_get_project` / `constructicon_list_projects` (return the new fields;
`list_projects` gains `kind`/`activity`/`stage` filters), `constructicon_set_project_status` (alias, 4.8),
`constructicon_list_pending_decisions` / `constructicon_resolve_pending_decision` (learn `card_*`).

### 7.4 Worked example: Custom GI Joe Skyhawk (from #428/#480)

Today: one flat project, GI Joe hobby only, every photo `documented`. Target, as one batch:

```
constructicon_split_card(
  source="custom-gi-joe-skyhawk",
  parts=[
    {title: "Skyhawk model", kind: "thing", relation: "sibling", provenance: "found",
     provenance_credit: "<designer / source: ASK OWNER>", stage: "done", whereabouts: "have_it",
     file_slugs: [<the model photos>], hobbies: "inherit"},
    {title: "Skyhawk conversion parts", kind: "project", relation: "child", provenance: "created",
     stage: "done", whereabouts: "partial", link_to_source: null,
     file_slugs: [<the parts photos>]}
  ])
constructicon_link(a="skyhawk-conversion-parts", b="skyhawk-model", type="applies_to")
constructicon_add_to_hobby(card="custom-gi-joe-skyhawk", hobby="3d-printing")
```

Result: the source is a Project in GI Joe + 3D printing; the model is a Thing (Found); the conversion parts are a nested
Project (Created, whereabouts Partial) that applies to the model. The model's designer/source and the parts photo are
placeholders the owner supplies (queued as computed needs: `missing_provenance` credit).

---

## 8. Display (v1 layout kept)

The owner: "the current layout is pretty close to what we want." Home and detail keep their structure; what changes is
the card component, the stack/fan pattern, and the date range under every name. Chronology is central: **every card
shows its date range in large type under the name**, and the stat box carries **counts only**.

### 8.1 Card component

One renderer, `web/static/js/cards.js` (`CardView.render(cardJson, {size: 'full'|'regular'|'compact'})`) plus
`web/static/css/cards.css`, because home already builds its tiles client-side from `/api/projects` JSON; the detail page
reuses the same function. JSON comes from a new `core/cards.py: card_json(card)` (and `GET /api/cards/{slug}`),
extended into `/api/projects`. Zones, top to bottom:

| Zone | Content | Source |
|---|---|---|
| Name bar | title (kind icon at the right edge) | `title`, `kind` |
| Date range | **large type**: `Nov 2025 - Jul 2026`; single moment `Jul 2026`; active cards with a past end `Nov 2025 - now` | `timeline.resolve_project_span`; `now` iff `activity='active'` |
| Pips | five dots, filled = score | `card_level` (3.11); hidden on hobby and asset cards |
| Art | cover thumbnail; else a kind-coloured placeholder with the kind icon | `resolve_project_cover_slug` |
| Type line | `<Kind> - <home hobby or family>` e.g. `Thing - R/C Adventures` | `kind`, `resolve_home` |
| Group codes + highlight dot | `COL . 3DP . RCA` (hobby codes in attach order), filled dot when highlighted | `group_code`, `highlight` |
| Facts box | up to 4 lines then `+N more`: whereabouts (+note), `Part of X`, `In family Y`, typed links (`Built for The Queen`) | 3.4-3.8 |
| Flavor line | optional, italic, first sentence of `description`, <=90 chars; omitted when empty | `description` |
| Status box | stage label with an activity dot (green active, grey inactive); `Stopped - failed`; a "needs your input" mark while a decision is open | 3.2, 4.3 |
| Stat box | **counts only**: `files N`, `nested N`, `links N`; family/collection: `members N`; hobby: `projects N` | counts |
| Footer | provenance (+credit); right side `n of N` when the card is in exactly one family with an ordering | 3.5, 3.6 |

Hobby cards (from `blog_tags`) fill the same zones: name, date range over member projects, group code, hobby activity
in the status box, `projects N`, no pips/provenance. Asset cards (files) show: display name, the file's effective date,
thumbnail, type line = media type label, provenance footer (own, else inherited, 3.5), no pips or stat box.

**Frame colour by kind** (CSS custom properties `--card-frame-<kind>`, white text on the frame):

| Kind | Hex | Note |
|---|---|---|
| project | `#563A86` | specified |
| thing | `#22508A` | specified |
| action | `#952C29` | specified |
| hobby | `#356640` | specified |
| asset | `#4F5862` | specified |
| family | `#7A3F70` | chosen: plum, a relative of project purple (umbrella over related builds) |
| collection | `#8A6A1F` | chosen: ochre (a kept set) |
| event | `#2F7F86` | chosen: teal (one-off occurrence), distinct from hobby green |

Colour is never the only carrier of meaning: the kind icon and the type line say it too. Sizes: **full** (featured,
~320 px wide), **regular** (~220), **compact** (~150; drops flavor line, facts box and footer).

### 8.2 Home (`home.html`, layout kept)

Keep: the page header, the `home-widgets` columns, the hobby pills, the Projects/Files widgets, the vertical
**timeline rail** (every year shown including gaps; `#gallery-timeline-rail`, `timeline-rail.js`). Changes in the
Projects widget:

- **Active zone** (`activity = 'active'`): the **featured** card at full size = highest `highlight`, then most recent
  real end date (not `updated_at`); remaining Active cards at regular size beside it (replaces `.featured-project` +
  `.project-grid` markup with cards).
- **Inactive zone** (replaces "Finished / Archived"; `activity = 'inactive'`): compact cards in the existing scroll row,
  stage visible in the status box.
- **Reference zone** (v1 `?ref=1`): collections with `provenance='referenced'`, plus the existing loose reference
  objects (asset cards).
- Top-level only (v1: `parent_id is None`), as today.
- **Group-by (card appears once, several badges):** the default view lists each card exactly once. When grouped by
  hobby, a card sits under its **home hobby** (`resolve_home` hobby, else its first-attached) and carries *all* its
  hobby codes as badges; it is not duplicated under each hobby. A hobby pill filter shows every member of that hobby
  (so a multi-hobby card appears in each filtered view, still one tile per view). **Family collapse:** in the
  unfiltered home, cards that belong to a family are represented by the family's card (`members N`) rather than
  repeating as tiles; a hobby-filtered view shows members individually with a family badge. (This is an assumption,
  see 11.)

### 8.3 Project detail (`project_detail.html`, layout kept)

Keep: title with inline rename, the breadcrumb (now the **home chain**: `resolve_home` walked upward), the hobby chips
and related-projects rows (the latter becomes the typed **links row**: chips grouped by type using forward/reverse
labels, with an add-link control that has a type select), the write-up block, the date-override controls, and the
**Project Timeline** (`project-video-timeline.js`): nested cards as blocks, files as points per month. Clod-a-Pede's
real files span Nov 2025 - Jul 2026 and are the test shape. Controls replace the old status select: kind, activity +
stage + stop reason (one control, rule-validated server-side), whereabouts (+note), provenance (+credit), home
override, parent select (the existing one, now rule-checked), family chips, highlight.

The item grid becomes **stacks and fans**:

- **One pile per file type** (`media_type`, label from the type registry): photos, 3D models, write-ups, and so on.
  A pile is real asset cards: the **top card flat**, **three more tilted underneath** (about -4, +3 and +6 degrees,
  offset a few px), a **count badge** on the top-right, and a **label** under it: `<type label> - <count> - <Mon YYYY -
  Mon YYYY>` (date span from `resolve_item_date`).
- **Click a pile** -> it **fans** into a tilted arc (up to 7 cards visible then `+N`); the label becomes "Show all N".
  **"Show all N"** (or clicking again) opens the **full grid** (the existing `project-item-grid`). Esc or clicking
  outside collapses.
- **Nested cards** (children via `parent_id`) render as regular cards **in the same row, beside the piles**; family and
  collection cards render their **members** the same way.
- Reference look: the owner singled out the Project-stack board ("in a good way"): the card on top, assets fanned under
  it at slight angles, then spread side by side. That is the feel to match.
- Accessibility: a pile is a `<button>` (Enter/Space toggles the fan, Esc closes); tilt/fan animation is disabled under
  `prefers-reduced-motion`; the count and span are text, not decoration.

---

## 9. Build pieces

Stacked branches: each starts from the previous piece's branch, and **each PR targets the previous branch** (piece 0
targets `main`), so diffs stay readable; merge in order. Only one thing uses constructicon-test at a time; it is
reseeded from a fresh prod copy before piece 1 (`scripts/seed_test_from_production.py`, check the `<title>` reads
`DEV-Constructicon` before running).

**Common verification recipe (every piece), per CLAUDE.md "Live-testing a branch":** tar `core web mcp_server scripts
assets` from the branch worktree to the constructicon-test checkout, `sudo docker restart constructicon-test`
**and** `constructicon-test-mcp`, confirm `Application startup complete` with no traceback, then use
`docker exec constructicon-test python3 -c "import urllib.request ..."` against `http://localhost:80` (no curl in the
image) and the `mcp__constructicon-test-mcp__*` tools for MCP checks. Read DB state with plain `sqlite3.connect`
inside the container (not `?mode=ro`). Run `python3 scripts/check_object_types.py` in the container once per piece
(regression guard on the type registry). Re-run `init_db()` a second time and confirm zero row changes
(idempotency). Visual checks use fetched HTML/JSON and computed values from scripts; the owner reviews looks in their
own browser (the in-app Browser pane is used only on explicit invitation).

### Piece 0: `v2c-0-spec` (base: `main`)

This document. Acceptance: file at `docs/design/v2-cards.md`; PR closes #482; no code changes.

### Piece 1: `v2c-1-kind-status` (base: `v2c-0-spec`)

- **Scope:** `projects.kind`, `activity`, `stage`, `stop_reason`; `core/card_rules.py` (kinds, stages, status
  validation, `curator_status` adapter, `CardError`); `core/cards.py` with `set_status`, `set_kind`; the **change-log
  infrastructure** (`audit_log` columns + `core/changes.py`) so every later setter records row images from day one;
  migration `v2c_1`: automatic mapping (4.2) + queued `card_status`, `card_built_for` (decision queue only; resolving
  `built_for` links arrives with piece 4, until then the option patch is stored but resolution is limited to
  `is_event`/`none`), `card_kind`; `db.queue_decision_once`; `core/decisions.py` + `web/app.py` pending-decision
  routes + `constructicon_*_pending_decision(s)` learn `card_*` kinds with the `card:<slug>` convention and the
  `list_open` stale-check fix (4.3); curator adapters; home Active/Inactive partition; `_project_tile.html` and
  `project_detail.html` status/kind controls; MCP `constructicon_set_status`, `constructicon_set_kind`, extended
  `create_project`/`update_project`/`list_projects`/`get_project`, `set_project_status` alias; `list_needs_decision`
  (stored decisions only at this stage).
- **Files:** `core/db.py`, `core/card_rules.py` (new), `core/cards.py` (new), `core/changes.py` (new),
  `core/decisions.py`, `core/curator.py`, `core/curator_needs.py`, `web/app.py`, `web/templates/home.html`,
  `web/templates/_project_tile.html`, `web/templates/project_detail.html`, `web/templates/admin*.html` (Needs your input
  rendering), `mcp_server/server.py`.
- **Acceptance:** after `init_db()` on a prod copy, counts by `(kind, activity, stage, stop_reason)` match 4.2 exactly
  (wip+active 6 -> in_progress; abandoned 11; failed 1; reference-only 1 -> collection/in_use; the 52 judgment cards
  (41 complete, 1 archived, 6 shelved, 4 means-to-an-end) provisional inactive); every `complete`/`archived`/`shelved`/`means-to-an-end` card has exactly one open `card_*`
  decision with `suggested`; a second `init_db()` changes nothing and queues nothing new; resolving a decision then
  re-running `init_db()` does not re-queue it; invalid statuses (`idea`+active, `stopped` without reason, `in_use` +
  `sold`) are rejected by both HTTP and MCP with the same error code; the Curator score for an unchanged project is
  identical before and after; the static export still produces the same output; **the new decisions are still open
  after loading `/admin` and `/api/pending-decisions`** (the stale-cleanup regression test).
- **Live verify:** the recipe, then `explain`-style checks via `constructicon_get_project`, resolve one decision of each
  kind with `constructicon_resolve_pending_decision`, and confirm the home page JSON partitions by activity.

### Piece 2: `v2c-2-hobby-active` (base: `v2c-1-kind-status`)

- **Scope:** `HOBBY_STATUSES -> active|inactive` with deprecated aliases; migration `v2c_2` (dormant/abandoned ->
  inactive, NULL -> active, `blog_tags.group_code` derivation); `db.hobby_flags`; flags in the hobby page, the hobbies
  drawer/page and `list_needs_decision` (computed needs `hobby_inactive_with_active_work`, `hobby_active_untouched`);
  `set_hobby_activity` in `core/cards.py` + change log; MCP `constructicon_set_hobby_status` now two-valued,
  `constructicon_list_hobbies` returns `status`, `group_code`, `flags`.
- **Files:** `core/db.py`, `core/cards.py`, `core/card_rules.py`, `web/app.py`, `web/templates/hobby.html`,
  `hobbies.html`, `_hobbies_drawer.html`, `mcp_server/server.py`.
- **Acceptance:** no hobby retains `dormant`/`abandoned`; every hobby has a unique `group_code`; an inactive hobby with
  an `active` project reports the flag listing that project; an active hobby whose newest member end date is >730 days
  old reports `active_untouched`; setting a hobby back and forth never produces stored flag rows; `dormant` passed to
  the MCP tool maps to `inactive` with a warning.
- **Live verify:** list hobbies via MCP before/after; set one hobby inactive that has an active project and read the
  flag; cross-check the 730-day claim against `timeline.resolve_project_span` ends in the DB.

### Piece 3: `v2c-3-families` (base: `v2c-2-hobby-active`)

- **Scope:** `family_members` table; `kind=family|collection` rules; `add_to_family`/`remove_from_family` (core + MCP);
  nest rules (3.7) now enforced in `db.update_project`/`create_project` (so `parent_id` means "part of" only);
  migration `v2c_3_alienwhoop` (4.5) + `card_family_members` resolution (sets kind=family, adds members, unnests chosen
  nested children); family chips on detail, a members list on family/collection detail; `delete_project` cleans
  memberships.
- **Files:** `core/db.py`, `core/card_rules.py`, `core/cards.py`, `core/decisions.py`, `web/app.py`,
  `web/templates/project_detail.html`, `mcp_server/server.py`.
- **Acceptance:** AlienWhoop decision exists with current children as suggested-yes and the three named candidates
  (when they exist); resolving it yields a family with the chosen members and no remaining nest parent for them;
  Clod-a-Pede -> Lua nest is untouched; a nest cycle, a second parent, and a family-as-parent are each refused;
  a card can be in two families; adding the same member twice is a no-op; deleting a member card removes its rows.
- **Live verify:** resolve the AlienWhoop decision on test; read `constructicon_get_project` for the family and a
  member; try the three refusals through both `/api/projects/{id}` and the MCP.

### Piece 4: `v2c-4-typed-links` (base: `v2c-3-families`)

- **Scope:** `project_relations` rebuild (3.8), `link`/`unlink`/`retype_link`/bulk retype in core + MCP; `related` <->
  typed rules; `list_links` with directions and labels; the `card_built_for` resolution now creates real links
  (completing piece 1's queue); detail-page links row grouped by type with add-link control; computed need
  `untyped_link`; existing routes `/api/project/{slug}/related(+remove)` keep working (they create/delete `related`).
- **Files:** `core/db.py`, `core/card_rules.py`, `core/cards.py`, `core/decisions.py`, `web/app.py`,
  `web/templates/project_detail.html`, `mcp_server/server.py`.
- **Acceptance:** after migration every pre-existing link is `type='related'` in both directions and row count is
  unchanged; `list_related_projects` output unchanged; `link` built_for a->b writes one row and `list_links` shows it as
  `out` on a and `in` on b with the reverse label; adding a typed link removes the pair's `related` rows; adding
  `related` over a typed pair is refused; bulk retype dry-run prints before/after and writes nothing; the four
  `means-to-an-end` decisions can be resolved to a real `built_for` link.
- **Live verify:** snapshot `SELECT count(*), type FROM project_relations` before and after; retype one pair through the
  MCP dry-run, then for real; confirm `GET /project/<slug>` renders the links row.

### Piece 5: `v2c-5-whereabouts-provenance` (base: `v2c-4-typed-links`)

- **Scope:** `whereabouts`, `whereabouts_note`, `provenance`, `provenance_credit`, `highlight` on projects (3.4, 3.5,
  3.12); core + MCP `set_whereabouts`, `set_card_provenance`, `set_card_highlight`; `reference-only` migration sets
  `provenance='referenced'` (4.2); file-provenance display mapping `card_rules.file_provenance_label`; detail-page
  controls; computed needs `missing_provenance` (with majority-file suggestion) and `missing_whereabouts`.
- **Files:** `core/db.py`, `core/card_rules.py`, `core/cards.py`, `web/app.py`, `web/templates/project_detail.html`,
  `mcp_server/server.py`.
- **Acceptance:** whereabouts on `action`/`event`/`family` is refused; `in_use` + `sold` is refused from either field's
  setter; `never_built` + `in_progress` refused; provenance values validate; `capture_events.provenance` is untouched by
  the migration and the existing file tool/dropdown still work; the suggestion for a card whose files are mostly
  `created` is `created` and is not applied; card highlight is independent of file highlight.
- **Live verify:** set and clear each field via MCP; run `list_needs_decision(need='missing_provenance')` and check the
  suggestions against a hand count of file provenance for two cards.

### Piece 6: `v2c-6-reorg-mcp` (base: `v2c-5-whereabouts-provenance`)

- **Scope:** `split_card`, `merge_cards`, `move_files`/`copy_files`, `nest`/`unnest` tools, `set_home` +
  `home_kind`/`home_ref` + `resolve_home` (3.10), `explain_card`, the full `list_needs_decision` (computed + stored,
  filters), `resolve_decisions` (bulk, dry-run default), `bulk_edit` (dry-run default), `undo`, `list_changes`;
  `hobby` membership tools under the new names; all returning the common result shape.
- **Files:** `core/cards.py`, `core/changes.py`, `core/db.py`, `core/card_rules.py`, `mcp_server/server.py`,
  `web/app.py` (simple page buttons for nest/unnest/home on the detail page; drag-and-drop is out of scope).
- **Acceptance:** the Skyhawk worked example (7.4) runs as one batch on a test-box copy and `constructicon_undo` of its
  `batch_id` restores the exact prior state (files, hobbies, links, card rows); `merge_cards` followed by undo restores
  the absorbed card with the same id and files; bulk with `dry_run` true writes nothing (row counts identical); an
  all-or-nothing bulk with one invalid item writes nothing; `undo` refuses after an intervening edit unless `force`;
  `explain_card` for Clod-a-Pede returns children (Lua), hobbies, links, home chain, file counts by type and a level;
  migration audit rows are refused by `undo`.
- **Live verify:** perform the Skyhawk split on a **copy** card on constructicon-test (create a throwaway project with
  a few moved files, since the real Skyhawk decisions need the owner), undo it, redo it, and diff
  `explain_card` before/after.

### Piece 7: `v2c-7-cards-home` (base: `v2c-6-reorg-mcp`)

- **Scope:** `web/static/js/cards.js` + `web/static/css/cards.css` (8.1), `core/card_level.py` (3.11),
  `core/cards.py: card_json`, `GET /api/cards/{slug}`, `/api/projects` extended with card JSON; the home Projects widget
  rebuilt on cards (8.2): featured full-size card, regular and compact cards, date ranges, group-by badges, family
  collapse, reference zone; the timeline rail untouched; `_project_tile.html` retired in favour of the card renderer.
- **Files:** `web/static/js/cards.js` (new), `web/static/css/cards.css` (new), `web/templates/home.html`,
  `web/templates/_project_tile.html`, `web/app.py`, `core/card_level.py` (new), `core/cards.py`.
- **Acceptance:** every top-level card on the test box renders with all zones populated or intentionally omitted;
  date ranges match `resolve_project_span`; pips match `card_level` for five hand-checked projects (including one whose
  blank auto-write-up must score **no** write-up pip); frame colours match the table in 8.1; featured card is the
  highlighted/most-recently-active card; a multi-hobby card appears once in the default view with all codes and once
  per hobby-filtered view; family members collapse under the family card unfiltered; the vertical timeline rail still
  renders every year including gaps; no UI string contains a forbidden word from section 1 (grep the templates and JS).
- **Live verify:** fetch `/` and `/api/projects` through the container; compute expected pips/ranges in a script from
  the DB and compare to the JSON; grep served assets for forbidden vocabulary; owner reviews the look in their browser.

### Piece 8: `v2c-8-detail-stacks` (base: `v2c-7-cards-home`)

- **Scope:** `web/static/js/stacks.js` (new) + CSS; the detail item grid replaced by per-type piles, fan and "Show all N"
  (8.3); asset card variant; nested/member cards beside the piles; the links row display; breadcrumb from the home
  chain; project timeline unchanged. Counts and date spans for each pile computed server-side
  (`cards.file_stacks(card)`).
- **Files:** `web/templates/project_detail.html`, `web/static/js/stacks.js` (new), `web/static/css/cards.css`,
  `core/cards.py`, `web/app.py`.
- **Acceptance:** Clod-a-Pede shows one pile per `media_type` with correct counts (e.g. the 71 photos in one pile) and
  spans (Nov 2025 - Jul 2026 overall); the top card is flat with three tilted beneath; click fans, "Show all N" reveals
  the existing full grid with every file present (`count == len(grid)` asserted from the DOM text); nested Lua card sits
  beside the piles; the links row lists typed links with the right labels; the write-up and the Project Timeline are
  unchanged; keyboard and reduced-motion behaviour as specified; a card with no files shows no piles and no errors.
- **Live verify:** fetch `/project/<slug>` for Clod-a-Pede, AlienWhoop (family) and a file-less card; assert pile
  counts against `SELECT media_type, count(*)` from `project_items`.

---

## 10. Out of scope

- **Static site export update** (`core/site_export.py`, `core/project_export.py`, the Pages repo): reads frozen legacy
  `status`; a follow-up issue moves it to cards.
- **Drag-and-drop** reorganizing: tools plus simple page buttons only; DnD later.
- **Auth** (#467). Still a single-owner, LAN-only tool.
- Typed links between **files** (object-level `capture_event_relations` stay untyped).
- Revision tracking for superseded files (#477).
- Family-in-family nesting, per-hobby ordering UI, a "Workplace" skin (generic vocabulary is already the only
  vocabulary, so a relabel layer can come later), and applying anything to `constructicon-web` (prod): this build
  produces PRs and test-box verification only.

---

## 11. Conflicts between sources, and what was chosen

1. **"Things as a first-class entity" (#428 early) vs. "Things as a `kind` field, not a table" (#428 later).** Later
   wins: `projects.kind`.
2. **"One home per card" (#428 early) vs. "no forced single home" (#428 later).** Later wins: many-to-many for hobby,
   family and files; single parent only for "part of" nesting; **home** is an automatic default plus a manual override
   (3.10).
3. **Status vocabulary.** Early proposals had Up next / Someday / Gone; the settled model is Active (In progress, In
   use) and Inactive (Idea, Paused, Done, Stopped + reason). Used the settled one. Game zone names were dropped.
4. **Collection and Event** were "proposed, not settled" in #480 but "included" in #481's defaults: included, using
   the group-kind mechanism for Collection and a restricted stage set for Event.
5. **#480 says "write every change to the existing audit log with before/after".** The existing `audit_log` is an
   HTTP-request log with no values and MCP never writes to it. Chose to **extend `audit_log`** (3.13) rather than add a
   parallel table, so there is still one log.
6. **#481 piece 3 says "Family flag"**; #428 later says families are many-to-many membership. Implemented as
   `kind='family'` (from piece 1) plus the `family_members` table (piece 3); there is no separate flag column.
7. **#481 piece 3: "AlienWhoop children move from nesting to family"** vs. the standing rule that ambiguous calls are
   queued. Queued as `card_family_members` with current children suggested-yes; nothing moves until answered.
8. **Highlight "reuse v1's".** v1's highlight exists only on files (`capture_events.highlight`). Added the same-named
   column on `projects` rather than invent a new concept (3.12).
9. **Hobby status.** v1 has three values (`active|dormant|abandoned`); the owner wants a two-value manual switch.
   Mapped `dormant`/`abandoned` to `inactive`; kept deprecated aliases for one release.
10. **`pending_decisions.post_slug` assumes a file slug** and `core.decisions.list_open` auto-resolves any decision
    whose slug has no file row. Card decisions would be destroyed on first load. Chose `card:<slug>` plus a required
    `list_open` change in piece 1 (4.3).
11. **Frozen legacy `projects.status`.** The export filters on legacy `status="active"`, and the export must not break.
    So the column is not rewritten or projected; live code moves to `stage`. Cost: the export keeps seeing stale
    status for edited cards until its own follow-up lands.
12. **"Dry-run default for single-card edits?" (#480 open question).** Bulk only; single-card tools apply, with an
    optional `dry_run`.
13. **Pip 4, "the owner's own words".** v1 has no authorship signal. Chose an explicit marker on the write-up document
    (`type_metadata.owner_words`) set when the owner's wording is recorded, over a text heuristic that would
    false-positive. Revisit when the oral-history flow exists as a tool.
14. **The word "stack" is kept** (a layout term) while Xyz/monster/spell vocabulary is removed, per the owner's
    "keep the card design, drop the game vocabulary" direction.
15. **Home page family collapse** (8.2) is an assumption: the owner said a card shows once with several badges but did
    not say whether family members repeat on home. Chose collapse (members visible on the family's detail page and in
    hobby-filtered views).
16. **Counts.** The stated 71-project total sums to 71 across the nine legacy values (complete 41, abandoned 11,
    shelved 6, wip 5, means-to-an-end 4, failed 1, archived 1, active 1, reference-only 1). `idea`, `published` and any
    other value currently have zero rows but are mapped for safety.
