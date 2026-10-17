"""Registry of object/file types Constructicon can store.

Every capture_events row has a `media_type` (see core/db.py's SCHEMA comment
for the full loose vocabulary — 'image', 'youtube', 'document', and
whatever a caller invents next). This module is the single place that
describes, per type:

  - what per-type metadata fields exist beyond the generic capture_events
    columns (stored in the row's `type_metadata` JSON column — see
    db.set_type_metadata — rather than one-off ALTER TABLEs per type)
  - how to obtain a representative thumbnail image for the type (an
    uploaded file IS the image, a URL is fetched, or an image is captured
    on demand — see ThumbnailSource)
  - whether that representative image should be run through OCR the same
    way an uploaded screenshot already is
  - what descriptive metadata the uploaded file itself carries (an audio
    file's ID3 tags — #255, see embedded_metadata_fn) and should seed the
    row's content fields with at upload time

core/thumbnails.py and core/ocr.py both dispatch purely off the spec
returned by get_object_type() — neither one should ever grow a literal
`if media_type == "some_new_type"` branch. Adding a new object type (PDF:
issue #13, STL: issue #14, PSD: issue #27, audio: issue #28, SVG/EPS: issue
#30, and whatever comes after) means adding one ObjectTypeSpec below plus,
if it needs one, a thumbnail_url_fn or capture_fn. Nothing else in the
codebase should need to change.
"""

import importlib
import mimetypes
import pkgutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .. import parse_limits


class ObjectTypeContractError(Exception):
    """Raised when object type spec registration or validation fails."""
    pass


class ThumbnailSource(Enum):
    """How a type's representative thumbnail image is obtained."""

    UPLOADED_FILE = "uploaded_file"  # the row's own stored_filename IS the image (today: 'image' uploads)
    FETCH_URL = "fetch_url"          # download a representative image from a URL derived from external_url (e.g. YouTube's static thumbnail)
    CAPTURE = "capture"              # run a capture routine to produce one on demand (a stream's OSD/wait-card frame, a URL's screenshot, a rendered PDF/STL preview)
    NONE = "none"                    # no thumbnail concept for this type (e.g. a plain text/document post)


@dataclass(frozen=True)
class MetadataField:
    """Documents one per-type property stored in a row's `type_metadata`
    JSON blob (see db.set_type_metadata) rather than as a dedicated column.
    Purely descriptive today — the standard a type's fields are recorded
    against — not yet wired into form generation or validation; that's for
    whichever follow-on issue first needs it."""

    key: str
    label: str
    required: bool = False
    help_text: str = ""
    input: str = ""  # "" = not editable; "text" | "textarea" | "markdown"


@dataclass(frozen=True)
class TypeAction:
    """An action available on rows of this type (#448).

    handler: Callable that takes a row dict and returns a dict of updates.
    confirm: Optional confirmation message to show before applying.
    applies_fn: Optional (row) -> bool (#446). When set, the action is only
        offered (object page) and only runnable (action route) on rows it
        returns True for, e.g. "Reclassify" only on .exe installers. None =
        every row of the type.
    """
    key: str
    label: str
    handler: object
    confirm: str = ""
    applies_fn: object = None

    def applies_to(self, row):
        """True when this action is offered for `row` (see applies_fn)."""
        if self.applies_fn is None:
            return True
        try:
            return bool(self.applies_fn(row))
        except Exception as e:
            print(f"TypeAction {self.key!r} applies_fn failed: {e!r}", flush=True)
            return False


@dataclass(frozen=True)
class PreviewContext:
    """Context passed to preview_fn (#449).

    item: the prepared item dict (object page) or export item.
    media_url: URL of the stored file for this render context (live /f/<slug>,
        or the export's relative media path), None when there's no file.
    thumb_url: URL of the thumbnail, if any.
    page_url: the item's external URL, if any.
    mode: "live" (object page) or "export" (static site).
    file_path: the stored file on disk (Path) or None. For previews that must
        read their own file (e.g. a CSV table); never guess at item keys (#449).
    """
    item: dict
    media_url: str | None
    thumb_url: str | None
    page_url: str | None
    mode: str = "live"
    file_path: object = None


@dataclass
class IngestCandidate:
    """Input to pre_store_fn: the minimal facts about an incoming file/content (#448)."""
    filename: str | None
    path: object  # pathlib.Path | None
    media_type: str
    source: str
    external_url: str | None = None
    content_description: str | None = None
    type_metadata: dict | None = None


@dataclass(frozen=True)
class PreStore:
    """Decision returned by pre_store_fn (#448): accept, reject, or defer to the owner."""
    action: str  # "accept" | "reject" | "metadata_only" | "needs_decision"
    reason: str = ""  # for "reject": human-readable reason
    row_overrides: dict = field(default_factory=dict)  # allowed: content_description, content_date, type_metadata
    type_metadata: dict | None = None  # for "metadata_only": per-type properties to store
    decision: dict | None = None  # for "needs_decision": {"kind", "question", "options", "provisional_type"}

    @classmethod
    def accept(cls, **row_overrides):
        """Accept the upload with optional field overrides."""
        return cls(action="accept", row_overrides=row_overrides)

    @classmethod
    def reject(cls, reason):
        """Reject the upload with a human-readable reason."""
        return cls(action="reject", reason=reason)

    @classmethod
    def metadata_only(cls, type_metadata):
        """Accept the content but skip storing the file (e.g. for URL-only content that needs special handling)."""
        return cls(action="metadata_only", type_metadata=type_metadata)

    @classmethod
    def needs_decision(cls, kind, question, options, provisional_type):
        """Defer to the owner: ask a question with a list of options, and store provisionally under provisional_type.

        options: list of {"key", "label"} dicts; a key must be a registered
        media_type (answering retypes the row to it). Optionally one option
        carries "suggested": True (#446): the admin queue preselects it.
        """
        return cls(
            action="needs_decision",
            decision={
                "kind": kind,
                "question": question,
                "options": options,
                "provisional_type": provisional_type,
            }
        )


@dataclass(frozen=True)
class ObjectTypeSpec:
    key: str
    label: str
    thumbnail_source: ThumbnailSource
    ocr_capable: bool  # should this type's representative image be run through OCR, same as an uploaded screenshot
    metadata_fields: tuple = ()  # tuple[MetadataField, ...] — per-type properties, stored in type_metadata
    extensions: frozenset = frozenset()  # frozenset[str] — file extensions this type can handle (e.g. {".pdf"}), empty for types with no file
    # For thumbnail_source == FETCH_URL: (external_url) -> image URL str, or None if not derivable.
    thumbnail_url_fn: object = None
    # For thumbnail_source == CAPTURE: (row: dict) -> raw image bytes, or None on failure.
    # None here just means "not implemented yet" — core/thumbnails.py treats
    # that as "no thumbnail available" rather than an error, so registering
    # a CAPTURE-sourced type ahead of its capture routine existing is safe.
    capture_fn: object = None
    # Issue #478: optional (row) -> bool for a CAPTURE type whose files only
    # SOMETIMES carry a picture (an Office document with or without its
    # embedded preview, a macOS app with or without an .icns icon). False
    # means "no thumbnail for this row": no /f/<slug>/thumb is advertised, so
    # tiles and pages show the type's icon instead of a broken image. Base it
    # on what embedded_metadata_fn stored at upload; never open the file here.
    # None = every row of the type has one (the old behavior).
    has_thumbnail_fn: object = None
    # (row: dict) -> extracted text str, or None/"" if this row has no usable
    # embedded text layer. core/ocr.py tries this FIRST, before ever running
    # OCR — a text-layer PDF (issue #13) is the first type to use this, but
    # it's generic: any future type with its own text layer (e.g. a .docx)
    # registers one here instead of ocr.py growing a per-type branch. Only
    # meaningful when ocr_capable=True; leave None for types that have no
    # text layer to try (an uploaded screenshot goes straight to OCR, same
    # as always).
    # #724: when the row has no picture to OCR either, the return value decides how OCR settles:
    # "" = the source was read and holds no text (done, empty); None = no readable source for this
    # row, e.g. a legacy format with no reader (ocr_status 'unsupported', with ocr_unsupported_note);
    # raising = a real read error (failed). A falsy return still falls back to OCR when there IS a
    # picture (a scanned PDF).
    text_extract_fn: object = None
    # Issue #135: (row: dict) -> dict[str, str] of type-specific properties
    # suitable for display on the object detail page. Returns an ordered dict
    # of label → display-value pairs (e.g. {"Dimensions": "1920 × 1080"} for
    # images, {"Duration": "3:42"} for video). Return {} on any failure
    # (corrupt/missing file, extraction error), same best-effort discipline
    # as text_extract_fn and capture_fn — never raise, print a warning and
    # return the empty dict. Leave None for types that have no computed
    # properties to display. web/app.py calls this defensively and routes the
    # result to the template for generic iteration (no per-type template
    # branches needed).
    properties_fn: object = None
    # Issue #255/#265: (path: pathlib.Path) -> dict of metadata the uploaded
    # file itself carries — an audio file's ID3/Vorbis/RIFF tags, a photo's
    # EXIF capture timestamp, a video's container creation_time — read once
    # at upload time to seed the row's content-side fields. Return shape:
    # {"content_description": str, "type_metadata": {key: value},
    # "content_date": float (UTC unix seconds)}, any key omitted when the
    # file has nothing usable for it, {} when it has nothing at all.
    # Consumed only by core/embedded_metadata.py, which owns the
    # never-overwrite fill rule (and the plausibility gate on content_date)
    # and dispatches off this hook the way core/ocr.py dispatches off
    # text_extract_fn — a new type with embedded metadata registers a
    # function here, nothing else grows a media_type branch. Same
    # best-effort contract as properties_fn: never raise, print a warning
    # and return {} on any failure. The keys a type writes into
    # type_metadata belong in its metadata_fields; a naive source date
    # goes through core/timeline.py's source_datetime_to_epoch.
    embedded_metadata_fn: object = None
    # Issue #12: what the "file kind" badge shown on gallery tiles and the
    # object detail page looks like for this type. badge_icon is a single
    # glyph/emoji for the compact tile-corner badge; badge_text is a short
    # (~3-6 char) uppercase label used on the detail page (and as the tile
    # badge's title/tooltip). Both are plain strings so web/app.py can pass
    # them straight through to templates — no per-media_type branching
    # needed there or in the templates. A future type (PDF: #13, STL: #14)
    # just fills these in alongside the rest of its ObjectTypeSpec and gets
    # a badge for free.
    badge_icon: str = "\U0001F4E6"  # package emoji — generic fallback
    badge_text: str = "FILE"
    # Issue #448: text of the object page's external-link button when the
    # item has a media_url. A type may override (e.g. YouTube); default
    # shown to the caller by render_preview's fallback when None.
    external_link_label: str = "View original ↗"
    # Issue #239: should this type's representative image (the same one OCR
    # runs against — the uploaded file itself, or the generated thumbnail /
    # video frame / rendered raster) be sent to the local vision model for
    # an auto-caption suggestion (core/captions.py)? Scoped by whether the
    # type has a *real rendered-image preview*, NOT by ocr_capable — the two
    # differ on purpose: video is captionable but not OCR'd, and STL is the
    # explicit hard exclusion (plain-background wireframe renders produced
    # degenerate repeated-punctuation garbage in testing, not just a weak
    # caption). Defaults False so a new type opts in deliberately.
    caption_capable: bool = False
    # Issue #448: preview rendering function for web/export display.
    # (PreviewContext) -> Markup; enforced at registration, #448.
    preview_fn: object = None
    # Issue #448: tuple of preview asset definitions (deferred to PR 3).
    preview_assets: tuple = ()
    # #606: how wide the object page makes the preview. "default" keeps the image-sized column
    # (max 700 px, the side panel beside it); "table" gives a wide table the whole page width
    # (the side panel moves below it). The data and spreadsheet types say "table".
    preview_layout: str = "default"
    # #703: (path: Path) -> the codec to show the stored file as plain text in the browser
    # (`/f/<slug>/raw`: text/plain; charset=utf-8, inline, nosniff, sandbox CSP), or None when this
    # file isn't showable text (binary, too big), in which case the raw view is the download.
    # None here = the type has no raw view at all. Read only a little of the file; never raise.
    raw_text_fn: object = None
    # Issue #448: content-based detection for file uploads.
    # (path: Path, filename: str) -> bool — True if this file's bytes match the type.
    # If None, extension-only matching; if present, called after extension match.
    sniff_fn: object = None
    # Issue #448: priority order for sniffers claiming the same extension.
    # Higher wins; ties broken by key order. Ignored if sniff_fn is None.
    sniff_priority: int = 0
    # Issue #448: pre-storage decision hook.
    # (IngestCandidate) -> PreStore; decides accept/reject/metadata_only/needs_decision.
    pre_store_fn: object = None
    # Issue #448: external URL classifier.
    # (url: str) -> bool — True if this URL matches the type's domain/pattern.
    url_match_fn: object = None
    # Issue #448: is this the fallback type for URLs that don't match any sniff_fn?
    # Exactly one type per extension can have url_fallback=True (checked at registry init).
    url_fallback: bool = False
    # Issue #448: actions available on rows of this type.
    actions: tuple = ()  # tuple[TypeAction]
    # Issue #448: form fields for editing type_metadata.
    edit_fields: tuple = ()  # tuple[MetadataField]
    # Issue #448: the key in type_metadata that holds the write-up text body
    # (if any). A type whose items can serve as a project's write-up declares
    # this (e.g. document.py sets it to "body"); None means this type cannot
    # be a project write-up. Used by can_be_writeup() and writeup_body().
    writeup_body_key: str | None = None
    # Issue #443: items of this type are the owner's private reference
    # (e.g. private keys). Stored whole, and still shown where the owner
    # deliberately puts them (project pages, related items, the admin pane's
    # Restricted list, direct links), but kept out of general browsing (home
    # lists, gallery/search, tag pages, unfiled) and NEVER exported: the
    # static site (the public "museum") and project zips skip them. Access
    # control proper is #467 (authentication).
    restricted: bool = False
    # #602: what a file of this type IS, for the refusal when its extension is supported but its
    # contents fail every sniffer ("This .msi file isn't a valid Windows Installer package").
    # A str for the whole type, or a dict {extension: noun} for a type spanning several formats.
    # Unset: "<label> file" is used.
    content_noun: object = ""
    # #724: the plain reason shown when OCR can't read an item of this type at all (no text source
    # and no picture to OCR): core/ocr.py settles it ocr_status='unsupported' with this note in
    # type_metadata.ocr_note, shown grey as "Not compatible with OCR: <note>". A str for the whole
    # type, or a dict {extension: note} for a type spanning several formats (a key "" is the
    # type-wide fallback). Unset: "<label>: no readable text".
    ocr_unsupported_note: object = ""


OBJECT_TYPES = {}

# #644: the hooks core/parse_limits.py runs in a child process (they read the uploaded file).
HOOKS_RUN_IN_CHILD = ("capture_fn", "text_extract_fn", "properties_fn", "embedded_metadata_fn",
                      "preview_fn", "sniff_fn", "pre_store_fn")


def _importable_by_name(fn):
    """True when `fn` is a module-level function (what pickle sends to a child by name)."""
    module = sys.modules.get(getattr(fn, "__module__", None) or "")
    qualname = getattr(fn, "__qualname__", "")
    return module is not None and "<" not in qualname and getattr(module, qualname, None) is fn


def register(spec):
    """Register an ObjectTypeSpec in the global OBJECT_TYPES registry.
    Called by each type module at the end of its definition.

    Checks for duplicate keys, conflicting url_fallback settings, and that
    both preview_fn and properties_fn are present (enforced at registration, #448).
    Full registry validation happens after all modules are imported (see _validate_registry).
    """
    if spec.key in OBJECT_TYPES:
        raise ObjectTypeContractError(f"Duplicate object type key: {spec.key}")

    # Enforce preview_fn and properties_fn (enforced at registration, #448)
    if spec.preview_fn is None or spec.properties_fn is None:
        raise ObjectTypeContractError(
            f"Object type '{spec.key}' must declare preview_fn and properties_fn "
            "(see docs/design/object-type-contract-v2.md)"
        )

    # #644: every hook that reads a file runs in a parser child process (core/parse_limits.py),
    # which receives it by its module and name, so it must be a module-level function.
    for hook_name in HOOKS_RUN_IN_CHILD:
        fn = getattr(spec, hook_name)
        if fn is not None and not _importable_by_name(fn):
            raise ObjectTypeContractError(
                f"Object type '{spec.key}': {hook_name} must be a module-level function (got {fn!r}); "
                "it runs in a parser child process (#644)"
            )

    if spec.preview_layout not in ("default", "table"):
        raise ObjectTypeContractError(
            f"Object type '{spec.key}': preview_layout must be 'default' or 'table', not {spec.preview_layout!r}"
        )

    # Check url_fallback conflicts: only one type can have url_fallback=True globally
    if spec.url_fallback:
        for existing_spec in OBJECT_TYPES.values():
            if existing_spec.url_fallback:
                raise ObjectTypeContractError(
                    f"Multiple url_fallback specs registered: {existing_spec.key} and {spec.key}"
                )

    OBJECT_TYPES[spec.key] = spec
    return spec


def _validate_registry():
    """After all modules are imported, check that extension ambiguities are resolvable.

    For each extension claimed by 2+ specs: exactly one must have sniff_fn=None (the fallback).
    Raises ObjectTypeContractError if unresolvable (2+ non-sniffer claimants).
    """
    extensions_to_specs = {}
    for spec in OBJECT_TYPES.values():
        for ext in spec.extensions:
            if ext not in extensions_to_specs:
                extensions_to_specs[ext] = []
            extensions_to_specs[ext].append(spec)

    for ext, specs in extensions_to_specs.items():
        if len(specs) <= 1:
            continue
        # Multiple specs claim this extension — check for fallback ambiguity
        non_sniffers = [s for s in specs if s.sniff_fn is None]
        if len(non_sniffers) > 1:
            keys = ", ".join(s.key for s in non_sniffers)
            raise ObjectTypeContractError(
                f"Extension {ext} claimed by {len(non_sniffers)} specs with no sniff_fn: {keys}"
            )


def writeup_body(row):
    """Extract the write-up text body from a row, if the row's type supports it.

    Returns the text body (str, possibly empty) if the type declares writeup_body_key,
    or None if the type doesn't support write-ups. Handles missing keys gracefully.

    Args:
        row: a capture_events row dict

    Returns:
        str (empty if present but falsy) or None
    """
    media_type = row.get("media_type")
    spec = get_object_type(media_type)
    if spec.writeup_body_key is None:
        return None
    type_metadata = row.get("type_metadata") or {}
    return type_metadata.get(spec.writeup_body_key) or ""


def can_be_writeup(row_or_media_type):
    """Check if a row or media type can serve as a project write-up.

    Args:
        row_or_media_type: a capture_events row dict, or a media_type string

    Returns:
        bool: True if the type declares writeup_body_key (can be a write-up)
    """
    if isinstance(row_or_media_type, dict):
        media_type = row_or_media_type.get("media_type")
    else:
        media_type = row_or_media_type
    spec = get_object_type(media_type)
    return spec.writeup_body_key is not None


# #701: the media types a browser shows by itself (its PDF viewer, an image, the video/audio player,
# plain text). /f/<slug> serves these `inline` and their link says "View original" and opens a new
# tab; everything else (STL, PSD, ZIP, EXE, fonts, ...) is a download and its link says so.
# Deliberately an allowlist of exact types, not "image/*": image/vnd.adobe.photoshop, image/tiff and
# friends are image/* but no browser shows them. web/content_security.py decides the actual header
# from this set plus #610's sniff (so an .html renamed .pdf is still a download), never the
# extension alone.
BROWSER_VIEWABLE_TYPES = frozenset({
    "application/pdf",
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp",
    "video/mp4", "video/webm", "video/ogg",
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/webm", "audio/flac", "audio/aac",
    "text/plain",
})

# #723: the other names Python's `mimetypes` (which version, and the host's /etc/mime.types) or an
# uploader may give one of the formats above, mapped to the name in BROWSER_VIEWABLE_TYPES that
# browsers understand. Python 3.14 guesses .wav as audio/vnd.wave (older ones said audio/x-wav), so
# without this WAV downloaded instead of playing. The allowlist above stays canonical names only;
# `browser_media_type` is the one place that folds an alias in, for the inline decision and for the
# Content-Type /f/<slug> sends. video/quicktime is deliberately absent: .mov stays a download (#701).
BROWSER_TYPE_ALIASES = {
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-pn-wav": "audio/wav",
    "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4",
    "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac",
    "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg", "audio/x-mpeg": "audio/mpeg",
    # #729: an .opus file is Ogg Opus, whose registered type is audio/ogg (RFC 7845); Python 3.14
    # guesses audio/opus, freedesktop says audio/x-opus+ogg. Served as audio/ogg, which every browser
    # plays. .m4v is MP4 video (Python: video/x-m4v), served as video/mp4.
    "audio/opus": "audio/ogg", "audio/x-opus": "audio/ogg", "audio/x-opus+ogg": "audio/ogg",
    "video/x-m4v": "video/mp4", "video/m4v": "video/mp4",
}


def guess_file_type(filename):
    """The media type a stored file is served with (the same guess Starlette's FileResponse makes
    from the original name), or None."""
    return mimetypes.guess_type(filename or "")[0] if filename else None


def browser_media_type(media_type):
    """#723: `media_type` (parameters dropped, lower-cased) with a known alias folded into the name
    browsers understand (`BROWSER_TYPE_ALIASES`), or "" for none."""
    mt = (media_type or "").split(";")[0].strip().lower()
    return BROWSER_TYPE_ALIASES.get(mt, mt)


def opens_in_browser(filename=None, media_type=None):
    """True when the browser can show this file itself (#701), from its served media type."""
    return browser_media_type(media_type or guess_file_type(filename)) in BROWSER_VIEWABLE_TYPES


def original_link(item):
    """#701: how the object page links to an item's stored file, for every type alike:
    {"label", "new_tab"}. A browser-viewable file says "View original <Type>" and opens in a new
    tab; anything else says "Download original <Type>" and downloads in place."""
    kind = item.get("type_label") or "file"
    if opens_in_browser(item.get("filename")):
        return {"label": f"View original {kind}", "new_tab": True}
    return {"label": f"Download original {kind}", "new_tab": False}


def raw_url(slug):
    """#703: the raw plain-text view of an item's stored file."""
    return f"/f/{slug}/raw"


def raw_text_codec(spec, path):
    """#703: the codec `/f/<slug>/raw` reads the file at `path` with (from the type's `raw_text_fn`),
    or None when the type has no raw view or this file isn't showable text. Never raises."""
    if spec is None or spec.raw_text_fn is None or not path:
        return None
    try:
        return spec.raw_text_fn(path) or None
    except Exception as e:
        print(f"raw_text_fn failed for {spec.key}: {e!r}")
        return None


def render_preview(spec, ctx):
    """Render a preview using spec.preview_fn if available.

    Returns None if spec.preview_fn is None.
    On any exception, prints a warning and returns None (buggy preview
    falls back to the generic chain in the template).

    Args:
        spec: ObjectTypeSpec instance
        ctx: PreviewContext instance

    Returns:
        markupsafe.Markup HTML or None
    """
    if spec.preview_fn is None:
        return None
    try:
        # #644: in a parser child, page-view budget; a limit hit is remembered for the page.
        key = ctx.file_path.name if ctx.file_path else (ctx.item or {}).get("slug")
        return parse_limits.call(spec.preview_fn, ctx, kind=parse_limits.VIEW, what="preview", key=key)
    except Exception as e:
        print(f"preview_fn failed for {spec.key}: {e!r}")
        return None


# Auto-discover and import all type modules in this package.
for _, name, _ in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{name}")

# Validate the registry after all modules are imported
_validate_registry()


def file_extension(filename):
    """Lowercased extension of `filename`, honoring multi-suffix extensions a
    registered type claims (".tar.gz", #485): "a.tar.gz" -> ".tar.gz", while
    "a.b.zip" -> ".zip". Longest registered compound extension wins."""
    name = Path(filename).name.lower()
    compound = {e for spec in OBJECT_TYPES.values() for e in spec.extensions if e.count(".") > 1}
    for ext in sorted(compound, key=len, reverse=True):
        if name.endswith(ext):
            return ext
    return Path(name).suffix


def detect_media_type(filename, path=None):
    """Detect the media_type of a file using extension and optional content-based sniffing.

    Args:
        filename: Original filename (used for extension detection).
        path: Optional Path to the file on disk (used for sniff_fn if available).

    Returns:
        The media_type key if a match is found, or None if unsupported.

    Algorithm:
        1. Extract extension from filename.
        2. Find all specs that claim this extension.
        3. If no specs claim it, return None.
        4. Sort specs with sniff_fn by (-sniff_priority, key).
        5. If path is provided, try each sniffer in order:
           - If sniff_fn(Path(path), filename) returns True, return that spec's key.
           - Exceptions logged as warnings, treated as False.
        6. If no sniffer matched (or no path), use the fallback spec (sniff_fn=None) if available.
        7. Otherwise, return the first sniffer's key if path=None, or None if only sniffers and no path.
    """
    ext = file_extension(filename)
    candidates = [s for s in OBJECT_TYPES.values() if ext in s.extensions]

    if not candidates:
        return None

    # Separate sniffers from fallback
    sniffers = [s for s in candidates if s.sniff_fn is not None]
    sniffers.sort(key=lambda s: (-s.sniff_priority, s.key))
    fallback = next((s for s in candidates if s.sniff_fn is None), None)

    # If path is provided, try sniffers
    if path is not None:
        for sniffer in sniffers:
            try:
                # #644: in a parser child, page-view budget (a sniff reads a header or a zip's
                # directory); a limit hit counts as "not this type".
                if parse_limits.call(sniffer.sniff_fn, Path(path), filename, kind=parse_limits.VIEW,
                                     what="type check"):
                    return sniffer.key
            except Exception as e:
                print(f"sniff_fn failed for {sniffer.key}: {e!r}", flush=True)

        # If no sniffer matched, use fallback if available
        if fallback:
            return fallback.key
        # Only sniffers and none matched, return None
        return None

    # No path provided: use fallback if available, else first sniffer
    if fallback:
        return fallback.key
    if sniffers:
        return sniffers[0].key

    return None


def mismatch_reason(filename):
    """#602: the refusal for a file whose extension IS supported but whose contents matched none of
    the sniffers that claim it (a .msi that isn't an installer package): names the real reason
    instead of "Unsupported file type", which reads as if the extension weren't accepted at all.
    Falls back to the unsupported wording for an extension nothing claims."""
    ext = file_extension(filename)
    sniffers = sorted((s for s in OBJECT_TYPES.values() if ext in s.extensions and s.sniff_fn is not None),
                      key=lambda s: (-s.sniff_priority, s.key))
    if not ext or not sniffers:
        return f"Unsupported file type: {ext or Path(filename).suffix}"
    spec = sniffers[0]
    noun = spec.content_noun.get(ext) if isinstance(spec.content_noun, dict) else spec.content_noun
    noun = noun or f"{spec.label.lower()} file"
    return (f"This {ext} file isn't a valid {noun}: its contents don't match what a {ext} file should "
            f"contain (is it corrupt, or renamed from another format?).")


def ocr_unsupported_note(row, spec=None):
    """#724: the plain reason OCR can't read `row` (its type's ocr_unsupported_note, per extension
    when the type gives a dict), e.g. "web link: no readable text yet"."""
    spec = spec or get_object_type(row.get("media_type"))
    note = spec.ocr_unsupported_note
    if isinstance(note, dict):
        note = note.get(file_extension(row.get("filename") or row.get("stored_filename") or "")) or note.get("")
    return note or f"{spec.label.lower()}: no readable text"


def picture_expected(row, spec):
    """#724: should this row have a picture for OCR to read? False when the type has no thumbnail
    strategy that can produce one (a web link: CAPTURE with no capture_fn yet), the URL gives no
    thumbnail URL, or the type says this row carries none (a .doc with no embedded preview,
    has_thumbnail_fn). True means a missing picture is a real failure (a fetch or render that went
    wrong), not "nothing to read". Reads only the row, never the file."""
    src = spec.thumbnail_source
    if src == ThumbnailSource.NONE:
        return False
    if src == ThumbnailSource.CAPTURE and spec.capture_fn is None:
        return False
    if src == ThumbnailSource.FETCH_URL:
        if spec.thumbnail_url_fn is None:
            return False
        try:
            if not spec.thumbnail_url_fn(row.get("external_url")):
                return False
        except Exception as e:  # can't tell: count it as expected, so a missing picture stays a failure
            print(f"thumbnail_url_fn failed for {row.get('slug')}: {e!r}")
            return True
    if spec.has_thumbnail_fn is not None:
        try:
            return bool(spec.has_thumbnail_fn(row))
        except Exception as e:  # can't tell: count it as expected, so a missing picture stays a failure
            print(f"has_thumbnail_fn failed for {row.get('slug')}: {e!r}")
            return True
    return True


def nothing_to_read(row, spec):
    """#724: True when OCR has nothing at all to read for this row: no text source (the type has no
    text_extract_fn) and no picture expected. The one test the re-settle migration
    (core/db.py _mig_ocr_unsupported_724) uses, so old rows are judged exactly as new ones are."""
    return spec.text_extract_fn is None and not picture_expected(row, spec)


def accepted_extensions():
    """Return a sorted list of all file extensions accepted by registered
    types. Used by web upload pickers to derive the accept attribute value
    dynamically from the registry rather than hardcoding it."""
    return sorted({e for spec in OBJECT_TYPES.values() for e in spec.extensions})


def classify_url(url):
    """Classify an external URL into a media_type key.

    Algorithm:
        1. Iterate through registered specs (sorted by key for determinism).
        2. If spec.url_match_fn(url) returns True, return that spec's key.
        3. Otherwise, use the spec with url_fallback=True, if any.
        4. Fallback: return "url" (the default generic type).
    """
    # Try matching specs in deterministic order
    for spec in sorted(OBJECT_TYPES.values(), key=lambda s: s.key):
        if spec.url_match_fn and spec.url_match_fn(url):
            return spec.key

    # Use url_fallback spec if available
    for spec in OBJECT_TYPES.values():
        if spec.url_fallback:
            return spec.key

    # Final fallback to generic "url" type
    return "url"


# Every concrete type is registered by its own module in this package (the
# pkgutil loop above imports all of them — `python -c "from core import
# object_types; print(sorted(object_types.OBJECT_TYPES))"` lists the current
# set). This section only defines the DEFAULT_SPEC fallback.


# Anything not registered above (or media_type left unset) — never breaks
# thumbnail/OCR dispatch, it just means "no thumbnail, no OCR" until a real
# spec is registered for it.
DEFAULT_SPEC = ObjectTypeSpec(
    key="unknown",
    label="Unknown",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    badge_icon="❓",
    badge_text="FILE",
)


def get_object_type(media_type):
    return OBJECT_TYPES.get(media_type, DEFAULT_SPEC)


def restricted_types():
    """#443: sorted keys of every registered type with restricted=True."""
    return sorted(k for k, s in OBJECT_TYPES.items() if s.restricted)


def is_restricted(row):
    """#443: True when this row's type is restricted, so it must never be
    exported (core/site_export.py, core/project_export.py)."""
    return get_object_type((row or {}).get("media_type")).restricted


# Re-export extract_youtube_id for backward compatibility with existing callers
# (web/app.py, scripts/* that reference object_types.extract_youtube_id).
# The function has moved to core/object_types/youtube.py as part of the
# object-type plugin architecture refactor.
from . import youtube
extract_youtube_id = youtube.extract_youtube_id
