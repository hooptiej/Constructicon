"""Hobby reads and the hobby setters (spec 3.3, 3.9): get_hobby, flags, fields, activity, group code.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

from .. import card_rules, changes, db, policy
from ..card_rules import CardError
from .base import _change_rows, Result


# --- Hobbies (3.3, 3.9) ----------------------------------------------------------

def get_hobby(hobby):
    """Resolves a hobby id or slug to its blog_tags row, or raises CardError('not_found')."""
    row = db.get_hobby(hobby)
    if row is None or not policy.can_view_hobby(row):  # #604 step 3: a hidden private hobby = missing
        raise CardError("not_found", f"No such hobby: {hobby!r}")
    return row


def hobby_flags(hobby):
    """The computed mismatch flags for one hobby (never stored). See db.hobby_flags."""
    return db.hobby_flags(get_hobby(hobby)["id"])


def hobby_fields(hobby_row, with_flags=True):
    """A hobby in the shape every API/page returns: activity (as `status`, the key the
    v1 callers already read), label, group_code, and the computed flags."""
    status = hobby_row.get("hobby_status") or "active"
    out = {
        "id": hobby_row["id"],
        "name": hobby_row["name"],
        "slug": hobby_row["slug"],
        "status": status,
        "status_label": card_rules.HOBBY_ACTIVITY_LABELS.get(status, status),
        "group_code": hobby_row.get("group_code"),
    }
    if "project_count" in hobby_row:
        out["project_count"] = hobby_row["project_count"]
    if with_flags:
        out["flags"] = db.hobby_flags(hobby_row["id"])
    return out


def set_hobby_activity(hobby, value, *, dry_run=False, actor=None, batch_id=None):
    """Sets a hobby's manual Active/Inactive switch (3.3) and logs it. `dormant` and
    `abandoned` are deprecated aliases for `inactive` (the result carries a warning).
    Setting it never writes a flag: flags are computed on read."""
    row = get_hobby(hobby)
    activity, warnings = card_rules.validate_hobby_activity(value)
    rows = _change_rows(row["slug"], {"hobby_status": row.get("hobby_status")}, {"hobby_status": activity})
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db._update_hobby_columns(row["id"], {"hobby_status": activity}, "set_hobby_activity", actor, batch_id=batch_id)
    return Result(True, rows, warnings, batch_id, dry_run)


def set_group_code(hobby, code, *, dry_run=False, actor=None, batch_id=None):
    """Owner-editable hobby code (3.9): 2-4 letters/digits, unique across hobbies."""
    row = get_hobby(hobby)
    code = card_rules.validate_group_code(code)
    if code in {c.upper() for c in db.all_group_codes(exclude_tag_id=row["id"])}:
        raise CardError("group_code_conflict", f"The code {code} is already used by another hobby.")
    rows = _change_rows(row["slug"], {"group_code": row.get("group_code")}, {"group_code": code})
    batch_id = batch_id or changes.new_batch_id()
    if not dry_run and rows:
        db._update_hobby_columns(row["id"], {"group_code": code}, "set_group_code", actor, batch_id=batch_id)
    return Result(True, rows, [], batch_id, dry_run)
