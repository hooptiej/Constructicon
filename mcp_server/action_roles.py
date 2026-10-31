"""#746: who may run which (tool, action): ACTION_ROLES and role_for (#547 step 3; moved verbatim
from server.py). mcp_server/checks.py refuses to start unless every tool and selector value has a row.
"""

from mcp_server import _boot  # noqa: F401  (the MCP role and file mode first, #549 / #652)

from core import roles


# --- #746: who may run which (tool, action) ---------------------------------------------------
# One row per visible tool, and per action for a tool with a selector (action=, view=, which=,
# direction=, or delete's target). Mirrors the web's role labels (#557): reads viewer, curation
# writes editor, anything permanent or install-wide admin. The MCP is one admin identity today
# (mcp_server/auth.py), so nothing is refused yet; the table is what per-user tokens will check.
# Startup refuses a tool or an action value with no row (_check_action_roles). A hidden alias
# uses the row of the call it stands for (its alias_of).
V, E, A = roles.VIEWER, roles.EDITOR, roles.ADMIN
ACTION_ROLES = {
    ("constructicon_version", None): V,
    ("constructicon_upload", None): E,
    ("constructicon_replace_file", None): E,
    ("constructicon_search", None): V,
    ("constructicon_get", None): V,
    ("constructicon_download", None): V,
    ("constructicon_import", None): E,
    ("constructicon_list_import", None): E,
    ("constructicon_update", None): E,
    ("constructicon_view", None): V,
    ("constructicon_set_caption", None): E,
    ("constructicon_undated", "list"): V,        # #725: undated items and their date guesses
    ("constructicon_undated", "accept"): E,      # one guess written as content_date (undoable)
    ("constructicon_undated", "apply_auto"): E,  # every high-confidence guess, one batch (undoable)
    ("constructicon_set_agent_notes", "slug"): E,  # an item's private notes (#206)
    ("constructicon_set_agent_notes", "card"): E,  # a card's (#745); same rule as the item's
    ("constructicon_add_content", None): E,
    ("constructicon_backup", None): A,
    ("constructicon_redact", "redact"): E,
    ("constructicon_redact", "unredact"): E,
    ("constructicon_redact", "recover"): E,
    ("constructicon_redact", "delete_file"): A,  # permanent
    ("constructicon_delete", "slug"): E,
    ("constructicon_delete", "slugs"): E,
    ("constructicon_delete", "card"): E,
    ("constructicon_delete", "blog_entry"): E,
    ("constructicon_delete", "everything"): A,  # the whole archive, permanent
    ("constructicon_trash", "list"): A,
    ("constructicon_trash", "empty"): A,  # permanent
    ("constructicon_duplicates", "plan"): E,   # #615: read-only, filtered per actor (policy.can_view)
    ("constructicon_duplicates", "apply"): A,  # #615: trashes copies library-wide (undoable)
    ("constructicon_lookalikes", "list"): V,     # #759: GET /api/curator/lookalikes (viewer)
    ("constructicon_lookalikes", "confirm"): E,  # POST /api/curator/lookalikes/confirm (editor; one undoable fold)
    ("constructicon_lookalikes", "dismiss"): E,  # POST /api/curator/lookalikes/dismiss (editor; remembered)
    ("constructicon_proposals", "list"): V,      # #354 / #344: GET /api/curator/provenance-rules | correlations
    ("constructicon_proposals", "confirm"): E,   # POST .../confirm (editor; one undoable batch)
    ("constructicon_proposals", "dismiss"): E,   # POST .../dismiss (editor; remembered)
    ("constructicon_item_tags", "attach"): E,
    ("constructicon_item_tags", "detach"): E,
    ("constructicon_related", "add"): E,
    ("constructicon_related", "remove"): E,
    ("constructicon_list_items", "redacted"): A,
    ("constructicon_list_items", "restricted"): A,
    ("constructicon_list_items", "brand_assets"): V,
    ("constructicon_list_items", "tag"): V,
    ("constructicon_list_items", "unfiled"): V,  # #556: the /unfiled page (viewer)
    ("constructicon_list_items", "similar"): V,  # #556: GET /api/image/{slug}/similar (viewer)
    ("constructicon_caption_review", "list"): V,    # #556: GET /api/captions/unreviewed (viewer)
    ("constructicon_caption_review", "accept"): E,  # POST /api/captions/{slug}/accept (editor)
    ("constructicon_caption_review", "skip"): E,    # POST /api/captions/{slug}/skip (editor)
    ("constructicon_caption_review", "retry"): E,   # POST /api/image/{slug}/caption (editor)
    ("constructicon_revision", "supersede"): E,
    ("constructicon_revision", "remove"): E,
    ("constructicon_run_type_action", None): E,
    ("constructicon_list_projects", None): V,
    ("constructicon_create_project", None): E,
    ("constructicon_update_project", None): E,
    ("constructicon_get_project", None): V,
    ("constructicon_explain_card", None): V,
    ("constructicon_card_files", "add"): E,
    ("constructicon_card_files", "remove"): E,
    ("constructicon_card_files", "move"): E,
    ("constructicon_card_files", "copy"): E,
    ("constructicon_card_hobby", "add"): E,
    ("constructicon_card_hobby", "remove"): E,
    ("constructicon_family", "add"): E,
    ("constructicon_family", "remove"): E,
    ("constructicon_nest", "nest"): E,
    ("constructicon_nest", "unnest"): E,
    ("constructicon_sharing", "get"): V,
    ("constructicon_sharing", "users"): E,
    ("constructicon_sharing", "set_private"): E,  # core/sharing.py then allows only the owner or an admin
    ("constructicon_sharing", "add_member"): E,
    ("constructicon_sharing", "remove_member"): E,
    ("constructicon_link", "add"): E,
    ("constructicon_link", "remove"): E,
    ("constructicon_link", "retype"): E,
    ("constructicon_link", "list"): V,
    ("constructicon_retype_links", None): E,
    ("constructicon_split_card", None): E,
    ("constructicon_merge_cards", None): E,
    ("constructicon_convert", "card_to_hobby"): A,  # whole-card conversion (web: admin)
    ("constructicon_convert", "hobby_to_card"): A,
    ("constructicon_bulk_edit", None): E,
    ("constructicon_undo", None): E,
    ("constructicon_list_changes", None): E,
    ("constructicon_list_hobbies", None): V,
    ("constructicon_create_hobby", None): E,
    ("constructicon_update_hobby", None): E,
    ("constructicon_unmark_hobby", None): E,
    ("constructicon_list_blog_entries", None): V,
    ("constructicon_get_blog_entry", None): V,
    ("constructicon_create_blog_entry", None): E,
    ("constructicon_update_blog_entry", None): E,
    ("constructicon_list_tags", None): V,
    ("constructicon_create_tag", None): E,
    # #555: tag management. Reads viewer; every write is one undoable batch, so editor (like a card edit).
    ("constructicon_tags", "tree"): V,
    ("constructicon_tags", "get"): V,
    ("constructicon_tags", "sweep"): E,
    ("constructicon_tags", "rename"): E,
    ("constructicon_tags", "merge"): E,
    ("constructicon_tags", "move"): E,
    ("constructicon_tags", "delete"): E,
    ("constructicon_tags", "sweep_apply"): E,
    ("constructicon_list_provenance_options", None): V,
    ("constructicon_list_vocab", None): V,
    ("constructicon_needs", "queue"): V,
    ("constructicon_needs", "nudges"): V,
    ("constructicon_needs", "caption"): V,
    ("constructicon_needs", "card_decisions"): V,
    ("constructicon_needs", "pending"): V,
    ("constructicon_queue_item", "dismiss"): E,
    ("constructicon_queue_item", "defer"): E,
    ("constructicon_queue_item", "bring_back"): E,
    ("constructicon_resolve_pending_decision", None): E,
    ("constructicon_resolve_decisions", None): E,
    ("constructicon_sweep_stale_decisions", None): E,
}
del V, E, A


def role_for(tool, action=None):
    """#746: the role a (tool, action) needs; an unknown pair fails closed (admin)."""
    return ACTION_ROLES.get((tool, action), roles.ADMIN)
