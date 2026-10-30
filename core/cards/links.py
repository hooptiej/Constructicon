"""Typed card-to-card links (spec 3.8): link, unlink, retype, list.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import card_rules, changes, db, policy
from ..card_rules import CardError
from .base import get_card, Result


# --- Typed links (3.8) -------------------------------------------------------------
# A link row (a, b, type) reads "a <type> b". Directed types are stored once;
# `related` is symmetric and stored twice ((a,b) and (b,a)), exactly as in v1, so
# old v1 readers kept working (list_related_projects, removed in #760). A pair can't be both typed and related.

def _logical_links(rows):
    """Collapses stored rows into logical links: [{a, b, type}]. The two rows of a
    `related` pair become one entry (a = the alphabetically first slug)."""
    out, seen = [], set()
    for r in rows:
        if r["type"] == "related":
            key = ("related", *sorted((r["slug_a"], r["slug_b"])))
            if key in seen:
                continue
            seen.add(key)
            a, b = sorted((r["slug_a"], r["slug_b"]))
            out.append({"a": a, "b": b, "type": "related", "note": r.get("note") or ""})
        else:
            out.append({"a": r["slug_a"], "b": r["slug_b"], "type": r["type"], "note": r.get("note") or ""})
    return out


def _link_text(link_type, other_slug):
    return f"{link_type} {other_slug}"


def _plan_link(ar, br, link_type, note, existing):
    """Validates and plans "a <link_type> b" against the rows already on the pair
    (`existing`). Returns (deletes, inserts, change_rows, warnings)."""
    verdict = card_rules.validate_link(ar, br, link_type, existing)
    deletes, inserts, warnings = [], [], []
    before = None
    if verdict["drop_related"]:
        deletes = [(r["slug_a"], r["slug_b"], "related") for r in existing if r["type"] == "related"]
        before = _link_text("related", br["slug"])
        warnings.append(f"'{ar['title']}' and '{br['title']}' were related; the typed link replaces that.")
    if link_type == "related":
        inserts = [{"slug_a": ar["slug"], "slug_b": br["slug"], "type": "related", "note": note},
                   {"slug_a": br["slug"], "slug_b": ar["slug"], "type": "related", "note": note}]
    else:
        inserts = [{"slug_a": ar["slug"], "slug_b": br["slug"], "type": link_type, "note": note}]
    rows = [{"card": ar["slug"], "field": "link", "before": before, "after": _link_text(link_type, br["slug"])}]
    return deletes, inserts, rows, warnings


def link(a, b, link_type, note="", *, dry_run=False, actor=None, batch_id=None, _op="link"):
    """Adds the link "a <link_type> b" (types: card_rules.LINK_TYPES). Directed types
    store one row; `related` stores two. Adding a typed link over a `related` pair
    upgrades it (the related rows are removed in the same transaction); adding
    `related` over a typed pair is refused. CardErrors: bad_link, link_conflict, not_found."""
    ar, br = get_card(a), get_card(b)
    existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
    deletes, inserts, rows, warnings = _plan_link(ar, br, link_type, note or "", existing)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run:
        db._write_project_links(deletes, inserts, _op, actor, batch_id=batch_id,
                               affected_slugs=[ar["slug"], br["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def _plan_unlink(ar, br, link_type, existing):
    """(deletes, change_rows, warnings) for removing links between a pair."""
    if link_type is not None:
        card_rules.validate_link_type(link_type)
    deletes, rows, warnings = [], [], []
    for lg in _logical_links(existing):
        if link_type is not None and lg["type"] != link_type:
            continue
        if lg["type"] == "related":
            deletes += [(ar["slug"], br["slug"], "related"), (br["slug"], ar["slug"], "related")]
            rows.append({"card": ar["slug"], "field": "link", "before": _link_text("related", br["slug"]), "after": None})
        elif link_type is None or (lg["a"] == ar["slug"]):
            deletes.append((lg["a"], lg["b"], lg["type"]))
            rows.append({"card": lg["a"], "field": "link",
                         "before": _link_text(lg["type"], lg["b"]), "after": None})
        else:
            warnings.append(f"'{ar['title']}' {link_type.replace('_', ' ')} '{br['title']}' doesn't exist, but the reverse does "
                            f"('{br['title']}' {link_type.replace('_', ' ')} '{ar['title']}'); pass the cards the other way round.")
    return deletes, rows, warnings


def unlink(a, b, link_type=None, *, dry_run=False, actor=None, batch_id=None):
    """Removes link(s) between two cards. With `link_type` only that type (for a
    directed type, "a <type> b" exactly; `related` removes both rows); without it,
    every link between the pair in either direction. No-op (with a warning) if
    nothing matched."""
    ar, br = get_card(a), get_card(b)
    existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
    deletes, rows, warnings = _plan_unlink(ar, br, link_type, existing)
    if not rows and not warnings:
        what = f"{link_type} " if link_type else ""
        warnings.append(f"No {what}link between '{ar['title']}' and '{br['title']}'.")
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and deletes:
        db._write_project_links(deletes, [], "unlink", actor, batch_id=batch_id,
                               affected_slugs=[ar["slug"], br["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def _plan_retype(ar, br, from_type, to_type, existing, note=None):
    """(deletes, inserts, change_rows, warnings) for turning the `from_type` link
    on this pair into "a <to_type> b". The old link is found in either direction;
    the new one always reads a -> b. Raises not_found when there is no such link."""
    card_rules.validate_link_type(from_type)
    card_rules.validate_link_type(to_type)
    if from_type == to_type:
        raise CardError("bad_link", f"The link is already {from_type!r}; pick a different type.")
    old = [r for r in existing if r["type"] == from_type]
    if not old:
        raise CardError("not_found", f"No {from_type!r} link between '{ar['title']}' and '{br['title']}'.")
    remaining = [r for r in existing if r["type"] != from_type]
    deletes = [(r["slug_a"], r["slug_b"], r["type"]) for r in old]
    carried = note if note is not None else (old[0].get("note") or "")
    verdict_deletes, inserts, rows, warnings = _plan_link(ar, br, to_type, carried, remaining)
    # `remaining` excludes the old rows and a pair never holds related + typed rows together,
    # so the validator can't ask to drop anything extra here: verdict_deletes is always empty.
    deletes += verdict_deletes
    rows = [{"card": ar["slug"], "field": "link", "before": _link_text(from_type, br["slug"]),
             "after": _link_text(to_type, br["slug"])}]
    warnings = [w for w in warnings if "were related" not in w]
    return deletes, inserts, rows, warnings


def retype_link(a, b, from_type, to_type, *, note=None, dry_run=False, actor=None, batch_id=None):
    """The single upgrade/downgrade path (3.8): replaces the `from_type` link on the
    pair (found in either direction) with "a <to_type> b", in one transaction. The
    note carries over unless one is given. CardErrors: not_found (no such link),
    bad_link, link_conflict."""
    ar, br = get_card(a), get_card(b)
    existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
    deletes, inserts, rows, warnings = _plan_retype(ar, br, from_type, to_type, existing, note)
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run:
        db._write_project_links(deletes, inserts, "retype_link", actor, batch_id=batch_id,
                               affected_slugs=[ar["slug"], br["slug"]])
    return Result(True, rows, warnings, batch_id, dry_run)


def retype_links(mapping, *, dry_run=True, partial_ok=False, actor=None, batch_id=None):
    """Bulk retype (7.3), for clearing v1's untyped `related` links. `mapping` is a
    list of {a, b, to_type, from_type='related'}. DRY-RUN BY DEFAULT: nothing is
    written unless dry_run=False. Validates every item first; all-or-nothing in one
    transaction and one batch_id (partial_ok=True applies the valid items and
    reports the rest). Returns {ok, dry_run, changes, warnings, batch_id, applied,
    items: [{a, b, to_type, ok, changes, error?}]}. Never raises for item errors."""
    batch_id = batch_id or changes.new_batch_id()
    items, deletes, inserts, all_rows, seen_pairs, slugs = [], [], [], [], set(), []
    for m in mapping or []:
        item = {"a": m.get("a"), "b": m.get("b"), "to_type": m.get("to_type"),
                "from_type": m.get("from_type") or "related"}
        try:
            ar, br = get_card(item["a"]), get_card(item["b"])
            pair = frozenset((ar["slug"], br["slug"]))
            if pair in seen_pairs:
                raise CardError("link_conflict", "This pair appears more than once in the mapping.",
                                {"reason": "duplicate_item"})
            existing = db.list_project_link_rows(pair=(ar["slug"], br["slug"]))
            d, i, rows, _w = _plan_retype(ar, br, item["from_type"], item["to_type"], existing)
            seen_pairs.add(pair)
            item.update(ok=True, changes=rows)
            deletes += d
            inserts += i
            all_rows += rows
            slugs += [ar["slug"], br["slug"]]
        except CardError as e:
            item.update(ok=False, changes=[], error=e.to_dict())
        items.append(item)
    failed = [it for it in items if not it["ok"]]
    ok = not failed or partial_ok
    applied = 0
    if ok and not dry_run and (deletes or inserts):
        db._write_project_links(deletes, inserts, "retype_links", actor, batch_id=batch_id,
                               affected_slugs=sorted(set(slugs)))
        applied = len([it for it in items if it["ok"]])
    warnings = [f"{len(failed)} item(s) can't be retyped; "
                + ("the valid ones are applied." if partial_ok and not dry_run else "nothing was written.")] if failed else []
    return {"ok": bool(ok), "dry_run": dry_run, "changes": all_rows, "warnings": warnings,
            "batch_id": batch_id, "applied": applied, "items": items}


def list_links(card):
    """Every link touching `card`, both directions, with labels (3.8):
    [{slug, title, kind, stage, type, direction: out|in|both, label, note}].
    `out` = this card is the source ("this <type> that"), `in` = it is the target,
    `both` = symmetric (`related`). Ordered by type (card_rules.LINK_TYPES), then title."""
    row = get_card(card)
    rows = db.list_project_link_rows(slug=row["slug"])
    cache, out = {}, []
    acc = policy.card_access()  # #604 step 3: a link to a private card the actor isn't on isn't listed

    def other(slug):
        if slug not in cache:
            cache[slug] = db.get_project(slug)
        return cache[slug]

    for lg in _logical_links(rows):
        if lg["type"] == "related":
            direction, o_slug = "both", lg["b"] if lg["a"] == row["slug"] else lg["a"]
        elif lg["a"] == row["slug"]:
            direction, o_slug = "out", lg["b"]
        else:
            direction, o_slug = "in", lg["a"]
        o = other(o_slug)
        if o is None or not policy.can_view_card(o, access=acc):
            continue
        out.append({"slug": o["slug"], "title": o["title"], "kind": o.get("kind") or "project",
                    "stage": o.get("stage"), "type": lg["type"], "direction": direction,
                    "label": card_rules.link_label(lg["type"], direction), "note": lg["note"]})
    order = {t: i for i, t in enumerate(card_rules.LINK_TYPES)}
    out.sort(key=lambda r: (order.get(r["type"], 99), r["direction"] != "out", r["title"].lower()))
    return out
