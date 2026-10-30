"""Piece 6, the reorganizing toolkit: a card's home (3.10), hobby membership, moving and copying
files, split_card, merge_cards and delete_card (#497).

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

import time

from .. import card_rules, changes, db, policy
from ..card_rules import CardError
from ..errors import AppError
from .base import get_card, Result
from .ops import set_highlight, set_provenance, set_whereabouts
from .families import add_to_family, _slug_of
from .links import link
from .hobbies import get_hobby
from .decisions import open_card_decisions


# =====================================================================================
# Piece 6: reorganizing toolkit (spec sections 3.10, 3.13, 6). Composite operations run
# inside db.transaction(): one atomic unit, every write imaged in the change log under
# one batch_id, and `dry_run` is the same code path rolled back (so a preview can't
# disagree with the real thing).
# =====================================================================================

def _slim(c):
    return {"id": c["id"], "slug": c["slug"], "title": c["title"], "kind": c.get("kind") or "project",
            "kind_label": card_rules.kind_label(c.get("kind") or "project"), "stage": c.get("stage"),
            "activity": c.get("activity")}


# --- Home (3.10) -----------------------------------------------------------------

def _home_text(kind, ref):
    if kind == "card":
        return f"card:{_slug_of(ref) or ref}"
    if kind == "hobby":
        h = db.get_hobby(ref)
        return f"hobby:{h['slug'] if h else ref}"
    return None


def _parse_home_target(target):
    """-> (home_kind, row). `target`: a card id (int), a dict {type|kind, ref|id|slug},
    or a string: 'card:<ref>', 'hobby:<ref>', or a bare card ref (a bare ref that is no
    card is tried as a hobby)."""
    if isinstance(target, dict):
        kind = target.get("type") or target.get("kind")
        ref = target.get("ref", target.get("id", target.get("slug")))
        card_rules.validate_home_kind(kind)
    elif isinstance(target, str) and ":" in target:
        kind, _, ref = target.partition(":")
        card_rules.validate_home_kind(kind)
    else:
        kind, ref = None, target
    if kind in (None, "card"):
        row = db.get_project(ref)
        if row is not None:
            return "card", row
        if kind == "card":
            raise CardError("not_found", f"No such card: {ref!r}")
    hob = db.get_hobby(ref)
    if hob is None:
        raise CardError("not_found", f"No such card or hobby: {ref!r}")
    return "hobby", hob


def set_home(card, target=None, *, dry_run=False, actor=None, batch_id=None):
    """Overrides a card's automatic home (3.10), or with target=None clears the override
    so the home goes back to automatic (parent, then first family, then first hobby).
    `target` is a card or a hobby (see _parse_home_target). A card can't be its own home."""
    row = get_card(card)
    if target in (None, ""):
        fields = {"home_kind": None, "home_ref": None}
    else:
        kind, target_row = _parse_home_target(target)
        if kind == "card" and target_row["id"] == row["id"]:
            raise CardError("bad_home", "A card can't be its own home.")
        fields = {"home_kind": kind, "home_ref": target_row["id"]}
    before_text = _home_text(row.get("home_kind"), row.get("home_ref")) if row.get("home_kind") else None
    after_text = _home_text(fields["home_kind"], fields["home_ref"]) if fields["home_kind"] else None
    rows = []
    if (row.get("home_kind"), row.get("home_ref")) != (fields["home_kind"], fields["home_ref"]):
        rows = [{"card": row["slug"], "field": "home", "before": before_text, "after": after_text}]
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db._update_card_columns(row["id"], fields, "set_home", actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)


def _home_entry(kind, row, source, dangling=False):
    return {"type": kind, "id": row["id"], "slug": row["slug"],
            "title": row.get("title") or row.get("name"), "source": source, "dangling_override": dangling}


def resolve_home(card):
    """The card's home (3.10): {type: card|hobby|None, id, slug, title, source:
    override|parent|family|hobby|none, dangling_override}. A valid manual override wins;
    else the parent; else the earliest family membership; else the earliest hobby;
    else none (the home page). A dangling override (its target was deleted) falls
    through to the automatic default and is flagged, never raised."""
    row = get_card(card)
    dangling = False
    kind, ref = row.get("home_kind"), row.get("home_ref")
    if kind and ref is not None:
        target = db.get_project(ref) if kind == "card" else (db.get_hobby(ref) if kind == "hobby" else None)
        # #604 step 3: a home override naming a private card / hobby the actor can't see is passed over
        # (not flagged dangling either, which would hint that something is there).
        hidden = target is not None and not (policy.can_view_card(target) if kind == "card" else policy.can_view_hobby(target))
        if target is not None and not hidden and not (kind == "card" and target["id"] == row["id"]):
            return _home_entry(kind, target, "override")
        dangling = not hidden
    if row.get("parent_id"):
        parent = db.get_project(row["parent_id"])
        if parent is not None:
            return _home_entry("card", parent, "parent", dangling)
    fams = db.list_families_for_member(row["id"])
    if fams:
        return _home_entry("card", fams[0], "family", dangling)
    for hobby_row in db.list_hobby_rows(row["id"]):
        hob = db.get_hobby(hobby_row["hobby_tag_id"])
        if hob is not None:
            return _home_entry("hobby", hob, "hobby", dangling)
    return {"type": None, "id": None, "slug": None, "title": None, "source": "none", "dangling_override": dangling}


def home_chain(card, limit=12):
    """The breadcrumb: the card's home, then that home's own home, and so on, nearest
    first, ending at a hobby or the top. Cycle-safe."""
    row = get_card(card)
    chain, seen = [], {row["id"]}
    current = row
    while len(chain) < limit:
        h = resolve_home(current["id"])
        if h["type"] is None:
            break
        chain.append(h)
        if h["type"] != "card" or h["id"] in seen:
            break
        seen.add(h["id"])
        current = db.get_project(h["id"])
        if current is None:
            break
    return chain


# --- Hobby membership under the new names (3.9) ----------------------------------

def add_to_hobby(card, hobby, *, dry_run=False, actor=None, batch_id=None):
    """Adds a card to a hobby (many allowed; adding twice is a no-op)."""
    row, hob = get_card(card), get_hobby(hobby)
    exists = any(r["hobby_tag_id"] == hob["id"] for r in db.list_hobby_rows(row["id"]))
    rows = [] if exists else [{"card": row["slug"], "field": "in_hobby", "before": None, "after": hob["slug"]}]
    warnings = [f"'{row['title']}' is already in '{hob['name']}'."] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and not exists:
        db._insert_card_hobby(row["id"], hob["id"], "add_to_hobby", actor, batch_id, [row["slug"], hob["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def remove_from_hobby(card, hobby, *, dry_run=False, actor=None, batch_id=None):
    """Takes a card out of a hobby (no-op if it wasn't in it)."""
    row, hob = get_card(card), get_hobby(hobby)
    exists = any(r["hobby_tag_id"] == hob["id"] for r in db.list_hobby_rows(row["id"]))
    rows = [{"card": row["slug"], "field": "in_hobby", "before": hob["slug"], "after": None}] if exists else []
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and exists:
        db._delete_card_hobby(row["id"], hob["id"], "remove_from_hobby", actor, batch_id, [row["slug"], hob["slug"]])
    return Result(True, rows, [], batch_id, dry_run)


# --- Moving and copying files ----------------------------------------------------

def _membership():
    """core/membership.py owns project_items writes (#541 phase C). Lazy: it imports Result from here."""
    from .. import membership
    return membership


def _as_slug_list(slugs):
    if isinstance(slugs, str):
        slugs = [slugs]
    out = []
    for s in slugs or []:
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)
    if not out:
        raise CardError("bad_files", "Name at least one file (by slug).")
    return out


def _transfer_files(slugs, from_card, to_card, move, dry_run, actor, batch_id):
    src, dst = get_card(from_card), get_card(to_card)
    if src["id"] == dst["id"]:
        raise CardError("bad_files", "The source and destination are the same card.")
    slugs = _as_slug_list(slugs)
    held = {r["post_slug"] for r in db.list_project_item_rows(src["id"])}
    missing = [s for s in slugs if s not in held]
    if missing:
        raise CardError("bad_files", f"Not in '{src['title']}': {', '.join(missing[:8])}.", {"missing": missing})
    # #749: every file moved / copied must be visible to the actor (#709's 403 saying why; nothing written).
    policy.viewable_rows(slugs, actor=actor)
    for s in slugs:
        if s == src.get("writeup_slug") or s == dst.get("writeup_slug"):
            raise CardError("bad_files", "A card's write-up can't be moved or copied; it belongs to its card.",
                            {"slug": s})
    already = {r["post_slug"] for r in db.list_project_item_rows(dst["id"])}
    rows, warnings = [], []
    for s in slugs:
        if s not in already:
            rows.append({"card": dst["slug"], "field": "file", "before": None, "after": s})
        else:
            warnings.append(f"{s} is already in '{dst['title']}'.")
        if move:
            rows.append({"card": src["slug"], "field": "file", "before": s, "after": None})
    if move and src.get("cover_slug") in slugs:
        warnings.append(f"'{src['title']}' still has {src['cover_slug']} as its cover; pick another if you want.")
    batch_id = batch_id or changes.new_batch_id()
    op = "move_files" if move else "copy_files"
    # #590: the files gain the destination's linked tag (both tag stores); a move also sheds the
    # source's own linked tag. Planned read-only first so a dry run shows it.
    plan, dst_tag, src_tag = _membership().plan_transfer_tags(slugs, src, dst, drop_source=move)
    rows += _tag_rows(plan, dst_tag, src_tag, src, dst)
    if not dry_run:
        with db.transaction():
            _membership().write(dst["id"], slugs, [], op, actor, batch_id, [src["slug"], dst["slug"]])
            if move:
                _membership().write(src["id"], [], slugs, op, actor, batch_id, [src["slug"], dst["slug"]])
            _membership().apply_transfer_tags(plan, dst_tag, src_tag, op, actor, batch_id,
                                              [src["slug"], dst["slug"]] + [d["slug"] for d in plan])
    return Result(True, rows, warnings, batch_id, dry_run,
                  {"tags_added": sum(1 for d in plan if d["link_add"]),
                   "tags_removed": sum(1 for d in plan if d["link_drop"])})


def _tag_rows(plan, dst_tag, src_tag, src, dst):
    """Change-summary rows for a tag plan: one per card, naming the tag and how many files."""
    rows = []
    gained = sum(1 for d in plan if d["link_add"])
    lost = sum(1 for d in plan if d["link_drop"])
    if gained and dst_tag:
        rows.append({"card": dst["slug"], "field": "tag", "before": None, "after": f"{dst_tag['name']} (+{gained} file(s))"})
    if lost and src_tag:
        rows.append({"card": src["slug"], "field": "tag", "before": f"{src_tag['name']} (-{lost} file(s))", "after": None})
    return rows


def move_files(slugs, from_card, to_card, *, dry_run=False, actor=None, batch_id=None):
    """Moves files (by slug) from one card to another: they leave `from_card` and join
    `to_card`. A card's own write-up can't be moved."""
    return _transfer_files(slugs, from_card, to_card, True, dry_run, actor, batch_id)


def copy_files(slugs, from_card, to_card, *, dry_run=False, actor=None, batch_id=None):
    """Adds files from one card to another WITHOUT removing them (files are many-to-many)."""
    return _transfer_files(slugs, from_card, to_card, False, dry_run, actor, batch_id)


# --- split_card (6) ----------------------------------------------------------------

def _resolve_hobby_refs(spec, src):
    """'inherit' -> the source's hobbies (attach order); a list -> those hobbies; else none."""
    if spec == "inherit":
        return [h for h in (db.get_hobby(r["hobby_tag_id"]) for r in db.list_hobby_rows(src["id"])) if h]
    if spec in (None, "", []):
        return []
    return [get_hobby(h) for h in spec]


def _resolve_family_refs(spec, src):
    if spec == "inherit":
        return [f for f in db.list_families_for_member(src["id"])]
    if spec in (None, "", []):
        return []
    return [get_card(f) for f in spec]


def _copy_privacy(from_card, to_card, op, actor, batch_id, slugs):
    """#604 step 3: tick Private on `to_card` and give it `from_card`'s member list (imaged)."""
    def _copy(log):
        log.update("projects", {"id": to_card}, {"private": 1})
        for m in db.share_members(policy.CARD, from_card):
            key = {"target_kind": policy.CARD, "target_id": to_card, "user_id": m["user_id"]}
            if log.get("share_members", key) is None:
                log.insert("share_members", key, {"role": m["role"], "added_at": time.time()})
    db.write_images(op, actor, batch_id, slugs, _copy)


def split_card(source, parts, *, keep_in_source=False, dry_run=False, actor=None, batch_id=None):
    """Carves files (and a description) out of `source` into new cards (spec 6).

    Each part: {title, kind='project', relation='sibling'|'child', stage, stop_reason,
    provenance, provenance_credit, whereabouts, whereabouts_note, description,
    move_description, file_slugs, link_to_source: {type, note?}|None,
    hobbies: 'inherit'|[hobby refs], families: 'inherit'|[family refs], highlight}.
    A `child` is created nested under the source (nest rules apply). A `sibling` has no
    parent and by default gets the source's hobbies and family memberships copied; a
    child defaults to none. Files named in `file_slugs` are MOVED out of the source
    (several parts may name the same file; it lands in each), unless keep_in_source=True
    (then copied). Every part is a full card, blank write-up included. One transaction and
    one batch_id: any rule violation writes nothing; `undo` removes the parts and restores
    the source's files."""
    src = get_card(source)
    if not isinstance(parts, list) or not parts:
        raise CardError("bad_split", "Give at least one part to split out.")
    relations = [card_rules.validate_split_part(p, i) for i, p in enumerate(parts)]
    held = {r["post_slug"] for r in db.list_project_item_rows(src["id"])}
    moved_all = []
    for i, p in enumerate(parts):
        files = _as_slug_list(p["file_slugs"]) if p.get("file_slugs") else []
        missing = [s for s in files if s not in held]
        if missing:
            raise CardError("bad_files", f"Part {i + 1} names file(s) not in '{src['title']}': {', '.join(missing[:8])}.",
                            {"missing": missing})
        if src.get("writeup_slug") in files:
            raise CardError("bad_files", "A card's write-up can't be split out; it belongs to its card.")
        moved_all += [s for s in files if s not in moved_all]
    # #749: every file a part names must be visible to the actor (#709's 403 saying why; nothing written).
    if moved_all:
        policy.viewable_rows(moved_all, actor=actor)
    batch_id = batch_id or changes.new_batch_id()
    rows, warnings, created = [], [], []
    with db.transaction(dry_run=dry_run):
        for i, p in enumerate(parts):
            relation = relations[i]
            kind = card_rules.validate_kind(p.get("kind") or card_rules.DEFAULT_KIND)
            stage = p.get("stage") or card_rules.DEFAULT_STAGE
            description = p.get("description") or ""
            if p.get("move_description"):
                description = src.get("description") or ""
            new = db._create_project(p["title"].strip(), description=description, kind=kind, stage=stage,
                                    stop_reason=p.get("stop_reason"),
                                    parent_id=src["id"] if relation == "child" else None,
                                    actor=actor, batch_id=batch_id)
            if p.get("provenance") or p.get("provenance_credit"):
                set_provenance(new["id"], p.get("provenance"), p.get("provenance_credit") or ...,
                               actor=actor, batch_id=batch_id)
            if p.get("whereabouts") or p.get("whereabouts_note"):
                set_whereabouts(new["id"], p.get("whereabouts"), p.get("whereabouts_note") or ...,
                                actor=actor, batch_id=batch_id)
            if p.get("highlight"):
                set_highlight(new["id"], True, actor=actor, batch_id=batch_id)
            if src.get("private"):  # #604 step 3: a part split out of a private card stays private
                _copy_privacy(src["id"], new["id"], "split_card", actor, batch_id, [new["slug"], src["slug"]])
            files = _as_slug_list(p["file_slugs"]) if p.get("file_slugs") else []
            if files:
                _membership().write(new["id"], files, [], "split_card", actor, batch_id, [new["slug"], src["slug"]])
                if src.get("cover_slug") in files:
                    db._write_card_row(new["id"], {"cover_slug": src["cover_slug"]}, "split_card", actor, batch_id)
            hobby_spec = p.get("hobbies", "inherit" if relation == "sibling" else None)
            for hob in _resolve_hobby_refs(hobby_spec, src):
                add_to_hobby(new["id"], hob["id"], actor=actor, batch_id=batch_id)
            family_spec = p.get("families", "inherit" if relation == "sibling" else None)
            for fam in _resolve_family_refs(family_spec, src):
                add_to_family(fam["id"], new["id"], actor=actor, batch_id=batch_id)
            if p.get("link_to_source"):
                spec = p["link_to_source"]
                link(new["id"], src["id"], spec.get("type"), spec.get("note") or "", actor=actor, batch_id=batch_id)
            fresh = db.get_project(new["id"])
            created.append({**_slim(fresh), "relation": relation, "files": files})
            rows.append({"card": fresh["slug"], "field": "created", "before": None,
                         "after": f"{fresh['title']} ({card_rules.kind_label(kind)}, {relation})"})
            for s in files:
                rows.append({"card": fresh["slug"], "field": "file", "before": None, "after": s})
        if any(p.get("move_description") for p in parts):
            db._write_card_row(src["id"], {"description": ""}, "split_card", actor, batch_id)
            rows.append({"card": src["slug"], "field": "description", "before": src.get("description"), "after": ""})
        if moved_all and not keep_in_source:
            _membership().write(src["id"], [], moved_all, "split_card", actor, batch_id, [src["slug"]])
            for s in moved_all:
                rows.append({"card": src["slug"], "field": "file", "before": s, "after": None})
            if src.get("cover_slug") in moved_all:
                warnings.append(f"'{src['title']}' still has {src['cover_slug']} as its cover; pick another if you want.")
    return Result(True, rows, warnings, batch_id, dry_run, {"created": created})


# --- merge_cards (6) ----------------------------------------------------------------

def _merge_one(keep, a, actor, batch_id, rows, warnings):
    """Folds card `a` into `keep` (runs inside the caller's transaction)."""
    keep_group = (keep.get("kind") or "project") in card_rules.GROUP_KINDS
    a_group = (a.get("kind") or "project") in card_rules.GROUP_KINDS
    if keep_group != a_group:
        raise CardError("bad_merge", f"Can't merge '{a['title']}' ({card_rules.kind_label(a.get('kind') or 'project')}) "
                        f"into '{keep['title']}' ({card_rules.kind_label(keep.get('kind') or 'project')}): "
                        "families and collections only merge with their own kind.")
    slugs = [keep["slug"], a["slug"]]
    op = "merge_cards"
    descendants = db._descendant_project_ids(a["id"])
    if keep["id"] in descendants and keep.get("parent_id") != a["id"]:
        raise CardError("nest_cycle", f"'{keep['title']}' is nested inside '{a['title']}' (not directly), so "
                        "merging would make a cycle. Unnest one of them first.")
    if keep.get("parent_id") == a["id"]:
        db._update_card_columns(keep["id"], {"parent_id": a.get("parent_id")}, op, actor, batch_id=batch_id)
        rows.append({"card": keep["slug"], "field": "parent", "before": a["slug"], "after": _slug_of(a.get("parent_id"))})
    # Children of the absorbed card now belong to keep.
    for child in db.list_child_projects(a["id"]):
        if child["id"] == keep["id"]:
            continue
        db.check_nest(child, keep["id"], replace=True)
        db._update_card_columns(child["id"], {"parent_id": keep["id"]}, op, actor, batch_id=batch_id)
        rows.append({"card": child["slug"], "field": "parent", "before": a["slug"], "after": keep["slug"]})
    # Files: unioned into keep. A blank auto write-up is deleted with the card; a real one stays as a file.
    item_rows = db.list_project_item_rows(a["id"])
    file_slugs = [r["post_slug"] for r in item_rows]
    blank_writeup = a.get("writeup_slug") if a.get("writeup_slug") and db.blank_document_body(a["writeup_slug"]) else None
    moving = [s for s in file_slugs if s != blank_writeup]
    added, _ = _membership().write(keep["id"], moving, [], op, actor, batch_id, slugs)
    for s in added:
        rows.append({"card": keep["slug"], "field": "file", "before": None, "after": s})
    if a.get("writeup_slug") and not blank_writeup and a["writeup_slug"] in moving:
        warnings.append(f"'{a['title']}' had a write-up with text; it's now an ordinary file in '{keep['title']}'.")
    _membership().write(a["id"], [], file_slugs, op, actor, batch_id, slugs)
    # #590: a merge is a move into the kept card, so the files take keep's linked tag and shed the
    # absorbed card's own (the same rule as move_files; the absorbed card is going away).
    plan, dst_tag, src_tag = _membership().plan_transfer_tags(moving, a, keep, drop_source=True)
    _membership().apply_transfer_tags(plan, dst_tag, src_tag, op, actor, batch_id, slugs + [d["slug"] for d in plan])
    rows += _tag_rows(plan, dst_tag, src_tag, a, keep)
    if blank_writeup:
        def _drop_doc(log):
            for r in db.list_post_tag_rows(blank_writeup):
                log.delete("post_tags", {"post_slug": blank_writeup, "tag_id": r["tag_id"]})
            log.delete("capture_events", {"slug": blank_writeup})
        db.write_images(op, actor, batch_id, slugs, _drop_doc)
    # Cover: keep's wins; adopt the absorbed one only if keep has none.
    if not keep.get("cover_slug") and not keep.get("cover_project_id"):
        adopt = {}
        if a.get("cover_slug"):
            adopt["cover_slug"] = a["cover_slug"]
        elif a.get("cover_project_id") and a["cover_project_id"] != keep["id"]:
            adopt["cover_project_id"] = a["cover_project_id"]
        if adopt:
            db._write_card_row(keep["id"], adopt, op, actor, batch_id)
            rows.append({"card": keep["slug"], "field": "cover", "before": None, "after": list(adopt.values())[0]})
    # Hobbies: unioned.
    for r in db.list_hobby_rows(a["id"]):
        if db._insert_card_hobby(keep["id"], r["hobby_tag_id"], op, actor, batch_id, slugs):
            hob = db.get_hobby(r["hobby_tag_id"])
            rows.append({"card": keep["slug"], "field": "in_hobby", "before": None, "after": hob["slug"] if hob else r["hobby_tag_id"]})
        db._delete_card_hobby(a["id"], r["hobby_tag_id"], op, actor, batch_id, slugs)
    # Family memberships (as a member), then members (as a family): unioned.
    for r in db.list_family_rows(a["id"], as_member=True):
        fam = db.get_project(r["family_id"])
        if fam is not None and fam["id"] != keep["id"]:
            res = add_to_family(fam["id"], keep["id"], actor=actor, batch_id=batch_id, _op=op)
            rows += res.changes
        db._remove_family_member(r["family_id"], a["id"], op, actor, batch_id=batch_id, affected_slugs=slugs)
    for r in db.list_family_rows(a["id"], as_member=False):
        mem = db.get_project(r["member_id"])
        if mem is not None and mem["id"] != keep["id"]:
            res = add_to_family(keep["id"], mem["id"], actor=actor, batch_id=batch_id, _op=op)
            rows += res.changes
        db._remove_family_member(a["id"], r["member_id"], op, actor, batch_id=batch_id, affected_slugs=slugs)
    # Links: re-pointed; self-links and duplicates dropped; a typed link beats `related` on the same pair.
    a_links = db.list_project_link_rows(slug=a["slug"])
    if a_links:
        existing = {(r["slug_a"], r["slug_b"], r["type"]) for r in db.list_project_link_rows(slug=keep["slug"])}
        deletes = [(r["slug_a"], r["slug_b"], r["type"]) for r in a_links]
        inserts, planned = [], set()
        for r in a_links:
            na = keep["slug"] if r["slug_a"] == a["slug"] else r["slug_a"]
            nb = keep["slug"] if r["slug_b"] == a["slug"] else r["slug_b"]
            key = (na, nb, r["type"])
            if na == nb or key in existing or key in planned:
                continue
            planned.add(key)
            inserts.append({"slug_a": na, "slug_b": nb, "type": r["type"], "note": r.get("note") or ""})
            rows.append({"card": keep["slug"], "field": "link", "before": None, "after": f"{r['type']} {nb if na == keep['slug'] else na}"})
        all_rows = existing | planned
        typed_pairs = {frozenset((x, y)) for x, y, t in all_rows if t != "related"}
        for (x, y, t) in sorted(all_rows):
            if t == "related" and frozenset((x, y)) in typed_pairs:
                if (x, y, t) in planned:
                    inserts = [i for i in inserts if (i["slug_a"], i["slug_b"], i["type"]) != (x, y, t)]
                else:
                    deletes.append((x, y, t))
                warnings.append(f"'{x}' and '{y}' ended up both related and typed after the merge; the typed link was kept.")
        db._write_project_links(deletes, inserts, op, actor, batch_id=batch_id, affected_slugs=slugs)
    # Blog entries that featured the absorbed card now feature keep.
    def _repoint_entries(log):
        have = {r["entry_id"] for r in db.list_entry_project_rows(keep["id"])}
        for r in db.list_entry_project_rows(a["id"]):
            log.delete("blog_entry_projects", {"entry_id": r["entry_id"], "project_id": a["id"]})
            if r["entry_id"] not in have:
                log.insert("blog_entry_projects", {"entry_id": r["entry_id"], "project_id": keep["id"]},
                           {"sort_order": r["sort_order"], "note": r["note"]})
    db.write_images(op, actor, batch_id, slugs, _repoint_entries)
    # Open questions about the absorbed card are moot.
    for d in open_card_decisions(a["slug"]):
        db._resolve_pending_decision(d["id"], {"stale": f"merged into {keep['slug']}"},
                                    log={"op": op, "actor": actor, "batch_id": batch_id})
        rows.append({"card": a["slug"], "field": "decision", "before": d["kind"], "after": "resolved (stale)"})
    # Other cards that borrowed the absorbed card's cover or named it as their home.
    for other in db.list_projects_referencing(a["id"]):
        if other["id"] == a["id"]:
            continue
        if other.get("cover_project_id") == a["id"]:
            db._write_card_row(other["id"], {"cover_project_id": keep["id"] if other["id"] != keep["id"] else None},
                              op, actor, batch_id)
        if other.get("home_kind") == "card" and other.get("home_ref") == a["id"]:
            new_home = (None, None) if other["id"] == keep["id"] else ("card", keep["id"])
            db._update_card_columns(other["id"], {"home_kind": new_home[0], "home_ref": new_home[1]}, op, actor,
                                   batch_id=batch_id)
    db._write_card_row(keep["id"], {}, op, actor, batch_id)
    if db.share_members(policy.CARD, a["id"]):  # #604 step 3: the kept card's member list governs
        warnings.append(f"'{a['title']}' had its own member list; the files now follow '{keep['title']}''s.")
        _drop_share_rows(a["id"], op, actor, batch_id, slugs)
    db.write_images(op, actor, batch_id, slugs, lambda log: log.delete("projects", {"id": a["id"]}))
    rows.append({"card": keep["slug"], "field": "merged", "before": a["slug"], "after": None})


def _require_card_files_visible(card_rows, verb, actor=None):
    """(#749) A card-level reshape (merge) moves EVERY file on the cards it names, including files
    the actor can't see (marked sensitive, a restricted type, also on a private card). Refuse the whole request (403
    `forbidden`, nothing written) when any is hidden from them, saying how many but never which:
    they named the card, not those files (a count only, as card counts already are)."""
    hidden = {}
    with policy.access_scope(actor):
        for c in card_rows:
            n = sum(1 for r in db.list_project_items(c["id"]) if not policy.can_view(r, actor))
            if n:
                hidden[c["slug"]] = n
    if hidden:
        total = sum(hidden.values())
        names = ", ".join(f"'{c['title']}'" for c in card_rows if c["slug"] in hidden)
        raise AppError("forbidden", f"Nothing was changed: {names} {'holds' if len(hidden) == 1 else 'hold'} "
                       f"{total} file{'s' if total != 1 else ''} you can't see (marked sensitive, a restricted "
                       f"file type, or also on a private card), and a {verb} would move {'it' if total == 1 else 'them'} too. Ask an admin "
                       f"to do it.", status=403, details={"reason": "hidden_files", "hidden": hidden})


def merge_cards(keep, absorb, *, dry_run=False, actor=None, batch_id=None):
    """Folds one or more cards into `keep` (spec 6): files unioned (deduped), hobbies and
    family memberships unioned, children re-parented, links re-pointed (self-links and
    duplicates dropped), blog-entry attachments re-pointed. keep's cover and write-up win; an
    absorbed card's blank write-up goes with it, a real one stays as an ordinary file.
    Open questions on absorbed cards are resolved as stale. The absorbed card rows are
    deleted; their row images make `undo` restore them (same id, same files). Refuses
    (writing nothing) when it would create a nest cycle or merge a group kind with a
    non-group kind."""
    keep_row = get_card(keep)
    absorb = [absorb] if not isinstance(absorb, (list, tuple)) else list(absorb)
    ids, absorbed = {keep_row["id"]}, []
    for ref in absorb:
        a = get_card(ref)
        if a["id"] == keep_row["id"]:
            raise CardError("bad_merge", "A card can't be merged into itself.")
        if a["id"] in ids:
            continue
        ids.add(a["id"])
        absorbed.append(a)
    if not absorbed:
        raise CardError("bad_merge", "Name at least one card to absorb.")
    _require_card_files_visible(absorbed, "merge", actor)  # #749
    # #604 step 3: merging moves the files, so a private card can't be folded into one that isn't.
    if not policy.share_state(policy.CARD, keep_row)["effective"]:
        for a in absorbed:
            if policy.share_state(policy.CARD, a)["effective"]:
                raise CardError("privacy_conflict",
                                f"Nothing was changed: '{a['title']}' is private and '{keep_row['title']}' isn't, so "
                                f"merging would show its files to everyone signed in. Tick Private on "
                                f"'{keep_row['title']}' first (and add the same members), or untick it on '{a['title']}'.",
                                {"private": a["slug"], "open": keep_row["slug"]})
    batch_id = batch_id or changes.new_batch_id()
    rows, warnings = [], []
    with db.transaction(dry_run=dry_run):
        for a in absorbed:
            _merge_one(db.get_project(keep_row["id"]), db.get_project(a["id"]), actor, batch_id, rows, warnings)
    return Result(True, rows, warnings, batch_id, dry_run, {"keep": keep_row["slug"], "absorbed": [a["slug"] for a in absorbed]})


# --- delete_card (#497) -------------------------------------------------------------

def _clear_card_dependents(card, op, actor, batch_id, rows, warnings, *, dissolving=False):
    """Removes everything that hangs off a card so no ghost rows survive it (runs inside the
    caller's transaction, every row imaged). `dissolving` = the card row itself is handled by
    the caller (convert-to-hobby): children, files and the card row are left alone, except
    that a blank auto write-up is still dropped. Returns {children, files, writeup}."""
    slug, cid = card["slug"], card["id"]
    slugs = [slug]
    out = {"children": 0, "files": 0, "writeup": None}
    # Open questions about this card are moot: resolve them with a note (never leave them dangling).
    for d in open_card_decisions(slug):
        db._resolve_pending_decision(d["id"], {"stale": f"card '{slug}' was deleted"},
                                    log={"op": op, "actor": actor, "batch_id": batch_id})
        rows.append({"card": slug, "field": "decision", "before": d["kind"], "after": "resolved (stale)"})
    # Children are orphaned (parent_id cleared), never deleted.
    if not dissolving:
        for child in db.list_child_projects(cid):
            db._update_card_columns(child["id"], {"parent_id": None}, op, actor, batch_id=batch_id)
            rows.append({"card": child["slug"], "field": "parent", "before": slug, "after": None})
            out["children"] += 1
        if out["children"]:
            warnings.append(f"{out['children']} nested card(s) now stand on their own.")
    # The auto write-up goes only while it's still blank; one with text stays as an ordinary unfiled file.
    ws = card.get("writeup_slug")
    blank_ws = ws if ws and db.blank_document_body(ws) else None
    file_slugs = [r["post_slug"] for r in db.list_project_item_rows(cid)]
    if blank_ws:
        for d in db.list_all_pending_decisions(post_slug=blank_ws):
            if d["resolved_at"] is None:
                db._resolve_pending_decision(d["id"], {"stale": f"write-up of deleted card '{slug}'"},
                                            log={"op": op, "actor": actor, "batch_id": batch_id})

        def _drop_doc(log):
            if blank_ws in file_slugs:
                _membership().write_items(log, cid, (), [blank_ws])
            for r in db.list_post_tag_rows(blank_ws):
                log.delete("post_tags", {"post_slug": blank_ws, "tag_id": r["tag_id"]})
            log.delete("capture_events", {"slug": blank_ws})
        db.write_images(op, actor, batch_id, slugs, _drop_doc)
        out["writeup"] = "deleted"
        rows.append({"card": slug, "field": "writeup", "before": blank_ws, "after": None})
    elif ws and ws in file_slugs and db.get_by_slug(ws) is not None:
        out["writeup"] = "kept"
        warnings.append(f"The write-up '{ws}' has text, so it was kept as an ordinary unfiled document.")
    if not dissolving:
        keep_files = [s for s in file_slugs if s != blank_ws]
        _membership().write(cid, [], keep_files, op, actor, batch_id, slugs)
        out["files"] = len(keep_files)
    for r in db.list_hobby_rows(cid):
        db._delete_card_hobby(cid, r["hobby_tag_id"], op, actor, batch_id, slugs)
    for r in db.list_family_rows(cid, as_member=True):
        db._remove_family_member(r["family_id"], cid, op, actor, batch_id=batch_id, affected_slugs=slugs)
    for r in db.list_family_rows(cid, as_member=False):
        db._remove_family_member(cid, r["member_id"], op, actor, batch_id=batch_id, affected_slugs=slugs)
    links = db.list_project_link_rows(slug=slug)
    if links:
        db._write_project_links([(r["slug_a"], r["slug_b"], r["type"]) for r in links], [], op, actor,
                               batch_id=batch_id, affected_slugs=slugs)
        for r in links:
            rows.append({"card": slug, "field": "link", "before": f"{r['slug_a']} {r['type']} {r['slug_b']}", "after": None})
    def _drop_entry_rows(log):
        for r in db.list_entry_project_rows(cid):
            log.delete("blog_entry_projects", {"entry_id": r["entry_id"], "project_id": cid})
    db.write_images(op, actor, batch_id, slugs, _drop_entry_rows)
    if not dissolving:  # #604 step 3: its member list goes with it (a conversion carries it over instead)
        _drop_share_rows(cid, op, actor, batch_id, slugs)
    # Other cards that borrowed this card's cover or named it as their home.
    for other in db.list_projects_referencing(cid):
        if other["id"] == cid:
            continue
        if other.get("cover_project_id") == cid:
            db._write_card_row(other["id"], {"cover_project_id": None}, op, actor, batch_id)
        if other.get("home_kind") == "card" and other.get("home_ref") == cid:
            db._update_card_columns(other["id"], {"home_kind": None, "home_ref": None}, op, actor, batch_id=batch_id)
    return out


def _drop_share_rows(card_id, op, actor, batch_id, slugs):
    """#604 step 3: a card that goes away takes its member list with it (imaged, so undo restores it)."""
    def _drop(log):
        for m in db.share_members(policy.CARD, card_id):
            log.delete("share_members", {"target_kind": policy.CARD, "target_id": card_id, "user_id": m["user_id"]})
    db.write_images(op, actor, batch_id, slugs, _drop)


def _privacy_note(card, files, children):
    """#604 step 3: say so when taking files / cards out from under a private card can open them up."""
    if not (files or children) or not policy.share_state(policy.CARD, card)["effective"]:
        return None
    return (f"'{card['title']}' was private: its {files} file(s) and {children} nested card(s) are no longer under it, "
            f"so everyone signed in can see them unless another private card holds them. Undo puts them back.")


def delete_card(card, *, dry_run=False, actor=None, batch_id=None):
    """Deletes a card cleanly (#497), in one transaction with row images, so `undo(batch_id)`
    brings everything back. Removes its blank auto write-up (a write-up with text is kept as an
    ordinary unfiled document, with a warning), its files membership, hobby and family rows
    (as member and as family), typed links in either direction, blog-entry attachments, and
    resolves its open decisions. Nested children are orphaned, not deleted; other cards that
    used it as a cover or home lose that pointer. Files themselves are never deleted.
    Returns Result with data {deleted, children_orphaned, items_detached, writeup}."""
    row = get_card(card)
    batch_id = batch_id or changes.new_batch_id()
    rows, warnings = [], []
    with db.transaction(dry_run=dry_run):
        fresh = db.get_project(row["id"])
        note = _privacy_note(fresh, len(db.list_project_item_rows(row["id"])), len(db.list_child_projects(row["id"])))
        out = _clear_card_dependents(fresh, "delete_card", actor, batch_id, rows, warnings)
        if note:
            warnings.append(note)
        db.write_images("delete_card", actor, batch_id, [row["slug"]], lambda log: log.delete("projects", {"id": row["id"]}))
        rows.append({"card": row["slug"], "field": "deleted", "before": row["title"], "after": None})
    return Result(True, rows, warnings, batch_id, dry_run,
                  {"deleted": row["slug"], "children_orphaned": out["children"], "items_detached": out["files"],
                   "writeup": out["writeup"]})


# (#541 phase D) convert_project_to_hobby moved to core/hobbies.py (`convert_from_card`), where it
# is fully row-imaged and undoable; it still runs _clear_card_dependents(dissolving=True) first.
