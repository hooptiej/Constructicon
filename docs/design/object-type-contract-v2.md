# Object-type contract v2

Design spec for epic [#447](https://github.com/hooptiej/Constructicon/issues/447) (phases [#448](https://github.com/hooptiej/Constructicon/issues/448) + [#449](https://github.com/hooptiej/Constructicon/issues/449)). It follows on from [v1 / #67](object-type-plugin-architecture.md). Like v1, it's written before implementation so the build has one agreed shape.

## The rule

> Every object type is **one self-contained drop-in file** in `core/object_types/`. There are **zero** type-specific edits anywhere else, and every file arrives through **one** ingest path. Every type has its **own importer and its own specific info**; there is no catch-all type ([#432](https://github.com/hooptiej/Constructicon/issues/432)). If the contract can't express something a type needs, the contract is extended. That plumbing is required scope. (Owner, 2026-09-30.)

v1 delivered "zero-edit *registration*". v2 delivers "zero-edit *behavior*". Today a new type still needs template, route, export and pipeline edits whenever it wants to look or act like itself.

## What v1 leaves outside the type files (measured on `main` at fee4585)

| Leak | Where | Fixed by |
|---|---|---|
| Object-page viewer chain: youtube embed, audio player + tag block, video player, code/hljs, url card, document fallback | `object_detail.html` 22-121, 1428-1444; `app.py` 521/525/529 (`is_audio_file`, `is_video_file`, `youtube_embed_url`), `_youtube_embed_url` 393-402 | `preview_fn` |
| A second, independent viewer chain for static export, plus a second YouTube parser | `export_templates/project.html` 17-35, `blog_post.html` 37-55; `site_export.py` 20-34, 49 | same `preview_fn` (one implementation, two render contexts) |
| YouTube stats block and "Watch on YouTube" CTA | `object_detail.html` 136-177 | `properties_fn` + `preview_fn` |
| YouTube "Fetch real date" endpoint gated on `media_type != "youtube"` | `app.py` 1816-1878; template 146-153, JS 406-423 | `actions` |
| Document write-up editor | `object_detail.html` 304-309, JS 1009-1013; `type_metadata.body` is unregistered | `edit_fields` |
| URL default title (`content_description = url`) | `app.py` 1683-1684 | `pre_store_fn` |
| URL classification hard-coded as youtube-else-url | `object_types/__init__.py` `classify_url` 167-175 | `url_match_fn` |
| Save-time thumbnail + OCR source gate keyed on an extension list | `storage.py` 23, 97-100, 131-132; `ocr.py` 110-115; `scripts/regenerate_thumbnails.py` | derive from `UPLOADED_FILE` specs |
| 12 of 19 types show no specific info | archive, code, data, document, eps, font, stl, svg, text, url, youtube, stream | required `properties_fn` |
| Four ingest paths running different steps | `/api/upload`, `/api/content`, MCP `_ingest`, MCP `add_content` (see below) | `core/ingest.py` |
| `media_type` never validated on content creation (an MCP typo silently makes an `unknown` row) | `/api/content`, MCP `add_content` | `core/ingest.py` validation |
| `stream` type: unreachable stub, no capture | `object_types/stream.py` | **deleted** |
| `backfill_content_dates.py` hard-codes `("image","video")` | script line 79 | derive: types with `embedded_metadata_fn` |

Legit-generic code (spec-dispatched capability checks, storage stats grouped by type, gallery filter tabs, youtube-domain sync scripts that create `youtube` rows) stays as-is. The full audit is on #447.

## 1. Spec changes (`ObjectTypeSpec`)

**Required for every type** (enforced by `register()`, see §4):

```python
preview_fn: Callable[[PreviewContext], Markup]
properties_fn: Callable[[dict], dict[str, str]]   # label -> display value; {} only when the file is missing/unreadable
```

**Optional**, declared only by types that need them:

```python
sniff_fn: Callable[[Path, str], bool] | None = None        # content-based claim (path, filename)
pre_store_fn: Callable[[IngestCandidate], PreStore] | None = None
url_match_fn: Callable[[str], bool] | None = None          # content-only types claiming pasted URLs
url_fallback: bool = False                                 # exactly one type (url) may set this
actions: tuple[TypeAction, ...] = ()                       # per-object buttons, e.g. youtube "Fetch real date"
edit_fields: tuple[MetadataField, ...] = ()                # editable type_metadata keys, e.g. document "body"
```

The existing fields stay. `metadata_fields` becomes *live*: the object page renders them generically and the API validates writes to those keys.

### `preview_fn(ctx)`

`ctx` is a small `PreviewContext`: the item dict, plus **resolved URLs** (`media_url`, `thumb_url`, `page_url`) and `mode` (`"live"` or `"export"`). The caller resolves URLs: `/f/<slug>` on the live site, bundled relative paths in the export. So the type writes its markup **once** and it works on both the object page and hooptiej.com. It returns `Markup`. Types must escape user data (Jinja `escape`/`Markup.format`); the review checklist covers this.

Shared building blocks live in `core/object_types/_preview.py` (leading underscore, so `pkgutil` doesn't register it as a type): `image_viewer`, `thumb_with_original_link`, `file_icon`, `media_player`. Types compose these rather than copying markup.

Page-level assets (highlight.js for code) are declared as `preview_assets: tuple[str, ...]` on the spec. The template includes the assets for the item's type, with no `{% if media_type == 'code' %}`.

### `sniff_fn` and extension overlap

`detect_media_type(filename, path=None)`:
1. Take the candidate specs whose `extensions` include the file's extension.
2. If a `path` is given, run the candidates that have a `sniff_fn`. The first `True` wins. Order is `sniff_priority` (int, default 0), then key.
3. Otherwise fall back to the single candidate **without** a sniffer.
4. No candidates → `None` (unsupported, rejected as today).

Registration fails if two specs claim the same extension and fewer than one of them lacks a sniffer (ambiguous), or if more than one of them lacks a sniffer (unresolvable). That gives `.exe` → installer (sniffed) vs application (fallback), and `.pem`/`.crt` key-vs-cert, without touching any caller.

### `pre_store_fn(candidate) -> PreStore`

It runs after type detection and before the row is inserted. **As built (#448):** `ingest_file` saves the upload to storage first, so both `sniff_fn` and `pre_store_fn` get a real file path. The file is deleted if the type is unsupported after sniffing, on `reject`, and on `metadata_only`, so no bytes outlive a refusal.

```python
PreStore.accept(**row_overrides)        # e.g. url: content_description=<url>
PreStore.reject(reason)                 # -> 400 on web, {"error": reason} on MCP
PreStore.metadata_only(type_metadata)   # store the row + metadata, discard the bytes (cert/key private-key policy, #443)
PreStore.needs_decision(kind, question, options, provisional_type)
                                        # store as provisional_type AND write a pending_decisions row (#240 queue); the
                                        # resolution re-types the object through the registry (.exe installer-vs-app, #446)
```

`needs_decision` makes the existing "Needs your input" queue generic. Resolving a decision whose `kind` is `retype` calls `ingest.retype(slug, new_type)`, which reruns the type's post-insert steps. None of that lives in upload code.

### `actions`

`TypeAction(key, label, handler: Callable[[dict], dict], confirm: str | None)`. One generic route, `POST /api/image/{slug}/action/{key}`, looks the action up on the row's spec. The object page renders one button per action. YouTube's "Fetch real date" moves here, and its dedicated endpoint is deleted.

### `edit_fields`

`MetadataField`s with an `input` kind (`text`, `textarea`, `markdown`). The object page renders them generically, and saves go through the existing `type_metadata` update with keys validated against the spec. Document's write-up editor moves here.

## 2. One ingest pipeline (`core/ingest.py`)

Today the four entry points run different steps:

| step | `/api/upload` | `/api/content` | MCP `_ingest` (upload/import) | MCP `add_content` |
|---|---|---|---|---|
| size guard | ✓ | n/a | ✓ | n/a |
| dupe check | ✓ (409) | ✗ | ✓ (returns existing) | ✗ |
| type detect / validate | ✓ ext | classify_url, **no validation** | ✓ ext | **no validation** |
| embedded metadata | ✓ | ✗ | ✓ | ✗ |
| OCR / capture thumb | ✓ (bg) | ✓ | ✓ (sync thumb) | ✓ (sync thumb) |
| captions | ✓ | ✓ | **✗** | **✗** |
| project attach / automatch | ✓ / ✓ | ✓ / ✗ | **✗ / ✗** | ✗ / ✗ |

v2 has two functions that every entry point calls:

```python
ingest.ingest_file(fileobj_or_path, filename, *, size, source, description, tags, source_modified_at,
                   project_id=None, folder_name=None) -> IngestResult
ingest.ingest_content(*, external_url=None, media_type=None, source, description, tags,
                      type_metadata=None, content_date=None, project_id=None) -> IngestResult
```

`ingest_file` runs these steps in order: size guard → dupe check → extension check → save (`storage.save_stream`) → `detect_media_type(filename, path)` (sniff) → `pre_store_fn` → insert → embedded metadata → post-insert dispatch (text pipeline / capture thumb / captions) → project attach → automatch.

`ingest_content` runs: validate or classify the type (`url_match_fn`, `url_fallback`) → `pre_store_fn` → insert → post-insert dispatch → project attach.

`IngestResult` carries `row`, `duplicate`, `error` and `pending_decision_id`. `/api/upload` maps it to HTTP codes; MCP maps it to dicts. The background runner is injected: FastAPI `BackgroundTasks` on web, a thread on MCP. The behaviour becomes identical everywhere, which also fixes MCP uploads never being captioned or auto-matched.

`storage.save_stream` stops making thumbnails from its extension list. The post-insert dispatch calls `thumbnails.ensure_thumbnail` for `UPLOADED_FILE` types instead, and `storage.IMAGE_EXTENSIONS` is deleted. `ocr._ocr_source_path` drops its extension check. `regenerate_thumbnails.py` derives its scope from the specs.

## 3. Existing types to be brought up to spec

| type | preview_fn | properties_fn (new or extended) | other |
|---|---|---|---|
| image | `image_viewer` (+rotation) | exists; + color mode, bit depth, DPI | |
| gif | `image_viewer` | exists; + loop count, duration | |
| video | `media_player` video (poster = thumb) | exists | |
| audio | `media_player` audio + tag block | exists (tags already here) | the template's tag block is deleted, since properties already show them |
| pdf / ai / eps / psd | `thumb_with_original_link` | pdf/ai/psd exist; **eps new**: BoundingBox, Title/Creator/CreationDate | |
| svg | **native `<img>` of the SVG** (sanitized; matches export) | **new**: viewBox/size, title/desc, element count | |
| stl | `thumb_with_original_link` | **new**: triangles, bbox dimensions, ascii/binary, volume if closed | |
| font | `thumb_with_original_link` (pangram) | **new**: family, style, version, designer, glyphs | |
| code | hljs block (`preview_assets` = hljs) | **new**: language, lines, encoding | |
| data | table preview (first ~20 rows, escaped) | **new**: rows, columns, headers, delimiter | |
| archive | file tree (from extracted listing) | **new**: entries, uncompressed size, ratio, encrypted? | |
| text | monospace block | **new**: lines, words, encoding, line endings | `.md` splits out in #450 |
| document | rendered body | **new**: word count, headings | `edit_fields`: body |
| youtube | embed via `extract_youtube_id` (export uses the same) | **new**: views, likes, comments, author, video id (moved from template) | `url_match_fn`; `actions`: fetch real date |
| url | link card | **new**: domain, scheme | `url_fallback`; `pre_store_fn` default title |
| stream | n/a | n/a | **delete the module** (never reachable; nothing to migrate, re-check prod at build time) |

## 4. Enforcement

`register(spec)` raises `ObjectTypeContractError` at import time (so the app refuses to boot) when:
- `preview_fn` or `properties_fn` is missing;
- `url_fallback` is set on more than one spec;
- extension ownership is ambiguous (see sniff rules);
- `edit_fields` keys collide with `metadata_fields` keys that aren't editable;
- `key` collides with an existing key.

Add a startup self-check test (`scripts/check_object_types.py`, run in the verify step) that imports the registry, lists every type's hooks, and renders each type's `preview_fn` against a synthetic row.

## 5. Delivery

**One epic, three PRs**, so each is reviewable and live-verifiable on constructicon-test:

1. **Plumbing, no visible change:** `core/ingest.py` with every entry point switched to it; `detect_media_type(path)` + `sniff_fn`; `pre_store_fn` + `PreStore`; `url_match_fn`/`url_fallback`; generic `actions`/`edit_fields` routes; storage/OCR extension leaks removed. Verify: every existing type ingests identically through web and MCP, MCP uploads now caption and auto-match, and a typo'd `media_type` is rejected.
2. **Types up to spec:** `preview_fn` + `properties_fn` for all 18 remaining types (one commit per type); `object_detail.html`'s viewer chain and the export templates' chain replaced by `preview_html`; YouTube/document leaks moved into their files; `stream` deleted. Verify: a real file per type (prod has at least one of every file type except svg, see CLAUDE.md) renders on the object page and in `/preview/` export, next to a before-screenshot or text dump.
3. **Enforcement on:** `register()` hard-fails + the self-check script + CLAUDE.md "Adding a type" section rewritten to point here. Verify: a deliberately incomplete throwaway type file makes boot fail with a clear message.

Then #450 (`.md`/`.txt`) and #443–#446 are each built as a single new file on top.

## Risks

- **Visual regressions on the object page** are the main risk of PR 2. Mitigation: per-type before/after on real rows; the owner eyeballs PR 2 on constructicon-test (a visual change, same as #441).
- **XSS surface moves into Python.** Viewer markup built in type files must escape user data. Mitigation: `_preview.py` helpers escape by construction; review checklist item; SVG preview sanitized; the Markdown renderer runs with raw HTML off (#450).
- **Background-task behaviour change on MCP** (it now captions and auto-matches). This is intended, but captioning is the heavy Ollama path. Bulk MCP imports will now queue captions exactly as web uploads do.
