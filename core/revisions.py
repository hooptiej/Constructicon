"""Revision chains: "which drawing is current?" (#477).

An item can supersede another item ("rev C replaces rev B"). The link is one row in
`item_revisions(old_slug PK, new_slug UNIQUE)`: an item has at most one successor and at most
one predecessor, so a chain is linear, A -> B -> C. The CURRENT revision is the item with no
successor of its own; everything earlier is superseded but stays findable.

Rules (all enforced here, never in the routes):
  * no self-link, no cycle, both items must exist and be un-redacted;
  * an item that already has a successor can't be given another (take it out of the chain or
    mark its successor instead), and an item that already has a predecessor can't take a second;
  * remove_from_chain closes the gap: A -> B -> C minus B is A -> C.

Every write goes through db.ImageLog inside db.transaction(), so it is one change-log entry (or
batch) that cards.undo() reverses. Violations raise card_rules.CardError, the shared error
convention (HTTP 422/409/404 and the MCP error dict carry the same code):
  not_found, bad_revision (self-link / redacted), revision_cycle, revision_conflict.

Upload-time asking: queue_replace_question() adds an `item_supersedes` pending decision when a
new file's normalized name matches an existing current item. It only ever ASKS; the link exists
only if the owner answers (resolve_decision). Never auto-links. Answers: replace (this file
supersedes the candidate), reverse (the candidate supersedes this file) and same (identical bytes:
this upload is trashed via items.delete); the suggestion follows the evidence (#586).
"""

import logging
import re
import time

from . import besteffort, changes, datefmt, db
from .card_rules import CardError
from .errors import InvalidInput

log = logging.getLogger("constructicon.revisions")

KIND_ITEM_SUPERSEDES = "item_supersedes"
NONE_KEY = "none"
OP_MARK = "mark_superseded"
OP_REMOVE = "remove_from_revisions"
OP_RESOLVE = "resolve_item_supersedes"


# --- Name normalization ----------------------------------------------------------

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_SEP = r"[\s_.\-]"
# Trailing suffixes that mark "another copy/revision of the same thing". Each needs a leading
# separator so a name that merely ends in those letters ("TV", "preview") is left alone.
_SUFFIX_RES = [re.compile(p, re.IGNORECASE) for p in (
    rf"{_SEP}*\(\d{{1,2}}\)$",                                                    # " (1)"
    rf"{_SEP}+(?:19|20)\d{{2}}{_SEP}?(?:0[1-9]|1[0-2]){_SEP}?(?:0[1-9]|[12]\d|3[01])$",  # _2026-10-01 / 20261001
    rf"{_SEP}+(?:rev(?:ision)?|ver(?:sion)?|v){_SEP}*\d+(?:\.\d+)*[a-z]?$",     # _rev2 -v2 v2.1 version 3
    rf"{_SEP}+rev(?:ision)?{_SEP}*[a-z]$",                                        # _revB
    rf"{_SEP}+(?:final|draft|latest|updated|copy|new|old|backup|bak)(?:{_SEP}*\d{{1,2}})?$",
)]
# Names that say nothing about WHICH document it is; matching on them would ask about every upload.
_GENERIC_STEMS = {"img", "image", "photo", "picture", "screenshot", "screen shot", "untitled", "document",
                  "doc", "scan", "video", "file", "test", "download", "capture", "new", "copy"}
MIN_STEM_LEN = 3


def normalize_stem(filename):
    """The comparable core of a filename: extension, version/revision/date/copy suffixes and
    separator noise removed, lowercased. "" means "too generic to match on"."""
    name = _EXT_RE.sub("", (filename or "").strip())
    for _ in range(8):  # "plan_revB_2026-10-01 (1)": peel suffixes until none apply
        before = name
        for rx in _SUFFIX_RES:
            name = rx.sub("", name)
        if name == before:
            break
    stem = re.sub(rf"{_SEP}+", " ", name).strip().lower()
    if len(stem) < MIN_STEM_LEN or stem in _GENERIC_STEMS or stem.replace(" ", "").isdigit():
        return ""
    return stem


# --- Reading chains --------------------------------------------------------------

def _maps(pairs=None):
    pairs = db.revision_pairs() if pairs is None else pairs
    return pairs, {new: old for old, new in pairs.items()}


def chain_slugs(slug, pairs=None):
    """The whole chain containing `slug`, oldest first; [slug] for an item in no chain."""
    fwd, back = _maps(pairs)
    seen = {slug}
    head = slug
    while head in back and back[head] not in seen:  # the seen-guard only matters on corrupt data
        head = back[head]
        seen.add(head)
    out, cur = [head], head
    while cur in fwd and fwd[cur] not in out:
        cur = fwd[cur]
        out.append(cur)
    return out


def current_slug(slug, pairs=None):
    return chain_slugs(slug, pairs)[-1]


def info_map(pairs=None):
    """{slug: {"rev": n, "of": total, "current": slug, "superseded_by": current_slug | None}} for
    every item that is in a chain (items in no chain are absent). One pass over a small table:
    callers decorate a whole grid with it instead of querying per item."""
    fwd, back = _maps(pairs)
    out = {}
    for tail in (n for n in set(fwd.values()) if n not in fwd):  # each chain's current revision
        chain = [tail]
        while chain[-1] in back and back[chain[-1]] not in chain:
            chain.append(back[chain[-1]])
        chain.reverse()  # oldest first
        for i, s in enumerate(chain, start=1):
            out[s] = {"rev": i, "of": len(chain), "current": tail, "superseded_by": None if s == tail else tail}
    return out


def decorate(items, mapping=None):
    """Adds `superseded_by` and `rev` (the card badge fields) to a list of public item dicts."""
    mapping = info_map() if mapping is None else mapping
    for it in items:
        info = mapping.get(it.get("slug"))
        it["superseded_by"] = info["superseded_by"] if info else None
        it["rev"] = info["rev"] if info else None
    return items


def _display(row):
    return row.get("display_name") or row.get("filename") or row.get("content_description") or row["slug"]


def chain_detail(slug):
    """The chain around `slug` for the item page / MCP: [{slug, title, filename, rev, is_current,
    is_this, redacted, thumb}] oldest first. [] when the item is in no chain."""
    slugs = chain_slugs(slug)
    if len(slugs) < 2:
        return []
    from . import policy  # lazy: policy imports nothing from here, but keep revisions' imports light
    out = []
    for i, s in enumerate(slugs, start=1):
        row = db.get_by_slug(s) or {"slug": s}
        if row.get("id") is not None and not policy.can_view(row):
            # #603: a sensitive revision the actor may not see keeps its place, never its name.
            out.append({"slug": s, "title": "(an item you can't see)", "filename": None, "rev": i,
                        "is_current": s == slugs[-1], "is_this": s == slug, "redacted": False, "hidden": True})
            continue
        out.append({"slug": s, "title": _display(row), "filename": row.get("filename"), "rev": i,
                    "is_current": s == slugs[-1], "is_this": s == slug, "redacted": bool(row.get("redacted"))})
    return out


def revision_view(slug):
    """What the item page needs: the chain plus where this item sits in it."""
    chain = chain_detail(slug)
    if not chain:
        return {"in_chain": False, "chain": [], "superseded": False, "current": None, "rev": None, "of": None}
    cur = chain[-1]
    this = next(c for c in chain if c["is_this"])
    return {"in_chain": True, "chain": chain, "superseded": not this["is_current"], "current": cur,
            "rev": this["rev"], "of": len(chain)}


# --- Writing ---------------------------------------------------------------------

def _exists(slug):
    row = db.get_by_slug(slug)
    if row is None:
        raise CardError("not_found", f"No item {slug!r}.")
    return row


def validate_mark(old, new, pairs=None):
    """Raises CardError unless "new supersedes old" is allowed. Returns (old_row, new_row)."""
    old_row, new_row = _exists(old), _exists(new)
    if old == new:
        raise CardError("bad_revision", "An item can't supersede itself.")
    if old_row.get("redacted") or new_row.get("redacted"):
        raise CardError("bad_revision", "A redacted item can't be part of a revision chain.")
    fwd, back = _maps(pairs)
    if old in fwd:
        raise CardError("revision_conflict",
                        f"{_display(old_row)!r} is already superseded by {fwd[old]!r}. Mark that item as superseded "
                        "instead, or remove this one from its chain first.", {"superseded_by": fwd[old]})
    if new in back:
        raise CardError("revision_conflict",
                        f"{_display(new_row)!r} already supersedes {back[new]!r}; an item replaces at most one other.",
                        {"supersedes": back[new]})
    if new in chain_slugs(old, pairs):
        raise CardError("revision_cycle", "That would make the chain loop back on itself.")
    return old_row, new_row


def mark_superseded(old, new, actor=None, batch_id=None, dry_run=False):
    """`new` supersedes `old` (so `old` shows "Superseded, see <current>"). One change-log entry.
    Returns {ok, dry_run, batch_id, old, new, chain: [slugs oldest first]}."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        validate_mark(old, new)
        with db.ImageLog(OP_MARK, actor, batch_id, [old, new]) as il:
            il.insert("item_revisions", {"old_slug": old}, {"new_slug": new, "created_at": time.time()})
        chain = chain_slugs(old)
    return {"ok": True, "dry_run": dry_run, "batch_id": batch_id, "old": old, "new": new, "chain": chain}


def remove_from_chain(slug, actor=None, batch_id=None, dry_run=False):
    """Takes `slug` out of its revision chain and closes the gap: A -> B -> C minus B is A -> C;
    minus the oldest, B -> C stands alone; minus the current one, the previous revision becomes
    current. A no-op (ok, `removed` False) for an item in no chain."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        _exists(slug)
        fwd, back = _maps()
        succ, pred = fwd.get(slug), back.get(slug)
        removed = succ is not None or pred is not None
        if removed:
            with db.ImageLog(OP_REMOVE, actor, batch_id, [s for s in (slug, succ, pred) if s]) as il:
                if succ is not None:
                    il.delete("item_revisions", {"old_slug": slug})  # first: new_slug is UNIQUE
                if pred is not None:
                    if succ is not None:
                        il.update("item_revisions", {"old_slug": pred}, {"new_slug": succ})
                    else:
                        il.delete("item_revisions", {"old_slug": pred})
        # What is left of the chain the item was in (empty when it was one of only two).
        left = chain_slugs(pred or succ) if removed and (pred or succ) else []
    return {"ok": True, "dry_run": dry_run, "batch_id": batch_id, "slug": slug, "removed": removed,
            "chain": left if len(left) > 1 else []}


# --- Upload-time question --------------------------------------------------------

REVERSE_PREFIX = "reverse:"  # "No -- <candidate> replaces THIS file" (#586)
SAME_PREFIX = "same:"         # "It's the same file: keep one" (#586)
_COPY_N_RE = re.compile(r"\((\d{1,2})\)\s*$")


def copy_number(filename):
    """The n of a trailing " (n)" copy marker (macOS / Chrome name a repeat download "X (1).msi"),
    0 for a name without one: no marker is older than (1)."""
    m = _COPY_N_RE.search(_EXT_RE.sub("", (filename or "").strip()))
    return int(m.group(1)) if m else 0


def option_target(key):
    """(action, candidate slug or None) for a question option key: 'replace' (a bare candidate
    slug: this file replaces it), 'reverse', 'same' or 'none'."""
    if key == NONE_KEY:
        return "none", None
    if key.startswith(REVERSE_PREFIX):
        return "reverse", key[len(REVERSE_PREFIX):]
    if key.startswith(SAME_PREFIX):
        return "same", key[len(SAME_PREFIX):]
    return "replace", key


def _day(ts, other=None):
    return datefmt.short_day(ts, other)


def _name(row):
    return row.get("display_name") or row.get("filename") or row["slug"]


def newer_of(this_row, cand_row):
    """Which of two files is the later one, by the best evidence available (#586), newest wins:
    1. the " (n)" copy marker (higher n = the later download; no marker is older than (1));
    2. the files' own modified dates (`source_modified_at`);
    3. upload time, only as a last resort.
    Returns (this_is_newer, reason) or (None, None) when nothing separates them."""
    a, b = copy_number(this_row.get("filename")), copy_number(cand_row.get("filename"))
    if a != b:
        newer, older = (this_row, cand_row) if a > b else (cand_row, this_row)
        n = max(a, b)
        reason = f"the ({n}) copy is the later download"
        ma, mb = this_row.get("source_modified_at"), cand_row.get("source_modified_at")
        if ma and mb and ma != mb:
            reason += f"; modified {_day(max(ma, mb), min(ma, mb))} vs {_day(min(ma, mb), max(ma, mb))}"
        return newer is this_row, reason
    ma, mb = this_row.get("source_modified_at"), cand_row.get("source_modified_at")
    if ma and mb and ma != mb:
        return ma > mb, f"modified {_day(max(ma, mb), min(ma, mb))} vs {_day(min(ma, mb), max(ma, mb))}"
    ta, tb = this_row.get("timestamp"), cand_row.get("timestamp")
    if ta and tb and ta != tb:
        return ta > tb, "uploaded later (no copy marker or modified date to go on)"
    return None, None


def same_file(row_a, row_b):
    """True when the two stored files have the same size and byte-for-byte contents (#586: the
    "(1)" copy of a download is often exactly the same file). Needs both files on disk."""
    import filecmp
    from . import storage
    try:
        if not row_a.get("stored_filename") or not row_b.get("stored_filename"):
            return False
        pa, pb = storage.path_for(row_a["stored_filename"]), storage.path_for(row_b["stored_filename"])
        if not pa.exists() or not pb.exists() or pa.stat().st_size != pb.stat().st_size:
            return False
        return filecmp.cmp(pa, pb, shallow=False)
    except OSError as e:
        besteffort.warn(log, "revisions: could not compare two files byte for byte", e,
                        slug=row_a.get("slug"), other=row_b.get("slug"))
        return False


def candidates_for(slug):
    """Existing CURRENT items of the same type whose normalized filename equals this item's.
    Newest first by the files' own evidence (copy marker, then modified date, then upload time,
    #586), not by upload order alone. Empty when the stem is too generic or the item is already
    in a chain."""
    row = db.get_by_slug(slug)
    if row is None or row.get("redacted") or not row.get("filename"):
        return []
    stem = normalize_stem(row["filename"])
    if not stem:
        return []
    pairs = db.revision_pairs()
    fwd, back = _maps(pairs)
    if slug in back or slug in fwd:
        return []
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT slug, filename, display_name, media_type, timestamp, source_modified_at, stored_filename "
            "FROM capture_events "
            "WHERE filename IS NOT NULL AND slug != ? AND redacted = 0 AND is_brand_asset = 0 AND media_type IS ? "
            "ORDER BY timestamp DESC", (slug, row.get("media_type"))).fetchall()
    finally:
        conn.close()
    out = [dict(r) for r in rows if r["slug"] not in fwd and normalize_stem(r["filename"]) == stem]
    out.sort(key=lambda c: (copy_number(c["filename"]), c.get("source_modified_at") or 0, c.get("timestamp") or 0),
             reverse=True)
    return out


def queue_replace_question(slug):
    """Queues "Does this replace ...?" for a freshly uploaded file, if anything matches.
    Returns the decision id, or None (no match, or this file was already asked about).

    #586: the file just uploaded is NOT assumed to be the newer one. Each candidate gets three
    answers: "Yes, it replaces <c>", "No -- <c> replaces this one" (the reverse, link recorded as
    <c> superseding this file) and, when the two files are byte-for-byte identical, "It's the same
    file: keep one" (this upload goes to the trash, 7-day undo). The suggestion follows the
    evidence (identical contents, then the " (n)" copy marker, then modified dates, then upload
    time) and `suggested_reason` says which one decided it."""
    row = db.get_by_slug(slug)
    cands = candidates_for(slug)
    if not cands:
        return None
    title = _display(row)
    _fwd, back = _maps()
    options, identical, directions = [], [], {}
    for c in cands:
        name = c["display_name"] or c["filename"]
        newer, why = newer_of(row, c)
        directions[c["slug"]] = (newer, why)
        options.append({"key": c["slug"], "label": f"Yes, it replaces {name}"})
        if c["slug"] not in back:  # a candidate that already replaces something can't also replace this
            options.append({"key": REVERSE_PREFIX + c["slug"], "label": f"No — {name} replaces this one"})
        if same_file(row, c):
            identical.append(c)
            options.append({"key": SAME_PREFIX + c["slug"],
                            "label": f"It's the same file: keep one (this upload goes to the trash; {name} stays)"})
    options.append({"key": NONE_KEY, "label": "No, it's a separate file"})
    suggested = reason = confidence = None
    if identical:
        c = identical[0]
        suggested = SAME_PREFIX + c["slug"]
        reason = (f"Same size and identical contents as {c['display_name'] or c['filename']}: "
                  "it is the same file, so keeping one is enough.")
        confidence = "high"
    elif len(cands) == 1:
        c = cands[0]
        newer, why = directions[c["slug"]]
        if newer is True:
            suggested, reason, confidence = c["slug"], f"This is the later file: {why}.", "medium"
        elif newer is False and c["slug"] not in back:
            suggested, reason, confidence = REVERSE_PREFIX + c["slug"], f"The other file is the later one: {why}.", "medium"
        else:
            reason = None
    payload = {
        "schema": 2, "question": f"Does “{title}” replace an earlier file?", "options": options,
        "candidate_slugs": [c["slug"] for c in cands], "stem": normalize_stem(row["filename"]),
        "suggested": suggested, "suggested_reason": reason, "confidence": confidence,
    }
    return db.queue_decision_once(KIND_ITEM_SUPERSEDES, slug, payload)


def live_candidates(decision):
    """The decision's candidate slugs that can still be superseded (exist, un-redacted, no
    successor yet) -- empty when the question has gone stale (e.g. the new file was already
    linked by hand)."""
    slug = decision["post_slug"]
    fwd, back = _maps()
    if slug in back or slug in fwd or db.get_by_slug(slug) is None:
        return []
    out = []
    for c in decision["payload"].get("candidate_slugs", []):
        r = db.get_by_slug(c)
        if r is not None and not r.get("redacted") and c not in fwd and c != slug:
            out.append(c)
    return out


def resolve_decision(decision, choice, actor=None, dry_run=False):
    """Answers an item_supersedes question. `choice` is a candidate slug (create the link) or
    "none". Link and resolution share one batch, so one undo reverses both. A rule violation
    (CardError) leaves the decision open."""
    candidates = {o["key"] for o in decision["payload"].get("options", [])}
    if choice not in candidates:
        raise InvalidInput(f"'{choice}' is not one of this question's options: {sorted(candidates)}",
                           code="invalid_choice")
    batch_id = changes.new_batch_id()
    action, cand = option_target(choice)
    this = decision["post_slug"]
    with db.transaction(dry_run=dry_run):
        applied = []
        if action == "replace":
            mark_superseded(cand, this, actor=actor, batch_id=batch_id)
            applied.append(cand)
        elif action == "reverse":  # #586: the candidate is the newer file, so it supersedes this one
            mark_superseded(this, cand, actor=actor, batch_id=batch_id)
            applied.append(cand)
        elif action == "same":
            # #586: keep one. The upload goes to the trash through the item service (7-day undo);
            # its pending question (this one) is deleted with it, imaged in the same batch.
            from . import items
            if not _exists(cand) or not same_file(_exists(this), _exists(cand)):
                raise CardError("bad_revision", "Those two files are no longer identical, so one can't be dropped as a copy.")
            items.delete([this], actor=actor, batch_id=batch_id, dry_run=dry_run)
            return {"ok": True, "applied": [], "trashed": this, "kept": cand, "batch_id": batch_id,
                    "remaining": _count_open()}
        db._resolve_pending_decision(decision["id"], {"choice": choice, "superseded": applied},
                                    log={"op": OP_RESOLVE, "actor": actor, "batch_id": batch_id})
    return {"ok": True, "applied": applied, "batch_id": batch_id, "remaining": _count_open()}


def _count_open():
    from . import decisions  # lazy: decisions imports this module
    return decisions.count_open()
