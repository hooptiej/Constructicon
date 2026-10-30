"""Revision chains (#477) and the look-alike image candidates (#759).

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""


from .conn import get_conn


# --- Revision chains (#477) ---
# item_revisions(old_slug PK, new_slug): "new supersedes old", at most one successor per item,
# so a chain A -> B -> C is two rows and the current revision is the item with no row of its
# own. Validation and the change log live in core/revisions.py; this is the raw access the
# listings share. Deliberately NOT capture_event_relations: that table is symmetric and its
# add_relation merges tags/projects, neither of which fits a directed "replaces" link.

def _unlink_revision(conn, slug):
    """Removes `slug` from its chain on the caller's connection: its predecessors are
    re-pointed at its successor (or become the chain's end when there is none)."""
    row = conn.execute("SELECT new_slug FROM item_revisions WHERE old_slug = ?", (slug,)).fetchone()
    if row is not None:
        conn.execute("DELETE FROM item_revisions WHERE old_slug = ?", (slug,))  # first: new_slug is UNIQUE
        conn.execute("UPDATE item_revisions SET new_slug = ? WHERE new_slug = ?", (row["new_slug"], slug))
    else:
        conn.execute("DELETE FROM item_revisions WHERE new_slug = ?", (slug,))


LOOKALIKE_IMAGE_CLAUSE = "(media_type = 'image' OR media_type IS NULL OR media_type = '')"


def list_lookalike_images():
    """(#759) Every un-redacted image item with an uploaded file, as row dicts without the embedding
    BLOB, oldest first: what the look-alike pass (core/lookalikes.py) considers. Rows with no usable
    perceptual hash are included (the pass counts them)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM capture_events WHERE COALESCE(redacted, 0) = 0 AND stored_filename IS NOT NULL "
            f"AND stored_filename != '' AND {LOOKALIKE_IMAGE_CLAUSE} ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            out.append(d)
        return out
    finally:
        conn.close()


def revision_pairs():
    """{old_slug: new_slug} for every direct supersedes link. One small query."""
    conn = get_conn()
    try:
        return {r["old_slug"]: r["new_slug"] for r in conn.execute("SELECT old_slug, new_slug FROM item_revisions")}
    finally:
        conn.close()


def superseded_slugs():
    """Slugs that have a successor (everything in a chain except its current revision)."""
    return set(revision_pairs())


def _not_superseded(prefix=""):
    """SQL fragment keeping superseded items out of a browse query (#477): only the
    current revision of each chain is listed. `prefix` is a table alias like "ce."."""
    return f" AND NOT EXISTS (SELECT 1 FROM item_revisions ir WHERE ir.old_slug = {prefix}slug)"
