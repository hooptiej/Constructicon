"""Undo and the change log (3.13): undo, list_changes.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import changes, db, policy
from ..card_rules import CardError
from .base import _changes_from_log, Result


# --- Undo and the change log (3.13) -------------------------------------------------

def _undo_rows(target):
    """Resolves an audit row id (int / digit string shorter than a batch id) or a batch id."""
    if isinstance(target, bool) or target in (None, ""):
        raise CardError("bad_undo", "Give an audit id or a batch id.")
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit() and len(target) < 16):
        rows = db.get_change_rows(audit_id=int(target))
    else:
        rows = db.get_change_rows(batch_id=str(target))
    if not rows:
        raise CardError("not_found", f"No change-log entry for {target!r}.")
    return rows


def undo(target, *, force=False, dry_run=False, actor=None):
    """Reverses a change-log entry or a whole batch (spec 3.13). Inverts every row image in
    reverse order inside one transaction and logs its own row, so an undo is itself undoable.
    REFUSES (writing nothing) when: a row was made by the migration (restore from the
    pre-deploy snapshot instead); an entry is already undone; or any affected row no longer
    equals what the log recorded (someone changed it since: undo_conflict, with the field
    and values). force=True skips the staleness check and applies best-effort."""
    rows = _undo_rows(target)
    for r in rows:
        if r.get("actor") == changes.ACTOR_MIGRATION:
            raise CardError("undo_refused", "That change was made by the migration and can't be undone here; restore "
                            "from the pre-deploy snapshot instead.", {"audit_id": r["id"]})
        if r.get("undone_by") is not None:
            raise CardError("undo_refused", f"Entry {r['id']} was already undone (by entry {r['undone_by']}).",
                            {"audit_id": r["id"], "undone_by": r["undone_by"]})
        if not r["mutations"] and r.get("op") not in _items_creation_ops():
            raise CardError("undo_refused", f"Entry {r['id']} ({r['op']}) recorded no row images, so it can't be undone.",
                            {"audit_id": r["id"]})
    # #541 phase B: an item delete/redact moved files into the trash. Plan the file side first so
    # a purged file refuses the whole undo (trash_expired) before anything is written.
    from .. import items as _items  # lazy: items imports this module
    from .. import sharing as _sharing  # lazy: sharing imports this module
    _items.check_undo_allowed(rows, actor)  # #603: an undo that would clear a sensitive flag is admin-only
    _sharing.check_undo_allowed(rows, actor)  # #604 step 3: private cards (sharing changes, hidden rows)
    file_plan = _items.undo_prepare(rows)
    batch_id = changes.new_batch_id()
    applied, slugs, moved = [], [], []
    try:
        with db.transaction(dry_run=dry_run):
            conn = db.get_conn()
            for r in sorted(rows, key=lambda x: x["id"], reverse=True):
                slugs += r["affected_slugs"]
                for m in reversed(r["mutations"]):
                    applied.append(db.invert_image(conn, m, force=force))
            undo_id = db.insert_change_log(conn, "undo", actor, applied, batch_id=batch_id,
                                           affected_slugs=sorted(set(slugs)))
            reversed_ids = [r["id"] for r in rows]
            db.mark_change_rows_undone(reversed_ids, undo_id)
            # Undoing an undo re-opens the entries that undo had reversed.
            for r in rows:
                if r["op"] == "undo":
                    orig = [o["id"] for o in _rows_undone_by(r["id"])]
                    db.mark_change_rows_undone(orig, None)
            if file_plan and not dry_run:
                _items.undo_apply(file_plan, moved)  # last: a failure rolls the rows back too
            # #605: items an op CREATED (an unpacked archive's members) go to the trash, under this
            # undo's batch (so undoing the undo restores them). After the row images were inverted:
            # their card memberships are already gone.
            created = _items.undo_creations(rows, batch_id, actor, moved, dry_run=dry_run)
    except db.UndoConflict as e:
        _items.rollback_moves(moved)
        raise CardError("undo_conflict", str(e), e.details)
    except BaseException:
        _items.rollback_moves(moved)
        raise
    if not dry_run and any(m.get("table") == "install_config" for m in applied):
        from .. import install_config  # #562: its per-process read cache
        install_config.clear_cache()
    if file_plan and not dry_run:
        _items.after_undo(rows, file_plan)
    elif not dry_run and _items.needs_after_undo(rows):  # a retype, or a replaced file (#617)
        _items.after_undo(rows, [])
    flat = _changes_from_log([{"op": "undo", "affected_slugs": sorted(set(slugs)), "mutations": applied}])
    data = {"undone": [r["id"] for r in rows], "undone_ops": [r["op"] for r in rows]}
    warnings = []
    if created["deleted"] or created["gone"]:
        data["created_items_trashed"] = created["deleted"]
        data["created_items_already_gone"] = created["gone"]
        if created["gone"]:
            warnings.append(f"{len(created['gone'])} item(s) this change created were already deleted, so there was "
                            f"nothing to remove for them: {', '.join(created['gone'][:10])}.")
    return Result(True, flat, warnings, batch_id, dry_run, data)


def _items_creation_ops():
    from .. import items as _items  # lazy: items imports this module
    return _items.CREATION_OPS


def _rows_undone_by(undo_id):
    return [r for r in db.list_change_log(limit=100000) if r.get("undone_by") == undo_id]


def list_changes(card=None, batch_id=None, limit=50):
    """Newest-first change-log rows (spec 7.3), as {changes: [{id, op, actor, batch_id, timestamp,
    affected_slugs, undone_by, mutations}], withheld, withheld_note}. `card` is a slug or id.
    #749: a non-admin gets only the rows that touch nothing hidden from them; `withheld` says how
    many of the newest `limit` rows were left out (never silently), `withheld_note` says why in words
    (None when nothing was withheld). Withheld rows are counted, never described."""
    slug = None
    if card not in (None, ""):
        c = db.get_project(card)
        if c is not None:
            policy.require_card_view(c, message=f"No such card: {card!r}")  # #604 step 3
        slug = c["slug"] if c else str(card)
    from .. import sharing as _sharing  # lazy: sharing imports this module
    # #604 steps 3 and 5: a row image carries the whole row, so a non-admin reads only the rows
    # that touch nothing hidden from them (a private card they aren't on, a sensitive item).
    every = db.list_change_log(card_slug=slug, batch_id=batch_id, limit=limit)
    rows = _sharing.visible_change_rows(every)
    withheld = len(every) - len(rows)
    note = None
    if withheld:
        note = (f"{withheld} of the {len(every)} newest change{'s' if len(every) != 1 else ''} "
                f"{'was' if withheld == 1 else 'were'} left out: {'it touches' if withheld == 1 else 'they touch'} "
                f"an item, card or {_sharing._noun(policy.HOBBY)} you can't see (marked sensitive, or private). "
                f"An admin sees the whole log.")
    return {"changes": [{"id": r["id"], "op": r["op"], "actor": r["actor"], "via": changes.via(r),  # #778
                         "batch_id": r["batch_id"],
                         "timestamp": r["timestamp"], "affected_slugs": r["affected_slugs"],
                         "undone_by": r.get("undone_by"), "mutations": r["mutations"]} for r in rows],
            "withheld": withheld, "withheld_note": note}
