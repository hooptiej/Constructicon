"""Unpack the Crate (#605): turn an archive item into a card holding its members.

A service module (scripts/check_layering.py): it writes only through the other service ops
(`cards.create`, `membership.add_files`, `items.record_created`) and the ingest pipeline, plus its
own job rows (`unpack_jobs`, private writers in core/db.py).

**Shapes** (owner decision 2026-10-25, answer (a) on #605: no browsable-site mode here, that is #792):
- `collection`: a flat collection; every member is a file on it (the default; reference material).
- `project`: a flat project, the same as a project.
- `nested`: a project whose sub-folders become nested cards (`cards.create(parent=...)`), down to
  `depth` levels (1 or 2, MAX_DEPTH); deeper folders flatten into their level-`depth` ancestor.
  Collections can't nest (card_rules.GROUP_KINDS), so this one is project-only.
When every member sits in one top-level folder (a zip of a folder), that folder is the card itself.
The card is named after the archive (its extension dropped, `%20` and friends decoded); folder cards
after their (decoded) folder names. Nested folder cards get no linked tag (link_tag=False), so a
folder called "images" never joins an existing "images" tag. Each member keeps its decoded in-archive
path in `type_metadata.extracted_from` ({slug, name, path, batch_id}), shown on its page as a
breadcrumb, and the archive's mtime rule: a member's own modified time is its `source_modified_at`,
which #725's date rule counts as a real date only when it is older than the install's first upload.

**Members go through the normal ingest path** (`ingest.ingest_file`: type sniffing, thumbnails,
OCR, the caption queue, embedded metadata, the restricted-type lock), with `run_auto_match=False`
(the conversion files them; nothing joins a card on its own) and the archive's own sensitive flag.
- **Byte-identical** to an item already in the library (#615's rule: same size + sha256) and visible
  to the actor: that item is LINKED onto the card (membership), not stored twice. Two identical
  members of one archive: the second links to the first. An identical item the actor can't see is
  not revealed; a new copy is stored instead.
- **Skipped, and listed with the reason:** junk (`__MACOSX/`, `.DS_Store`, `Thumbs.db`, `desktop.ini`,
  `._*`), links, devices / pipes, encrypted members, an unsupported type (the upload's own words),
  a member over the max upload size, and whatever the ingest pipeline refuses (a content mismatch).
- **Nested archives stay archives** (not unpacked; each can be converted on its own).
- **Safety** is core/archive_reader.py's (paths, links, bomb caps, encryption, corruption), each a
  refusal of the whole archive with its own code and words; nothing is created when one hits.

**Preview first:** `preview(slug, shape, depth)` extracts into a temp folder (to hash), reports
members, sizes, kinds, what is skipped and why, what already exists and will be linked, and the
cards it would make, then deletes the temp folder. Nothing in the library is written.

**One undo:** the whole conversion is ONE `batch_id`: the cards (`cards.create`, imaged), the
archive and every member on them (`membership.add_files`, imaged), and one `item_create_unpacked`
row per item it made (`items.record_created`). `cards.undo(batch_id)` removes the cards and the
memberships and sends every created item to the trash (7 days, like any delete; undoing that undo
restores them). Linked items and the original archive are only taken off the card.

**Background job with progress** (#549: the web process runs it; the MCP only queues). `start()`
inserts an `unpack_jobs` row (queued) and wakes the web worker (`start_worker()`, one thread, one job
at a time); `run_job()` (as the starter, with their channel and personal MCP key replayed: `acting_via`,
#560 / #778) reads, creates the cards, then ingests member by member, writing progress
({phase, done, total, current}) as it goes. Each member's failure is caught, named and kept
(`failed: [{path, reason}]`); the job ends `done`, `partial` (some failed: the message says what was
and wasn't done, and undo still covers what was) or `failed` (nothing made, or stopped: `error` says
why). A job still `running` when the web process starts again was cut off by a restart:
`recover_interrupted()` marks it failed with that reason (what it made is still in its batch, so undo
works). `job_status(id)` is the read the page polls (GET /api/unpack/jobs/{id}) and MCP's
mode=status.

Doors: the archive's type action "unpack" (`run_action`: mode preview | start | status), web
`POST /api/image/{slug}/action/unpack` (editor; viewers see it greyed, #779) and MCP
`constructicon_run_type_action(slug, "unpack", params)`.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import actor as actor_ctx, archive_reader, besteffort, cards, changes, db, duplicates, ingest, \
    install_config, items, membership, object_types, parse_limits, policy, roles, sharing, storage, timeline
from .errors import AppError, Conflict, InvalidInput, NotFound

log = logging.getLogger("constructicon.unpack")

SHAPES = {
    "collection": {"kind": "collection", "nested": False, "label": "a flat collection"},
    "project": {"kind": "project", "nested": False, "label": "a flat project"},
    "nested": {"kind": "project", "nested": True, "label": "a project with nested cards for its folders"},
}
DEFAULT_SHAPE = "collection"
MAX_DEPTH = 2
EXTRACTED_KEY = "extracted_from"
JUNK_DIRS = {"__macosx"}
JUNK_FILES = {".ds_store": "a macOS folder-settings file", "thumbs.db": "a Windows thumbnail cache",
              "desktop.ini": "a Windows folder-settings file"}
PREVIEW_MEMBERS_MAX = 1000          # the preview lists at most this many members (counts are whole)
MAX_FAILURES_IN_A_ROW = 25          # a job stops when this many members fail one after another
PROGRESS_EVERY_SECONDS = 0.5
ACTIVE = ("queued", "running")
FINISHED = ("done", "partial", "failed")
INGEST_SKIP_KINDS = ("unsupported", "content_mismatch", "rejected", "too_large")
UNNAMED_FOLDER = "(unnamed folder)"
INTERRUPTED = ("Stopped: the server restarted while this was running. What it made before the restart is "
               "listed here, and Undo (this job's batch) removes it; run the conversion again for the rest.")


def _refuse(code, message, status=422, **details):
    raise AppError(code, message, status=status, details=details or None)


# --- names ----------------------------------------------------------------------------------------

def decode(part):
    """A path part for display: percent-decoded ("Repair%20and%20Diagnosis" -> "Repair and Diagnosis")."""
    return urllib.parse.unquote(str(part), errors="replace")


def card_title_for(filename):
    """The archive's file name without its archive extension, decoded."""
    name = Path(str(filename or "")).name
    low = name.lower()
    for suffix in sorted(archive_reader.TAR_SUFFIXES + (".zip", ".7z"), key=len, reverse=True):
        if low.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return decode(name).strip() or "Unpacked archive"


def _junk_reason(parts):
    for p in parts[:-1]:
        if p.lower() in JUNK_DIRS:
            return f"junk: inside {p}/ (macOS resource forks)"
    base = parts[-1] if parts else ""
    if base.lower() in JUNK_FILES:
        return f"junk: {JUNK_FILES[base.lower()]}"
    if base.startswith("._"):
        return "junk: a macOS resource fork (._ file)"
    if parts and parts[0].lower() in JUNK_DIRS:
        return f"junk: inside {parts[0]}/ (macOS resource forks)"
    return None


# --- the archive ----------------------------------------------------------------------------------

def _archive_row(slug, actor=None):
    """The archive item, refusing (404 / 403 saying why) what the actor can't see, and anything that
    isn't a stored, unredacted zip / 7z / tar."""
    row = items.get_item(slug, actor)
    if row.get("media_type") != "archive":
        label = object_types.get_object_type(row.get("media_type")).label
        _refuse("not_an_archive", f"{item_name(row)} is a {label}, not an archive, so it can't be unpacked.", 400)
    if row.get("redacted") or not row.get("stored_filename"):
        _refuse("redacted", f"{item_name(row)} has no stored file (it was redacted), so there is nothing to unpack.", 409)
    if archive_reader.archive_format(row.get("filename") or row["stored_filename"]) is None:
        _refuse("archive_unsupported", f"{item_name(row)} isn't a zip, 7z or tar archive, so it can't be unpacked.")
    if not storage.path_for(row["stored_filename"]).is_file():
        _refuse("archive_missing", f"The stored file for {item_name(row)} is missing from storage, so it can't be "
                "unpacked. Nothing was changed.", 409)
    return row


def item_name(row):
    from . import item_title  # lazy: keeps the import list short
    return item_title.title_of(row)


def check_shape(shape, depth):
    shape = (shape or DEFAULT_SHAPE).strip().lower()
    if shape not in SHAPES:
        _refuse("bad_shape", f"Unknown shape {shape!r}: use collection (flat), project (flat) or nested (a project "
                "with nested cards for folders).", 400)
    if not SHAPES[shape]["nested"]:
        return shape, 0
    try:
        depth = MAX_DEPTH if depth in (None, "") else int(depth)
    except (TypeError, ValueError):
        _refuse("bad_shape", f"depth must be 1 or {MAX_DEPTH}, not {depth!r}.", 400)
    if not 1 <= depth <= MAX_DEPTH:
        _refuse("bad_shape", f"depth must be 1 or {MAX_DEPTH} (folder levels that become nested cards), not {depth}.", 400)
    return shape, depth


def _caps_args(caps):
    return {"max_members": caps.max_members, "max_total_bytes": caps.max_total_bytes,
            "max_ratio": caps.max_ratio, "max_seconds": caps.max_seconds}


@contextmanager
def extracted(row):
    """Extracts the archive into a fresh temp folder (in a bounded parser child) and yields the
    reader's answer; the folder is always removed afterwards. A refusal raises AppError with the
    reader's code and words, before anything is created."""
    caps = parse_limits.unpack_caps()
    tmp = tempfile.mkdtemp(prefix="constructicon-unpack-")
    try:
        try:
            answer = parse_limits.call(archive_reader.read_archive, str(storage.path_for(row["stored_filename"])),
                                       row.get("filename") or row["stored_filename"], tmp, _caps_args(caps),
                                       kind=parse_limits.BACKGROUND, what="unpack", seconds=caps.max_seconds + 30)
        except parse_limits.LimitExceeded as e:
            code = "archive_too_slow" if e.kind == "time" else "archive_too_big" if e.kind == "memory" else "archive_corrupt"
            _refuse(code, f"This archive was refused: reading it {e.message}. Nothing was extracted.", kind=e.kind)
        if not answer.get("ok"):
            _refuse(answer.get("code") or "archive_corrupt", answer.get("message") or "This archive can't be read.",
                    **(answer.get("details") or {}))
        answer["caps"] = caps.as_dict()
        yield answer
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError as e:
            besteffort.warn(log, "unpack temp cleanup", e, folder=tmp)


# --- byte-identical items already in the library (#615's rule) -------------------------------------

class _Existing:
    """Finds a stored, unredacted item with the same size and sha256 as a member. Sizes the stored
    files once (on first use), hashes only same-size ones, each at most once."""

    def __init__(self):
        self._by_size = None
        self._digests = {}

    def _load(self):
        self._by_size = {}
        for slug, sf in duplicates.stored_files():
            p = storage.path_for(sf)
            try:
                size = p.stat().st_size
            except OSError as e:
                besteffort.warn(log, "unpack: size a stored file", e, slug=slug)
                continue
            self._by_size.setdefault(size, []).append((slug, p))

    def find(self, size, digest, actor=None):
        if not size or not digest:
            return None
        if self._by_size is None:
            self._load()
        for slug, p in self._by_size.get(size, []):
            if slug not in self._digests:
                try:
                    self._digests[slug] = duplicates.sha256_of(p)
                except OSError as e:
                    besteffort.warn(log, "unpack: hash a stored file", e, slug=slug)
                    self._digests[slug] = None
            if self._digests[slug] == digest:
                row = db.get_by_slug(slug)
                if row is not None and policy.can_view(row, actor):
                    return row
        return None


# --- the plan -------------------------------------------------------------------------------------

def _mtime(e):
    if e.get("mtime"):
        return float(e["mtime"])
    if e.get("mtime_tuple"):
        try:
            return timeline.source_datetime_to_epoch(datetime(*e["mtime_tuple"][:6]))
        except (TypeError, ValueError):
            return None  # silent-ok: an impossible zip date is "no date", like the zip format's own 1980 sentinel
    return None


def _ext_label(name):
    return Path(name).suffix.lower() or "(none)"


def analyse(row, answer, shape, depth, actor=None, existing=None):
    """What the conversion would do with the reader's `answer` (pure: reads the library, writes
    nothing). Returns the plan dict (see preview())."""
    spec = SHAPES[shape]
    entries = answer["entries"]
    junk_free = [e for e in entries if e["parts"] and not _junk_reason(e["parts"])]
    files_or_dirs = [e for e in junk_free if e["kind"] in ("file", "link", "special", "dir")]
    wrapper = None
    tops = {e["parts"][0] for e in files_or_dirs}
    if len(tops) == 1 and all(len(e["parts"]) > 1 or e["kind"] == "dir" for e in files_or_dirs):
        wrapper = next(iter(tops))
    existing = existing or _Existing()
    title = card_title_for(row.get("filename") or row.get("stored_filename"))
    members, skipped, by_type, card_keys = [], [], {}, set()
    planned_by_sha = {}
    max_depth = 0
    folders = 0
    for e in entries:
        parts = e["parts"]
        if not parts:
            continue
        rel = parts[1:] if wrapper and parts and parts[0] == wrapper else parts
        if e["kind"] == "dir":
            folders += 0 if _junk_reason(parts) else 1
            continue
        path = "/".join(decode(p) for p in parts)
        folder = tuple(decode(p) or UNNAMED_FOLDER for p in rel[:-1])
        max_depth = max(max_depth, len(folder))
        card_key = folder[:depth] if spec["nested"] else ()
        filename = decode(parts[-1]).strip() or "unnamed"
        m = {"index": e["index"], "path": path, "filename": filename, "size": e["size"], "folder": "/".join(folder),
             "card_key": card_key, "file": e.get("file"), "sha256": e.get("sha256"), "mtime": _mtime(e)}
        junk = _junk_reason(parts)
        if not junk:
            by_type[_ext_label(filename)] = by_type.get(_ext_label(filename), 0) + 1
        why = junk or e.get("reason")
        if not why and e["kind"] != "file":
            why = archive_reader.LINK_REASON if e["kind"] == "link" else archive_reader.SPECIAL_REASON
        media_type = None
        if not why:
            media_type = object_types.detect_media_type(filename)
            if media_type is None:
                why = f"Unsupported file type: {Path(filename).suffix or '(no extension)'}"
            elif e["size"] > storage.MAX_BYTES:
                why = f"larger than the {storage.MAX_MB} MB upload limit"
            elif not e.get("file"):
                why = "it couldn't be extracted"
        if why:
            skipped.append({"path": path, "size": e["size"], "reason": why})
            continue
        spec_t = object_types.get_object_type(media_type)
        m.update(media_type=media_type, type_label=spec_t.label,
                 picture=spec_t.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE)
        notes = []
        if media_type == "archive":
            notes.append("kept as an archive (not unpacked; convert it on its own)")
        if policy.is_type_restricted({"media_type": media_type}):
            notes.append(f"comes in locked: a {spec_t.label} is a restricted type (admins and its uploader only)")
        found = existing.find(e["size"], e.get("sha256"), actor)
        if found is not None:
            m.update(action="link", existing_slug=found["slug"], existing_title=item_name(found))
            notes.append(f"byte-identical to {item_name(found)!r}, already in the library: linked, not stored twice")
        elif e.get("sha256") and e["size"] and e["sha256"] in planned_by_sha:
            m.update(action="link_member", same_as=planned_by_sha[e["sha256"]])
            notes.append(f"byte-identical to {planned_by_sha[e['sha256']]!r} in this archive: linked to that one")
        else:
            m["action"] = "ingest"
            if e.get("sha256") and e["size"]:
                planned_by_sha[e["sha256"]] = path
        m["notes"] = notes
        members.append(m)
        for n in range(1, len(card_key) + 1):
            card_keys.add(card_key[:n])
    cards_plan = [{"key": (), "title": title, "kind": spec["kind"], "parent": None, "files": 0}]
    for key in sorted(card_keys, key=lambda k: (len(k), k)):
        cards_plan.append({"key": key, "title": key[-1], "kind": "project", "parent": key[:-1], "files": 0})
    counts = {}
    for m in members:
        counts[m["card_key"]] = counts.get(m["card_key"], 0) + 1
    for c in cards_plan:
        c["files"] = counts.get(c["key"], 0) + (1 if c["key"] == () else 0)  # the archive itself sits on the root
        c["path"] = "/".join(c["key"])
    return {"archive": {"slug": row["slug"], "name": item_name(row), "filename": row.get("filename")},
            "format": answer["format"], "shape": shape, "shape_label": spec["label"], "depth": depth,
            "card_title": title, "kind": spec["kind"], "wrapper_folder": decode(wrapper) if wrapper else None,
            "members": members, "skipped": skipped, "cards": cards_plan, "folders": folders, "max_depth": max_depth,
            "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
            "totals": {"entries": len(entries), "files": len(members) + len(skipped), "folders": folders,
                       "bytes": sum(m["size"] for m in members) + sum(s["size"] for s in skipped),
                       "to_ingest": sum(1 for m in members if m["action"] == "ingest"),
                       "to_link": sum(1 for m in members if m["action"] != "ingest"),
                       "skipped": len(skipped), "cards": len(cards_plan)},
            "caps": answer.get("caps"), "private": policy.outsider_access().item_hidden(row)}


def _public_member(m):
    out = {k: m.get(k) for k in ("path", "size", "action", "type_label", "notes", "existing_slug", "existing_title",
                                 "same_as")}
    out["card"] = "/".join(m["card_key"]) or None
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _public_plan(plan):
    members = plan["members"]
    out = {k: v for k, v in plan.items() if k not in ("members", "cards")}
    out["members"] = [_public_member(m) for m in members[:PREVIEW_MEMBERS_MAX]]
    out["members_truncated"] = len(members) > PREVIEW_MEMBERS_MAX
    out["cards"] = [{"title": c["title"], "kind": c["kind"], "path": c["path"] or None,
                     "parent": "/".join(c["parent"]) if c["parent"] else (None if c["key"] == () else ""),
                     "files": c["files"]} for c in plan["cards"]]
    t = plan["totals"]
    extra = (f" Every file sits in the folder {plan['wrapper_folder']!r}, so that folder is the card itself."
             if plan["wrapper_folder"] else "")
    nested = f" with {t['cards'] - 1} nested card(s)" if t["cards"] > 1 else ""
    out["message"] = (f"Unpacking would make {plan['shape_label']} named {plan['card_title']!r}{nested}: "
                      f"{t['to_ingest']:,} new item(s), {t['to_link']:,} linked to identical items, "
                      f"{t['skipped']:,} skipped (each with its reason), plus the archive itself on the card.{extra} "
                      "Nothing has been written yet.")
    if plan.get("private"):
        out["message"] += " The archive is on a private card, so the new card will be private too."
    out["nothing_written"] = True
    return out


def preview(slug, shape=DEFAULT_SHAPE, depth=None, *, actor=None):
    """The dry run (see the module docstring). Writes nothing to the library."""
    row = _archive_row(slug, actor)
    shape, depth = check_shape(shape, depth)
    with extracted(row) as answer:
        plan = analyse(row, answer, shape, depth, actor)
    return _public_plan(plan)


# --- jobs -----------------------------------------------------------------------------------------

def _job_public(job):
    if job is None:
        return None
    p, r = job.get("progress") or {}, job.get("result") or {}
    out = {"id": job["id"], "archive_slug": job["archive_slug"], "status": job["status"], "shape": job["shape"],
           "depth": job["depth"] or None, "batch_id": job["batch_id"], "created_at": job["created_at"],
           "started_at": job["started_at"], "finished_at": job["finished_at"], "error": job.get("error"),
           "progress": p, "result": r, "finished": job["status"] in FINISHED}
    out["message"] = r.get("message") or job.get("error") or p.get("message") or {
        "queued": "Waiting to start.", "running": "Working."}.get(job["status"], "")
    return out


def start(slug, shape=DEFAULT_SHAPE, depth=None, *, actor=None):
    """Queues the conversion (see the module docstring). -> the job (status queued)."""
    row = _archive_row(slug, actor)
    shape, depth = check_shape(shape, depth)
    busy = db.list_unpack_jobs(status=ACTIVE, archive_slug=row["slug"], limit=1)
    if busy:
        raise Conflict(f"{item_name(row)} is already being unpacked (job {busy[0]['id']}, {busy[0]['status']}); wait "
                       "for it to finish.", code="unpack_busy", details={"job": busy[0]["id"]})
    who = actor_ctx.resolve(actor)
    # #560 / #778: how the starter reached the app and their personal MCP key, replayed by run_job, so
    # the members say "Arrived via the MCP" and "<user>'s <key name>" when a personal key started it.
    job_id = db._insert_unpack_job(row["slug"], changes.new_batch_id(), who, shape, depth,
                                   channel=actor_ctx.current_channel(), via_token_id=actor_ctx.token_id_for(who))
    wake_worker()
    return _job_public(db.get_unpack_job(job_id))


def job_status(job_id, *, actor=None):
    """The job, for the page's poll and MCP's mode=status. 404 when there is no such job or the actor
    can't see its archive (a job reveals nothing about an item the actor can't see)."""
    try:
        job = db.get_unpack_job(int(job_id))
    except (TypeError, ValueError):
        raise InvalidInput(f"job must be a job number, not {job_id!r}.")
    if job is None:
        raise NotFound(f"No unpack job {job_id}.")
    row = db.get_by_slug(job["archive_slug"])
    if row is not None:
        policy.require_view(row, actor, message=f"No unpack job {job_id}.")
    elif roles.role_of(actor_ctx.resolve(actor)) != roles.ADMIN:
        raise NotFound(f"No unpack job {job_id}.")
    return _job_public(job)


def run_action(row, params):
    """The archive type's "unpack" action (both doors): mode preview (default) | start | status."""
    mode = (params.get("mode") or "preview").strip().lower()
    if mode == "status":
        job = job_status(params.get("job"))
        if job["archive_slug"] != row["slug"]:
            raise NotFound(f"No unpack job {params.get('job')} for this archive.")
        return {"job": job, "message": job["message"]}
    if mode == "start":
        job = start(row["slug"], params.get("shape"), params.get("depth"))
        return {"job": job, "message": f"Unpacking started (job {job['id']}). It runs in the background; this "
                                      "page shows its progress, and Undo removes the whole conversion."}
    if mode == "preview":
        plan = preview(row["slug"], params.get("shape"), params.get("depth"))
        return {"preview": plan, "message": plan["message"]}
    raise InvalidInput(f"Unknown mode {mode!r}: use preview, start or status.")


# --- running a job --------------------------------------------------------------------------------

class _Progress:
    def __init__(self, job_id):
        self.job_id, self.last = job_id, 0.0
        self.state = {"phase": "reading", "done": 0, "total": 0, "current": None,
                      "message": "Reading and checking the archive."}

    def set(self, force=False, **kw):
        self.state.update(kw)
        now = time.monotonic()
        if force or now - self.last >= PROGRESS_EVERY_SECONDS:
            self.last = now
            db._update_unpack_job(self.job_id, progress=dict(self.state))


_pool_lock = threading.Lock()
_pool = None


def _background(fn, *args):
    """Post-insert work (OCR, thumbnails) for unpacked members: two at a time, carrying the actor,
    instead of one thread per member (a thousand members would start a thousand threads)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="unpack-post")
    _pool.submit(actor_ctx.carry(_logged(fn)), *args)


def _logged(fn):
    def run(*args):
        try:
            return fn(*args)
        except Exception as e:  # a pool swallows exceptions; this one is logged, never silent
            besteffort.warn(log, f"unpack post-processing ({getattr(fn, '__name__', fn)})", e, args=args)
    return run


def _make_cards(plan, row, batch_id, actor):
    """Every card of the plan plus the archive on the root card, one transaction, the job's batch.
    An archive under a private card or hobby makes the new card private too (#604: its members
    would otherwise sit on an open card, visible to everyone); the result's message says so."""
    ids = {}
    private = policy.outsider_access().item_hidden(row)
    with db.transaction():
        for c in plan["cards"]:
            if c["key"] == ():
                made = cards.create(c["title"], kind=c["kind"], actor=actor, batch_id=batch_id).data["card"]
            else:
                made = cards.create(c["title"], kind="project", parent=ids[c["parent"]], link_tag=False,
                                    actor=actor, batch_id=batch_id).data["card"]
            ids[c["key"]] = made["id"]
            if c["key"] == () and private:
                sharing.set_private("card", made["id"], True, actor=actor, batch_id=batch_id)
        membership.add_files(ids[()], [row["slug"]], link_tag=True, auto_cover=False, actor=actor, batch_id=batch_id)
    return ids, private


def _ingest_member(m, row, plan, batch_id, source, bg, actor):
    """-> ("created", slug) | ("linked", slug, why) | ("skipped", reason) | ("failed", reason)."""
    meta = {EXTRACTED_KEY: {"slug": row["slug"], "name": plan["archive"]["filename"] or plan["archive"]["name"],
                            "path": m["path"], "batch_id": batch_id}}
    with open(m["file"], "rb") as f:
        res = ingest.ingest_file(f, m["filename"], size=m["size"], source=source, run_background=bg,
                                 source_modified_at=m["mtime"], sensitive=bool(row.get("sensitive")),
                                 type_metadata=meta, run_auto_match=False)
    if res.error:
        return ("skipped", res.error) if res.error_kind in INGEST_SKIP_KINDS else ("failed", res.error)
    if res.duplicate:
        return ("linked", res.row["slug"], "same name, size and date as an item already in the library")
    items.record_created(res.row["slug"], batch_id=batch_id, actor=actor,
                         details={"archive": row["slug"], "path": m["path"], "job_batch": batch_id})
    return ("created", res.row["slug"])


def _run(job, bg):
    job_id, batch_id, actor = job["id"], job["batch_id"], job["actor"]
    prog = _Progress(job_id)
    prog.set(force=True)
    row = db.get_by_slug(job["archive_slug"])
    if row is None or row.get("redacted"):
        _refuse("archive_missing", "The archive was deleted or redacted after this was queued; nothing was made.", 409)
    row = _archive_row(row["slug"], actor)
    created, linked, failed = [], [], []
    skipped = []
    card_ids, root = {}, None
    with extracted(row) as answer:
        prog.set(force=True, phase="planning", message="Checking members against the library.")
        plan = analyse(row, answer, job["shape"], job["depth"] or 0, actor)
        skipped = list(plan["skipped"])
        todo = plan["members"]
        if not todo:
            _refuse("nothing_to_unpack", f"Nothing to unpack: all {len(skipped):,} member(s) were skipped "
                    f"({'; '.join(sorted({s['reason'] for s in skipped})[:5]) or 'the archive is empty'}). Nothing was made.",
                    skipped=skipped[:200])
        prog.set(force=True, phase="cards", total=len(todo), message="Making the card(s).")
        card_ids, private = _make_cards(plan, row, batch_id, actor)
        root = db.get_project(card_ids[()])
        source = f"{install_config.owner_label()} — extracted from {plan['archive']['filename'] or plan['archive']['name']}"
        by_path = {}
        in_a_row = 0
        stopped = None
        processed = 0
        for i, m in enumerate(todo):
            processed = i + 1
            prog.set(phase="members", done=i, total=len(todo), current=m["path"],
                     message=f"Unpacking {i + 1:,} of {len(todo):,}: {m['path']}")
            try:
                if m["action"] == "link":
                    outcome = ("linked", m["existing_slug"], "byte-identical to an item already in the library")
                elif m["action"] == "link_member" and m["same_as"] in by_path:
                    outcome = ("linked", by_path[m["same_as"]], f"byte-identical to {m['same_as']!r} in this archive")
                else:
                    outcome = _ingest_member(m, row, plan, batch_id, source, bg, actor)
                if outcome[0] in ("created", "linked"):
                    membership.add_files(card_ids[m["card_key"]], [outcome[1]], link_tag=True,
                                         auto_cover=bool(m.get("picture")), actor=actor, batch_id=batch_id)
                    by_path[m["path"]] = outcome[1]
                    (created if outcome[0] == "created" else linked).append(
                        {"path": m["path"], "slug": outcome[1], **({"why": outcome[2]} if outcome[0] == "linked" else {})})
                elif outcome[0] == "skipped":
                    skipped.append({"path": m["path"], "size": m["size"], "reason": outcome[1]})
                else:
                    failed.append({"path": m["path"], "reason": outcome[1]})
                in_a_row = in_a_row + 1 if outcome[0] == "failed" else 0
            except Exception as e:
                besteffort.warn(log, "unpack member", e, job=job_id, member=m["path"])
                failed.append({"path": m["path"], "reason": object_types.short_error(e)})
                in_a_row += 1
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                stopped = (f"Stopped after {in_a_row} members in a row failed (the last: {failed[-1]['path']}: "
                           f"{failed[-1]['reason']}).")
                break
        prog.set(force=True, phase="finishing", done=processed, current=None, message="Finishing.")
    return _summary(plan, root, card_ids, created, linked, skipped, failed, stopped, len(todo) - processed,
                    batch_id, private)


def _summary(plan, root, card_ids, created, linked, skipped, failed, stopped, not_reached, batch_id, private):
    card = {"id": root["id"], "slug": root["slug"], "title": root["title"], "url": f"/project/{root['slug']}"} if root else None
    head = (f"Unpacked {plan['archive']['name']!r} into {plan['shape_label']} {card['title']!r}: "
            f"{len(created):,} new item(s), {len(linked):,} linked, {len(skipped):,} skipped") if card else ""
    parts = [head + (f", {len(failed):,} failed" if failed else "") + "."]
    if failed:
        parts.append("Failed: " + "; ".join(f"{f['path']} ({f['reason']})" for f in failed[:10])
                     + (f"; and {len(failed) - 10} more" if len(failed) > 10 else "") + ".")
    if stopped:
        parts.append(stopped + f" {not_reached:,} member(s) were not reached.")
    if private:
        parts.append("The archive is on a private card, so the new card is private too (add people under its Sharing).")
    if failed or stopped:
        parts.append("Everything listed as new or linked IS on the card; Undo removes all of it.")
    status = "partial" if (failed or stopped) else "done"
    return status, {"message": " ".join(parts), "card": card, "cards_made": len(card_ids), "batch_id": batch_id,
                    "created": created, "linked": linked, "skipped": skipped, "failed": failed,
                    "not_reached": max(not_reached, 0), "stopped": stopped, "private": private,
                    "counts": {"created": len(created), "linked": len(linked), "skipped": len(skipped),
                               "failed": len(failed)}}


def run_job(job_id, *, run_background=None):
    """Runs one queued job to the end (the worker calls this; tests call it directly). Returns the job."""
    if not db._claim_unpack_job(job_id):
        return _job_public(db.get_unpack_job(job_id))
    job = db.get_unpack_job(job_id)
    # Runs AS the starter, arriving the way they did (#560 / #778): acting_via ties the key to that actor.
    with actor_ctx.acting_as(job["actor"]), \
            actor_ctx.acting_via(job.get("channel") or actor_ctx.channel_for_actor(job["actor"]), job.get("via_token_id")):
        try:
            status, result = _run(job, run_background or _background)
            db._update_unpack_job(job_id, status=status, result=result, finished_at=time.time())
        except AppError as e:
            made = bool(db.get_change_rows(batch_id=job["batch_id"]))
            msg = e.message + (" Some cards were made before it stopped; Undo removes them." if made else "")
            db._update_unpack_job(job_id, status="failed", error=msg, finished_at=time.time(),
                                  result={"message": msg, "code": e.code, "details": e.details,
                                          "batch_id": job["batch_id"] if made else None})
        except Exception as e:
            log.exception("unpack job %s failed", job_id)
            made = bool(db.get_change_rows(batch_id=job["batch_id"]))
            msg = (f"The conversion failed: {object_types.short_error(e)}."
                   + (" What it made before that is in its batch; Undo removes it." if made else " Nothing was made."))
            db._update_unpack_job(job_id, status="failed", error=msg, finished_at=time.time(),
                                  result={"message": msg, "code": "unpack_failed",
                                          "batch_id": job["batch_id"] if made else None})
    return _job_public(db.get_unpack_job(job_id))


def recover_interrupted():
    """Web startup: a job left `running` was cut off by a restart. Marks it failed, saying so."""
    n = 0
    for job in db.list_unpack_jobs(status="running", limit=1000):
        result = dict(job.get("result") or {})
        result.update(message=INTERRUPTED, batch_id=job["batch_id"])
        n += db._update_unpack_job(job["id"], status="failed", error=INTERRUPTED, result=result,
                                   finished_at=time.time())
    if n:
        print(f"unpack: {n} job(s) were cut off by a restart and marked failed (undo still covers what they made)",
              flush=True)
    return n


# --- the web worker -------------------------------------------------------------------------------

_wake = threading.Event()
_worker_thread = None
_worker_guard = threading.Lock()
IDLE_POLL_SECONDS = 5


def wake_worker():
    _wake.set()


def _worker_loop():
    while True:
        try:
            queued = db.list_unpack_jobs(status="queued", limit=1000)
            if queued:
                run_job(queued[-1]["id"])  # oldest first
                continue
        except Exception as e:  # the loop must outlive one bad job; the job row already says why
            besteffort.warn(log, "unpack worker", e)
        _wake.wait(IDLE_POLL_SECONDS)
        _wake.clear()


def start_worker():
    """Starts the single unpack thread (idempotent). Web process only (#549: the MCP queues)."""
    global _worker_thread
    with _worker_guard:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_thread = threading.Thread(target=actor_ctx.carry(_worker_loop), name="unpack-jobs", daemon=True)
        _worker_thread.start()
        return True
