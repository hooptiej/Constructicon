# Object-type plugin architecture

Design spec for [#67](https://github.com/hooptiej/Constructicon/issues/67). Written up before implementation starts so the migration has one agreed shape to build against, rather than each type's move being a separate judgment call.

## Problem

Every object type added so far (image, youtube, pdf, stl, psd, audio, svg, eps) required a matching hand-edit in four places:

1. **`core/object_types.py`** — the `ObjectTypeSpec` registry entry, plus a manual `from . import pdf as _pdf`-style import at the top of the file for any type with a dedicated logic module.
2. **`core/storage.py`** (`ALLOWED_EXTENSIONS`, lines 18-21) — a hand-maintained set built from six separately-named extension constants (`AUDIO_EXTENSIONS`, `SVG_EXTENSIONS`, `PDF_EXTENSIONS`, ...), disconnected from the registry that otherwise describes the type.
3. **`web/app.py`** (`api_upload`, lines 636-650) — a 15-line `if ext in storage.PDF_EXTENSIONS: media_type = "pdf" elif ...` chain.
4. **`mcp_server/server.py`** (lines 91-104) — the *exact same* if/elif chain, copy-pasted.

The per-type logic itself (`core/pdf.py`, `core/stl.py`, `core/psd.py`, `core/svg.py`, `core/eps.py`) is already well-isolated — the scatter is purely in *registration*, not logic.

## Design

### 1. `core/object_types.py` becomes a package

```
core/object_types/
  __init__.py      # registry + discovery, no type-specific code
  image.py
  youtube.py
  document.py
  audio.py
  pdf.py
  stl.py
  psd.py
  svg.py
  eps.py
  stream.py         # unreachable stub today, kept for parity
  url.py            # unreachable stub today, kept for parity
```

Each type file is genuinely self-contained: extensions it claims, badge icon/text, thumbnail strategy, OCR flag, `capture_fn`/`text_extract_fn`, metadata fields — everything `ObjectTypeSpec` holds, plus whatever real logic the type needs (a `pdf.py`-shaped module keeps its `render_first_page`/`extract_text` functions in the same file as its spec, instead of split across a central registry entry and a separate logic module).

### 2. `ObjectTypeSpec` gains an `extensions` field

```python
extensions: frozenset[str] = frozenset()
```

Extensions move from being a `storage.py` concept to being a property of the type itself, matching everything else `ObjectTypeSpec` already describes. Types with no file (youtube, document, stream, url) simply leave it empty.

### 3. Auto-discovery via `pkgutil`, self-registration via decorator

No new dependency — this is a plain FastAPI app with no existing plugin framework, so a directory scan + import is the right amount of mechanism:

```python
# core/object_types/__init__.py
import importlib
import pkgutil

OBJECT_TYPES = {}

def register(spec):
    OBJECT_TYPES[spec.key] = spec
    return spec

for _, name, _ in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{name}")
```

Each type file ends with:

```python
object_types.register(ObjectTypeSpec(
    key="pdf",
    label="PDF document",
    extensions=frozenset({".pdf"}),
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,
    capture_fn=capture_thumbnail,       # defined earlier in this same file
    text_extract_fn=extract_text_for_row,
    badge_icon="\U0001F4C4",
    badge_text="PDF",
))
```

Dropping a new file into `core/object_types/` is genuinely zero-edit elsewhere — the directory scan picks it up on next import.

### 4. One shared extension→type detector replaces the duplicated if/elif chains

```python
# core/object_types/__init__.py
def detect_media_type(filename):
    ext = Path(filename).suffix.lower()
    for spec in OBJECT_TYPES.values():
        if ext in spec.extensions:
            return spec.key
    return None
```

`web/app.py`'s `api_upload` and `mcp_server/server.py`'s upload tool both replace their 15-line chains with one call to this. `None` means "unsupported file type" — reject with the same 400 both already raise today.

### 5. `storage.py` stops owning an extension allowlist

This is the one real gotcha. `core/object_types/__init__.py` imports each type module (via `pkgutil`), and type modules like `pdf.py` import `storage` (for `storage.path_for()` etc.) — so having `storage.py` import back from `object_types` to build its allowlist would be a circular import.

Fix: move the "is this extension supported" decision to the call site. `detect_media_type()` returning `None` *is* the rejection — `storage.save_file()` no longer needs an opinion of its own and drops `ALLOWED_EXTENSIONS` entirely, becoming a dumb "write these bytes under a fresh slug" primitive. Callers (`web/app.py`, `mcp_server/server.py`) call `detect_media_type()` first and only reach `storage.save_file()` once they have a known-good type. This also directly satisfies #67's own ask that the allowlist "derive from the per-type files' own declared extensions, not be hand-maintained lists."

### 6. YouTube vs. a plain URL — content-type identification

Two of the registered types have no file extension at all (`youtube`, and the still-unreachable `url` stub) — they're both created via `POST /api/content` with an `external_url`, not an upload. Today `media_type` is a required, trusted `Form(...)` param on that route: the caller (today, only `scripts/*.py` and manual entry) has to already know which type it's creating.

Once `url` becomes a real, reachable type (tracked as its own future issue, not in #67's scope, but worth designing for now since this package is being restructured anyway), both types will accept "some URL the owner pasted" as input, and something needs to decide which of the two it actually is. That decision should not be a caller-supplied guess — it should be a shared, testable classifier:

```python
# core/object_types/youtube.py
YOUTUBE_ID_RE = re.compile(r"(?:v=|/embed/|youtu\.be/)([A-Za-z0-9_-]{6,})")

def extract_youtube_id(url):
    ...  # unchanged from today's object_types.py

def matches(url):
    return extract_youtube_id(url) is not None
```

```python
# core/object_types/__init__.py
def classify_url(url):
    """Which registered type recognizes this URL, checked in a fixed,
    documented order (youtube before the generic url fallback) so a
    youtu.be link is never misclassified as a plain URL. Returns a
    media_type key, defaulting to "url" if nothing more specific claims it."""
    if youtube.matches(url):
        return "youtube"
    return "url"
```

`web/app.py`'s `/api/content` route calls `object_types.classify_url(external_url)` when the caller doesn't explicitly pass `media_type` (or to validate/correct one that's passed), instead of trusting an unchecked string. This keeps the "one file, self-contained" property — `youtube.py` is the only place that knows what a YouTube URL looks like, exactly like `pdf.py` is the only place that knows how to render a PDF's first page.

## Migration order

Do this as one PR per the codebase's existing discipline (branch → test against `constructicon-test` → confirm before merge), but land the *types* serially within it so each step is independently verifiable:

1. **Scaffold**: `core/object_types/__init__.py` (registry + `register()` + `pkgutil` discovery + `detect_media_type()` + `classify_url()`), with `OBJECT_TYPES` empty and nothing registered yet. No behavior change possible yet — this step is pure plumbing.
2. **`image`** first. It's the largest real-world slice of existing data (verify against a live `constructicon_search` count before assuming, but image uploads are the historical bulk of this gallery), and it's the simplest spec shape — `UPLOADED_FILE` thumbnail source, no `capture_fn`, no dedicated logic module today. A clean pilot that proves the discovery mechanism and the `detect_media_type()`/`storage.py` decoupling before any of the more complex CAPTURE-sourced types move.
3. **`youtube`** — proves the no-extension / `FETCH_URL` shape, and is where `classify_url()` actually lands (`extract_youtube_id` moves from the old flat `object_types.py` into this file).
4. **`document`**, **`audio`** — both `NONE` thumbnail source, no dedicated logic module, low risk.
5. **`pdf`**, **`stl`**, **`psd`**, **`svg`**, **`eps`** — each moves its existing `core/<type>.py` module's content directly into `core/object_types/<type>.py` wholesale (function bodies unchanged), then appends the spec.
6. **`stream`**, **`url`** — the two unreachable stubs, moved as-is (still no `capture_fn` for `stream`; `url` gains `classify_url()`'s fallback meaning but stays otherwise unimplemented — making it reachable from the UI is separate, future scope).
7. **Callers**: update `web/app.py`'s `api_upload` and `mcp_server/server.py`'s equivalent to call `object_types.detect_media_type()` instead of their duplicated if/elif chains; update `/api/content` to call `classify_url()`.
8. **Cleanup**: delete the now-unused `core/pdf.py` etc. (content moved into the package), delete `storage.py`'s `ALLOWED_EXTENSIONS` and the six extension-set constants.

Every currently-working type must be verified explicitly post-migration against `constructicon-test` — not just import/syntax checked — before merging, per #67's own stated constraint and the lesson from #68's review (an agent's own "verified" claim via `py_compile` alone previously missed two real bugs; a live smoke test against real data is the actual bar).
