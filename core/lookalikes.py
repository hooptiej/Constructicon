"""Look-alike photos: the same picture under different bytes, confirmed by a person (#759).

#615 (core/duplicates.py) folds only BYTE-IDENTICAL files. A photo that was resized, recompressed,
cropped or re-exported (a Facebook download, a Wix migration) has other bytes and survives it. This
pass finds those by perceptual hash and ASKS; it never folds on its own.

  * Candidates: images only, by the perceptual hash the OCR pipeline already stores
    (capture_events.perceptual_hash; the grouping itself is core/lookalike_match.py, stdlib only).
    Left out: redacted items (deleted ones are gone from the table), byte-identical pairs (#615's job),
    pairs in one revision chain (#477: versions, not copies), and pairs the owner answered
    "Not the same". A group with an item the actor may not see is left out and only counted.
  * "Not the same" is remembered PER PAIR in curator_dismissals (key `need:lookalike_pair:<a>:<b>`,
    action dismiss; one imaged change-log row, undoable). A group that later gains a new member comes
    back with only the newcomer and what it matches; earlier answers are listed under `answered`.
  * "Same photo, keep this one" reuses #615's fold (duplicates.fold_group): the kept copy inherits the
    others' cards, tags, blank text fields, flags, related links and covers; the others go to the trash
    (7-day undo), all in ONE batch. A member the owner already said is not the same as the kept copy
    (or as another copy being folded) is left out of the fold and listed under `left_out` with why.
  * The suggested keeper follows #615's survivor rule (duplicates.rank_copies, including #768's macOS
    " 2" suffix), with its `why`.

  plan(actor=None)                 read-only: {summary, groups, hidden_groups}
  confirm(keep, slugs=None, ...)   editor: fold a group into `keep` (dry_run supported)
  dismiss(slugs, ...)              editor: "Not the same" for the pairs among `slugs`
  queue_items()                    the Curator queue's "these look like the same photo" items

Measure first on a read-only copy of prod: scripts/measure_lookalikes_759.py.
"""

import hashlib
import time

from . import actor as actor_ctx
from . import changes, datefmt, db, duplicates, item_title, items, lookalike_match, paths, policy, roles
from .cards import Result
from .errors import AppError, Conflict, InvalidInput

NEED_KIND = "lookalike"
PAIR_PREFIX = "need:lookalike_pair:"
OP_FOLD = "lookalike_fold"
OP_NOT_SAME = "lookalike_not_same"
MAX_DISTANCE = lookalike_match.MAX_DISTANCE

LABEL = "These look like the same photo: keep which?"


def pair_key(a, b):
    a, b = lookalike_match.pair(a, b)
    return f"{PAIR_PREFIX}{a}:{b}"


def _dismissed_pairs():
    out = set()
    for key, action in db.list_curator_states().items():
        if action == db.CURATOR_DISMISS and key.startswith(PAIR_PREFIX):
            a, _, b = key[len(PAIR_PREFIX):].partition(":")
            if a and b:
                out.add(lookalike_match.pair(a, b))
    return out


# sha256 of a stored file, keyed by (path, size, mtime): only distance-0 pairs are ever hashed.
_SHA_CACHE = {}


class _Identical:
    """identical(a, b) for lookalike_match.build: same size and sha256 on disk. A file that can't be
    read is never called identical; it is recorded in `unreadable` with the reason (never silent)."""

    def __init__(self, rows):
        self.stored = {r["slug"]: r["stored_filename"] for r in rows}
        self.unreadable = {}

    def _sig(self, slug):
        p = paths.storage_dir() / self.stored[slug]
        try:
            st = p.stat()
            key = (str(p), st.st_size, st.st_mtime_ns)
            if key not in _SHA_CACHE:
                _SHA_CACHE[key] = duplicates._sha256(p)
            return st.st_size, _SHA_CACHE[key]
        except OSError as e:
            self.unreadable[slug] = f"can't read the stored file: {e}"
            return None

    def __call__(self, a, b):
        sa, sb = self._sig(a), self._sig(b)
        return sa is not None and sa == sb


def _dimensions(row):
    """(width, height, error): read from the image file's header. A file Pillow can't open says why."""
    try:
        from PIL import Image  # lazy: only when a group is described
        with Image.open(paths.storage_dir() / row["stored_filename"]) as im:
            w, h = im.size
        return w, h, None
    except Exception as e:  # noqa: BLE001 -- reported on the member, never hidden
        return None, None, f"can't read the image size: {type(e).__name__}: {e}"


def _member(f):
    row = f["row"]
    w, h, err = _dimensions(row)
    slug = row["slug"]
    size = row.get("file_size")
    if size is None:
        try:
            size = (paths.storage_dir() / row["stored_filename"]).stat().st_size
        except OSError:  # silent-ok: size stays unknown (null) and the dimensions error says the file is unreadable
            size = None
    out = {"slug": slug, "title": item_title.title_of(row), "filename": row.get("filename"),
           "width": w, "height": h, "file_size": size,
           "date": f["day"], "date_is_real": f["date"] is not None,
           "uploaded": datefmt.iso_day(row.get("timestamp")),
           "cards": [c["title"] for c in f["cards"]],
           "copy_marker": f.get("copy_marker"),
           "thumb_url": f"/f/{slug}/thumb", "url": f"/object/{slug}"}
    if err:
        out["dimensions_error"] = err
    return out


def _group_id(members):
    return hashlib.sha1(",".join(sorted(members)).encode()).hexdigest()[:16]


def _scan():
    """(rows by slug, lookalike_match.build result, unreadable, considered count)."""
    rows = db.list_lookalike_images()
    by_slug = {r["slug"]: r for r in rows}
    hashes = {r["slug"]: r.get("perceptual_hash") for r in rows if r.get("perceptual_hash")}
    ident = _Identical(rows)
    built = lookalike_match.build(hashes, chains=lookalike_match.chain_index(db.revision_pairs()),
                                  dismissed=_dismissed_pairs(), identical=ident)
    return by_slug, built, ident.unreadable, len(rows)


def _describe(g, by_slug, cutoff, with_members=True):
    rows = [by_slug[s] for s in g["members"]]
    ranked = duplicates.rank_copies(rows, cutoff)
    keep = ranked[0]
    answered = [list(p) for p in g["answered"]]
    in_answered = {s for p in g["answered"] for s in p}
    out = {
        "id": _group_id(g["members"]),
        "slugs": [f["slug"] for f in ranked],
        "keep": keep["slug"],
        "why": duplicates._why(keep, ranked[1]),
        "pairs": [{"a": a, "b": b, "distance": d} for (a, b), d in sorted(g["pairs"].items())],
        "max_distance": MAX_DISTANCE,
        "answered": answered,
        "new_members": sorted(s for s in g["members"] if s not in in_answered) if answered else [],
    }
    out["key"] = f"need:{NEED_KIND}:{out['id']}"
    if with_members:
        out["members"] = [_member(f) for f in ranked]
    out["_ranked"] = [f["slug"] for f in ranked]
    return out


def _groups(who, with_members=True):
    by_slug, built, unreadable, considered = _scan()
    cutoff = duplicates.first_upload_cutoff()
    groups, hidden = [], 0
    for g in built["groups"]:
        if not all(policy.can_view(by_slug[s], who) for s in g["members"]):
            hidden += 1  # counted, never named: the actor may not see one of its photos
            continue
        groups.append(_describe(g, by_slug, cutoff, with_members))
    summary = {
        "images_considered": considered,
        "with_hash": built["hashed"],
        "without_usable_hash": considered - built["hashed"],
        "groups": len(groups),
        "photos_in_groups": sum(len(g["slugs"]) for g in groups),
        "hidden_groups": hidden,
        "max_distance": MAX_DISTANCE,
        "excluded_pairs": built["excluded_pairs"],
        "excluded_groups": built["excluded_groups"],
        "unreadable_files": len(unreadable),
    }
    return {"summary": summary, "groups": groups, "built": built, "by_slug": by_slug,
            "unreadable": [{"slug": s, "reason": r} for s, r in sorted(unreadable.items())]}


def _public(g):
    return {k: v for k, v in g.items() if not k.startswith("_")}


def plan(actor=None):
    """Read-only. {summary, groups: [{id, key, slugs, keep, why, members: [{slug, title, filename,
    width, height, file_size, date, uploaded, cards, ...}], pairs: [{a, b, distance}], answered,
    new_members}], unreadable}. Groups are largest first; members best keeper first."""
    who = actor_ctx.resolve(actor)
    res = _groups(who)
    return {"summary": res["summary"], "groups": [_public(g) for g in res["groups"]], "unreadable": res["unreadable"]}


# --- writing -----------------------------------------------------------------------------

def _require_editor(who, what):
    if not roles.at_least(roles.role_of(who), roles.EDITOR):
        raise AppError("forbidden", f"{what} needs the editor role, and {who} has {roles.role_of(who)}. "
                       "Nothing was changed.", status=403)


def _group_of(slug, groups):
    for g in groups:
        if slug in g["slugs"]:
            return g
    return None


def _fold_set(g, keep, wanted, dismissed):
    """(fold, left_out): `keep` first, then each wanted member (best keeper first) unless the owner said
    it is not the same as one already in. Explicit picks that conflict raise lookalike_conflict."""
    fold, left_out = [keep], []
    for s in g["_ranked"]:
        if s == keep or s not in wanted:
            continue
        clash = next((x for x in fold if lookalike_match.pair(s, x) in dismissed), None)
        if clash is None:
            fold.append(s)
        else:
            left_out.append({"slug": s, "reason": f"you said it is not the same photo as {clash}"})
    return fold, left_out


def confirm(keep, slugs=None, *, shown=None, dry_run=False, actor=None, batch_id=None):
    """"Same photo, keep this one": folds the look-alike group holding `keep` into it with #615's fold
    (duplicates.fold_group; one batch, one undo). `slugs` (optional) = the members to fold, picked one by
    one, from the same group; default every member. `shown` (the Curator's buttons) = the members the
    person was looking at: only those are folded (a newcomer that joined since stays asked), and one
    answered "Not the same" is left out and listed rather than refused. Editor. Refusals: 403 forbidden (role, or a photo the actor may not
    see), 404 not_found (unknown slug), 409 not_in_group (the keeper or a named slug is in no current
    look-alike group with it), 409 lookalike_conflict (a named slug was answered "Not the same" against
    the keeper or another named slug), 409 nothing_to_fold. Returns a Result; data: {group, kept,
    trashed, left_out, plan, expires_at, trash_days}."""
    who = actor_ctx.resolve(actor)
    _require_editor(who, "Folding look-alike photos")
    if not keep or not isinstance(keep, str):
        raise InvalidInput("Name the photo to keep (keep=<slug>).")
    named = [s for s in dict.fromkeys(slugs or []) if s and s != keep]
    seen = [s for s in dict.fromkeys(shown or []) if s and s != keep]
    if named and seen:
        raise InvalidInput("Give slugs (the copies to fold) or shown (the copies on screen), not both.")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        policy.viewable_rows([keep] + named + seen, actor=who)  # 404 unknown, 403 hidden (#709)
        res = _groups(who, with_members=False)
        g = _group_of(keep, res["groups"])
        if g is None:
            raise Conflict(f"Nothing was changed: {keep} is not in any look-alike group now (it may have been "
                           "folded or answered already). List the groups again.", code="not_in_group")
        stray = [s for s in named + seen if s not in g["slugs"]]
        if stray:
            raise Conflict(f"Nothing was changed: {', '.join(stray)} {'is' if len(stray) == 1 else 'are'} not in "
                           f"the look-alike group with {keep} (its members: {', '.join(g['slugs'])}).",
                           code="not_in_group", details={"not_in_group": stray, "group": g["slugs"]})
        dismissed = _dismissed_pairs()
        wanted = set(named or seen) or set(g["slugs"])
        fold, left_out = _fold_set(g, keep, wanted, dismissed)
        if named and left_out:
            raise Conflict("Nothing was changed: " + "; ".join(f"{x['slug']}: {x['reason']}" for x in left_out)
                           + ". Leave it out, or undo that answer first.", code="lookalike_conflict",
                           details={"conflicts": left_out})
        if len(fold) < 2:
            raise Conflict(f"Nothing was changed: no other photo in the group can be folded into {keep}"
                           + (f" ({'; '.join(x['slug'] + ': ' + x['reason'] for x in left_out)})" if left_out else "")
                           + ".", code="nothing_to_fold", details={"left_out": left_out})
        out = duplicates.fold_group(keep, fold[1:], op=OP_FOLD, dry_run=dry_run, actor=who, batch_id=batch_id)
    return Result(True, out.changes, [f"left out {x['slug']}: {x['reason']}" for x in left_out], batch_id, dry_run,
                  {**out.data, "group": g["id"], "left_out": left_out})


def dismiss(slugs, *, dry_run=False, actor=None, batch_id=None):
    """"Not the same": remembers every current look-alike pair among `slugs` (2+ photos of one group)
    so it is never asked again. One imaged change-log row (op lookalike_not_same), undoable. Editor.
    Refusals: 403 forbidden, 404 not_found, 400 bad_request (fewer than two photos), 409 not_lookalikes
    (no open look-alike pair among them). Returns a Result; data: {pairs: [[a, b]...], group}."""
    who = actor_ctx.resolve(actor)
    _require_editor(who, "Answering a look-alike question")
    named = [s for s in dict.fromkeys(slugs or []) if s]
    if len(named) < 2:
        raise InvalidInput("Name at least two photos (slugs=[...]) to say they are not the same.")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        policy.viewable_rows(named, actor=who)
        res = _groups(who, with_members=False)
        pairs = res["built"]["pairs"]
        ns = set(named)
        todo = sorted(p for p in pairs if p[0] in ns and p[1] in ns)
        if not todo:
            raise Conflict("Nothing was changed: these photos are no longer an open look-alike question (already "
                           "answered, folded, or no longer alike). List the groups again.", code="not_lookalikes")
        g = _group_of(todo[0][0], res["groups"])
        with db.ImageLog(OP_NOT_SAME, who, batch_id, named) as log:
            for a, b in todo:
                key = {"nudge_key": pair_key(a, b)}
                if log.get("curator_dismissals", key) is None:
                    log.insert("curator_dismissals", key, {"action": db.CURATOR_DISMISS, "snooze_until": None,
                                                           "created_at": time.time()})
            muts = list(log.muts)
    return Result(True, items._flat(OP_NOT_SAME, muts), [], batch_id, dry_run,
                  {"pairs": [list(p) for p in todo], "group": g["id"] if g else None})


# --- the Curator queue -------------------------------------------------------------------

def queue_items():
    """One Curator need per look-alike group, for curation_queue.build_queue (built as `system`;
    for_actor hides a group with a photo the asker can't see through `item_slugs`)."""
    out = []
    for g in _groups(actor_ctx.resolve(None))["groups"]:
        keep = next(m for m in g["members"] if m["slug"] == g["keep"])
        n = len(g["members"])
        closest = min(p["distance"] for p in g["pairs"])
        detail = (f"{n} photos with nearly the same picture (perceptual hash {closest} of 64 bits apart"
                  + (f", at most {max(p['distance'] for p in g['pairs'])}" if len(g["pairs"]) > 1 else "") + "). "
                  "Keeping one moves the others to the trash (7-day undo); it takes over their cards, tags and notes.")
        if g["new_members"]:
            detail += (" You already said some of these are not the same; this asks about "
                       + ", ".join(m["title"] for m in g["members"] if m["slug"] in g["new_members"])
                       + ".")
        out.append({
            "type": "need", "key": g["key"], "id": None, "kind": NEED_KIND,
            "label": LABEL, "detail": detail,
            "suggested": {"picks": [g["keep"]], "labels": [keep["title"]], "reason": g["why"]},
            "confidence": None, "group": None, "href": keep["url"], "fix_label": "Open",
            "deferred": False, "dismissible": False,
            "item_slugs": list(g["slugs"]), "lookalike": _public(g),
        })
    return out
