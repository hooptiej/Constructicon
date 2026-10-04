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
only if the owner answers with a candidate (resolve_decision). Never auto-links.
"""

import re
import time

from . import changes, db
from .card_rules import CardError

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
    out = []
    for i, s in enumerate(slugs, start=1):
        row = db.get_by_slug(s) or {"slug": s}
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


def mark_superseded(old, new, actor=changes.ACTOR_UI, batch_id=None, dry_run=False):
    """`new` supersedes `old` (so `old` shows "Superseded, see <current>"). One change-log entry.
    Returns {ok, dry_run, batch_id, old, new, chain: [slugs oldest first]}."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        validate_mark(old, new)
        with db.ImageLog(OP_MARK, actor, batch_id, [old, new]) as il:
            il.insert("item_revisions", {"old_slug": old}, {"new_slug": new, "created_at": time.time()})
        chain = chain_slugs(old)
    return {"ok": True, "dry_run": dry_run, "batch_id": batch_id, "old": old, "new": new, "chain": chain}


def remove_from_chain(slug, actor=changes.ACTOR_UI, batch_id=None, dry_run=False):
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

def candidates_for(slug):
    """Existing CURRENT items of the same type whose normalized filename equals this item's.
    Newest upload first. Empty when the stem is too generic or the item is already in a chain."""
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
            "SELECT slug, filename, display_name, media_type, timestamp FROM capture_events "
            "WHERE filename IS NOT NULL AND slug != ? AND redacted = 0 AND is_brand_asset = 0 AND media_type IS ? "
            "ORDER BY timestamp DESC", (slug, row.get("media_type"))).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if r["slug"] not in fwd and normalize_stem(r["filename"]) == stem]


def queue_replace_question(slug):
    """Queues "Does this replace ...?" for a freshly uploaded file, if anything matches.
    Returns the decision id, or None (no match, or this file was already asked about)."""
    row = db.get_by_slug(slug)
    cands = candidates_for(slug)
    if not cands:
        return None
    title = _display(row)
    options = [{"key": c["slug"], "label": f"Yes, it replaces {c['display_name'] or c['filename']}"} for c in cands]
    options.append({"key": NONE_KEY, "label": "No, it's a separate file"})
    payload = {
        "schema": 1, "question": f"Does “{title}” replace an earlier file?", "options": options,
        "candidate_slugs": [c["slug"] for c in cands], "stem": normalize_stem(row["filename"]),
        "suggested": cands[0]["slug"] if len(cands) == 1 else None,
        "suggested_reason": "Same name apart from the revision or date marker." if len(cands) == 1 else None,
        "confidence": "medium" if len(cands) == 1 else None,
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


def resolve_decision(decision, choice, actor=changes.ACTOR_UI, dry_run=False):
    """Answers an item_supersedes question. `choice` is a candidate slug (create the link) or
    "none". Link and resolution share one batch, so one undo reverses both. A rule violation
    (CardError) leaves the decision open."""
    candidates = {o["key"] for o in decision["payload"].get("options", [])}
    if choice not in candidates:
        raise ValueError(f"'{choice}' is not one of this question's options: {sorted(candidates)}")
    batch_id = changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        applied = []
        if choice != NONE_KEY:
            mark_superseded(choice, decision["post_slug"], actor=actor, batch_id=batch_id)
            applied.append(choice)
        db.resolve_pending_decision(decision["id"], {"choice": choice, "superseded": applied},
                                    log={"op": OP_RESOLVE, "actor": actor, "batch_id": batch_id})
    return {"ok": True, "applied": applied, "batch_id": batch_id, "remaining": db.count_pending_decisions()}
