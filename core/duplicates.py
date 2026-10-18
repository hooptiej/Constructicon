"""Duplicate files: find byte-identical copies and fold them into one survivor (#615).

The owner's rule (2026-10-17, binding, see the issue's comments):

  1. A duplicate is a BYTE-IDENTICAL stored file (same size, same sha256). Nothing else: perceptual
     look-alikes (a PDF's v1.0 / v1.1 sharing a cover page, the .eps and .png of a logo) are never
     duplicates here.
  2. The survivor: a copy whose card date (timeline.resolve_item_date) is on or after Constructicon's
     first upload is "probably the incorrect one", so copies with a real date from BEFORE the first
     upload come first; among those the EARLIEST date wins (compared by Mountain-Time day). Ties go to
     the copy on more cards, then the one with more metadata (description, caption, tags), then the
     oldest upload. A copy with no real date (#725) or a post-first-upload date only wins when no copy
     has an earlier real date.
  3. The survivor INHERITS what the dropped copies had and it lacks: card memberships, tags in both
     stores (post_tags and the free-text chips), description / caption / provenance / agent notes
     when its own are blank, the highlight / brand-asset / sensitive flags when a dropped copy has
     them (sensitive is inherited so trashing a flagged copy can never expose the picture), related
     links, blog-entry placements, and any card cover, card write-up, hobby cover or blog cover that
     pointed at a dropped copy.
  4. The dropped copies go to the trash through items.delete (7-day undo), all in ONE batch with the
     inheritance rows, so one constructicon_undo(batch_id) puts everything back. Nothing is ever
     deleted permanently here.
  5. No date edits on anything. Revision chains are left to items.delete's gap-closing (A -> B -> C
     minus B is A -> C); the plan names any copy that sits in a chain.

  plan(actor=None)                      read-only: the groups, survivor, what would be trashed and
                                        inherited, and a summary. Groups holding an item the actor may
                                        not see are left out (counted, never named).
  apply(dry_run=True, expect_trash=None, actor=None)
                                        admin only. Re-plans inside one transaction and applies it.
                                        `expect_trash`: the slugs a dry run showed; when the fresh plan
                                        would trash anything else (or less), it refuses (plan_changed)
                                        and writes nothing. Idempotent: a second run finds no groups.

Files that can't be read are never silently skipped: each is listed under `unreadable` with the
reason, and the group it might have belonged to is planned without it.
"""

import hashlib
import json
import time

from . import cards, changes, datefmt, db, item_title, items, membership, paths, policy, roles, timeline
from . import actor as actor_ctx
from . import tags as tags_svc
from .cards import Result
from .errors import AppError, Conflict

OP_MERGE = "duplicates_merge"
_CHUNK = 1024 * 1024

# Text fields the survivor takes from a dropped copy when its own is blank (first non-blank copy
# in survivor-rank order wins). Dates are deliberately NOT here (rule 5).
FILL_FIELDS = ("description", "content_description", "provenance", "agent_notes", "brand_role")
# Flags the survivor takes when any dropped copy has them set.
OR_FLAGS = ("highlight", "is_brand_asset", "sensitive")


# --- reading -----------------------------------------------------------------------------

def first_upload_cutoff():
    """Constructicon's first upload (UTC unix seconds): MIN(capture_events.timestamp). A copy dated
    on or after it is the less trusted one. None on an empty library."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT MIN(timestamp) AS t FROM capture_events").fetchone()
        return row["t"] if row else None
    finally:
        conn.close()


def _candidates():
    """[(slug, stored_filename)] for every un-redacted item with an uploaded file."""
    conn = db.get_conn()
    try:
        return [(r["slug"], r["stored_filename"]) for r in conn.execute(
            "SELECT slug, stored_filename FROM capture_events "
            "WHERE redacted = 0 AND stored_filename IS NOT NULL AND stored_filename != '' ORDER BY id")]
    finally:
        conn.close()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_groups():
    """Byte-identical groups: ({(size, sha256): [slug, ...]} with 2+ slugs, unreadable). Files are
    sized first and only same-size files are hashed. `unreadable` lists every file that could not be
    sized or read, with the reason (never skipped silently)."""
    by_size, unreadable = {}, []
    for slug, sf in _candidates():
        p = paths.storage_dir() / sf
        try:
            size = p.stat().st_size
        except OSError as e:
            unreadable.append({"slug": slug, "stored_filename": sf, "reason": f"can't read the stored file: {e}"})
            continue
        if size == 0:
            continue  # an empty file is not a photo of anything; nothing to fold
        by_size.setdefault(size, []).append((slug, sf, p))
    groups = {}
    for size, entries in by_size.items():
        if len(entries) < 2:
            continue
        for slug, sf, p in entries:
            try:
                digest = _sha256(p)
            except OSError as e:
                unreadable.append({"slug": slug, "stored_filename": sf, "reason": f"can't read the stored file: {e}"})
                continue
            groups.setdefault((size, digest), []).append(slug)
    return {k: v for k, v in groups.items() if len(v) >= 2}, unreadable


def _facts(row, cutoff):
    """What the survivor rule looks at for one copy."""
    slug = row["slug"]
    date = timeline.resolve_item_date(row)
    early = date is not None and (cutoff is None or date < cutoff)
    cards_ = db.list_projects_for_post(slug)
    tag_rows = db.list_tags_for_post(slug)
    free = [t for t in (row.get("tags") or []) if str(t).strip()]
    tag_names = {t["name"] for t in tag_rows} | set(free)
    metadata = (int(bool((row.get("description") or "").strip()))
                + int(bool((row.get("content_description") or "").strip()))
                + len(tag_names))
    return {
        "row": row, "slug": slug, "date": date, "day": datefmt.iso_day(date) if date is not None else None,
        "early": early, "cards": cards_, "tag_rows": tag_rows, "free_tags": free, "metadata": metadata,
    }


def _rank_key(f):
    # bucket 0 = a real date before the first upload; 1 = no real date, or one on/after it.
    return (0 if f["early"] else 1, f["day"] or "9999-99-99", -len(f["cards"]), -f["metadata"],
            f["row"].get("timestamp") or 0, f["slug"])


def _why(survivor, runner_up):
    """One plain sentence: what decided the survivor over the next-best copy."""
    s, r = survivor, runner_up
    if s["early"] and not r["early"]:
        if r["date"] is None:
            return f"the only copy with a real date before Constructicon ({s['day']}); the other has no real date"
        return f"its date ({s['day']}) is from before Constructicon; the other's ({r['day']}) is not"
    if s["day"] != r["day"]:
        return f"earliest date ({s['day'] or 'none'} vs {r['day'] or 'none'})"
    if len(s["cards"]) != len(r["cards"]):
        return f"same date; on more cards ({len(s['cards'])} vs {len(r['cards'])})"
    if s["metadata"] != r["metadata"]:
        return f"same date and cards; more description/caption/tags ({s['metadata']} vs {r['metadata']})"
    return "same date, cards and metadata; uploaded first"


def _face(f):
    row = f["row"]
    return {"slug": f["slug"], "title": item_title.title_of(row), "filename": row.get("filename"),
            "media_type": row.get("media_type"), "date": f["day"],
            "date_before_constructicon": f["early"], "uploaded": datefmt.iso_day(row.get("timestamp")),
            "cards": [c["title"] for c in f["cards"]]}


def _plan_group(key, rows, cutoff):
    """One group's plan, or (None, skip reason) when it can't be folded safely."""
    stored = [r["stored_filename"] for r in rows]
    if len(set(stored)) != len(stored):
        return None, "two items share one stored file, so trashing one would take the other's file too"
    facts = sorted((_facts(r, cutoff) for r in rows), key=_rank_key)
    survivor, dropped = facts[0], facts[1:]
    surv_cards = {c["id"] for c in survivor["cards"]}
    surv_tag_ids = {t["id"] for t in survivor["tag_rows"]}
    surv_free = list(survivor["free_tags"])
    new_cards, new_tag_ids, new_tag_names, new_free = {}, [], [], []
    fills = {}
    flags = {}
    for d in dropped:
        for c in d["cards"]:
            if c["id"] not in surv_cards and c["id"] not in new_cards:
                new_cards[c["id"]] = c["title"]
        for t in d["tag_rows"]:
            if t["id"] not in surv_tag_ids and t["id"] not in new_tag_ids:
                new_tag_ids.append(t["id"])
                new_tag_names.append(t["name"])
        for name in d["free_tags"]:
            if name not in surv_free and name not in new_free:
                new_free.append(name)
        for col in FILL_FIELDS:
            if col not in fills and not (survivor["row"].get(col) or "").strip() and (d["row"].get(col) or "").strip():
                fills[col] = d["row"][col]
        for col in OR_FLAGS:
            if d["row"].get(col) and not survivor["row"].get(col):
                flags[col] = 1
    chains = [d["slug"] for d in dropped if _in_chain(d["slug"])]
    size, digest = key
    plan = {
        "sha256": digest, "size_bytes": size,
        "keep": _face(survivor),
        "why": _why(survivor, dropped[0]),
        "trash": [_face(d) for d in dropped],
        "dates_differ": len({f["day"] for f in facts}) > 1,
        "has_post_constructicon_copy": any(f["date"] is not None and not f["early"] for f in facts),
        "on_several_cards": len({c["id"] for f in facts for c in f["cards"]}) > 1,
        "inherit": {
            "cards": [{"id": cid, "title": t} for cid, t in new_cards.items()],
            "tags": new_tag_names, "free_text_tags": new_free,
            "fields": sorted(fills), "flags": sorted(flags),
        },
        "in_revision_chain": chains,
        # what apply() writes; stripped from the public plan
        "_ops": {"survivor": survivor["slug"], "dropped": [d["slug"] for d in dropped],
                 "cards": list(new_cards), "tag_ids": new_tag_ids, "free": new_free,
                 "fields": {**fills, **flags}},
    }
    return plan, None


def _in_chain(slug):
    refs = db.item_references(slug)
    return refs["successor"] is not None or refs["predecessor"] is not None


def _build(actor=None):
    who = actor_ctx.resolve(actor)
    cutoff = first_upload_cutoff()
    found, unreadable = find_groups()
    groups, skipped, hidden = [], [], 0
    for key in sorted(found, key=lambda k: (-len(found[k]), k[1])):
        rows = [r for r in (db.get_by_slug(s) for s in found[key]) if r is not None]
        if len(rows) < 2:
            continue
        if not all(policy.can_view(r, who) for r in rows):
            hidden += 1  # counted, never named: the actor may not see one of its copies
            continue
        plan, why = _plan_group(key, rows, cutoff)
        if why:
            skipped.append({"sha256": key[1], "slugs": found[key], "reason": why})
        elif plan:
            groups.append(plan)
    by_type = {}
    for g in groups:
        for t in g["trash"]:
            by_type[t["media_type"]] = by_type.get(t["media_type"], 0) + 1
    summary = {
        "groups": len(groups),
        "copies_to_trash": sum(len(g["trash"]) for g in groups),
        "trash_by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0] or ""))),
        "groups_same_date": sum(1 for g in groups if not g["dates_differ"]),
        "groups_dates_differ": sum(1 for g in groups if g["dates_differ"]),
        "groups_with_post_constructicon_copy": sum(1 for g in groups if g["has_post_constructicon_copy"]),
        "groups_on_several_cards": sum(1 for g in groups if g["on_several_cards"]),
        "groups_touching_revision_chains": sum(1 for g in groups if g["in_revision_chain"]),
        "skipped_groups": len(skipped),
        "hidden_groups": hidden,
        "unreadable_files": len(unreadable),
        "first_upload": datefmt.iso_day(cutoff) if cutoff else None,
    }
    return {"summary": summary, "groups": groups, "skipped": skipped, "unreadable": unreadable}


def _public(built):
    return {**built, "groups": [{k: v for k, v in g.items() if k != "_ops"} for g in built["groups"]]}


def plan(actor=None):
    """Read-only. {summary, groups: [{sha256, size_bytes, keep, why, trash, inherit, ...}], skipped,
    unreadable}. A group holding an item `actor` may not see (policy.can_view) is left out and only
    counted (summary.hidden_groups)."""
    return _public(_build(actor))


# --- writing -----------------------------------------------------------------------------

def _inherit(log, ops, who):
    """The survivor takes over what the dropped copies had (rule 3), on the open ImageLog."""
    keep, dropped = ops["survivor"], ops["dropped"]
    group = set(dropped) | {keep}
    for card_id in ops["cards"]:
        membership.write_items(log, card_id, [keep])
    for tid in ops["tag_ids"]:
        tags_svc.link(log, keep, tid)
    for name in ops["free"]:
        tags_svc.merge_free_text_name(log, keep, name)
    if ops["fields"]:
        log.update("capture_events", {"slug": keep}, dict(ops["fields"]))
        if ops["fields"].get("sensitive"):
            log.update("capture_events", {"slug": keep}, {"sensitive_by": who, "sensitive_at": time.time()})
    now = time.time()
    for d in dropped:
        refs = db.item_references(d)
        for a, b in refs["relations"]:
            other = b if a == d else a
            if other in group:
                continue
            for x, y in ((keep, other), (other, keep)):
                if log.get("capture_event_relations", {"slug_a": x, "slug_b": y}) is None:
                    log.insert("capture_event_relations", {"slug_a": x, "slug_b": y}, {"created_at": now})
                    log.slugs.append(other)
        for eid in refs["blog_entry_items"]:
            if log.get("blog_entry_items", {"entry_id": eid, "post_slug": keep}) is None:
                src = log.get("blog_entry_items", {"entry_id": eid, "post_slug": d})
                log.insert("blog_entry_items", {"entry_id": eid, "post_slug": keep},
                           {"sort_order": src["sort_order"], "note": src["note"]})
        conn = log.conn
        for r in conn.execute("SELECT id FROM projects WHERE cover_slug = ?", (d,)).fetchall():
            log.update("projects", {"id": r["id"]}, {"cover_slug": keep})
        rewritten = conn.execute("SELECT id FROM projects WHERE writeup_slug = ?", (d,)).fetchall()
        for r in rewritten:
            log.update("projects", {"id": r["id"]}, {"writeup_slug": keep})
        if rewritten:
            cards.refresh_writeup_lead(log, keep)
        for r in conn.execute("SELECT hobby_tag_id FROM hobby_settings WHERE cover_slug = ?", (d,)).fetchall():
            log.update("hobby_settings", {"hobby_tag_id": r["hobby_tag_id"]}, {"cover_slug": keep})
        for r in conn.execute("SELECT id FROM blog_entries WHERE cover_slug = ?", (d,)).fetchall():
            log.update("blog_entries", {"id": r["id"]}, {"cover_slug": keep})


def apply(*, dry_run=True, expect_trash=None, actor=None, batch_id=None):
    """Folds every duplicate group into its survivor (rules 2-4) as ONE batch: per group one imaged
    `duplicates_merge` change-log row (the inheritance), then one items.delete of every dropped copy
    (files to the trash, 7-day undo). DRY-RUN BY DEFAULT: dry_run=True runs the same path and rolls it
    back (no file moves). Admin only (403 forbidden otherwise).

    `expect_trash`: the slugs a dry run said it would trash. When given and the fresh plan would trash
    a different set, it refuses with 409 `plan_changed` (saying what differs) and writes nothing, so a
    go-ahead covers exactly what was shown. Returns a Result; data: {summary, groups (the plan),
    trashed, kept, expires_at, trash_days}. Nothing to do = ok, nothing written."""
    who = actor_ctx.resolve(actor)
    if not roles.at_least(roles.role_of(who), roles.ADMIN):
        raise AppError("forbidden", "Folding duplicates trashes items across the whole library, so it needs an "
                       "admin. Nothing was changed.", status=403)
    batch_id = batch_id or changes.new_batch_id()
    moved_muts = []
    with db.transaction(dry_run=dry_run):
        built = _build(who)
        groups = built["groups"]
        drop = [s for g in groups for s in g["_ops"]["dropped"]]
        if expect_trash is not None:
            want = {s for s in expect_trash if s}
            missing, extra = sorted(want - set(drop)), sorted(set(drop) - want)
            if missing or extra:
                raise Conflict(
                    "The duplicates have changed since that dry run, so nothing was changed. "
                    + (f"No longer to be trashed: {', '.join(missing)}. " if missing else "")
                    + (f"Newly to be trashed: {', '.join(extra)}. " if extra else "")
                    + "Run the dry run again and check the new list.",
                    code="plan_changed", details={"no_longer": missing, "new": extra})
        if not groups:
            return Result(True, [], [], batch_id, dry_run, {**_public(built), "trashed": [], "kept": []})
        keep = [g["_ops"]["survivor"] for g in groups]
        policy.viewable_rows(keep + drop, actor=who)  # every slug written must be visible (#709)
        for g in groups:
            ops = g["_ops"]
            with db.ImageLog(OP_MERGE, who, batch_id, [ops["survivor"]] + ops["dropped"]) as log:
                _inherit(log, ops, who)
                moved_muts += list(log.muts)
        res = items.delete(drop, dry_run=dry_run, actor=who, batch_id=batch_id)
    out = Result(True, items._flat(OP_MERGE, moved_muts) + res.changes, [], batch_id, dry_run,
                 {**_public(built), "trashed": drop, "kept": keep,
                  "expires_at": res.data.get("expires_at"), "trash_days": items.TRASH_DAYS})
    return out


def summary_text(built):
    """A one-screen plain-text summary of a plan (for the owner)."""
    s = built["summary"]
    lines = [f"{s['groups']} groups of byte-identical files; {s['copies_to_trash']} extra copies would go to the "
             f"trash (7-day undo).",
             "By type: " + ", ".join(f"{k} {v}" for k, v in s["trash_by_type"].items()),
             f"{s['groups_same_date']} groups have the same date on every copy, {s['groups_dates_differ']} differ, "
             f"{s['groups_with_post_constructicon_copy']} have a copy dated after Constructicon's first upload "
             f"({s['first_upload']}); {s['groups_on_several_cards']} span more than one card."]
    if s["skipped_groups"] or s["unreadable_files"] or s["hidden_groups"]:
        lines.append(f"Left alone: {s['skipped_groups']} groups (see skipped), {s['unreadable_files']} unreadable "
                     f"files, {s['hidden_groups']} groups you may not see.")
    return "\n".join(lines)


def to_json(built):
    return json.dumps(built, indent=1, default=str)
