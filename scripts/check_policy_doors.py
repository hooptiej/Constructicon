"""Every item door asks core/policy.py (#557, groundwork for auth #467).

A static check (AST, no server, no DB). It fails when:
  1. a KNOWN door below no longer calls `policy.<something>` in its body;
  2. (the net) a web GET route calls a db read that hands out items (ITEM_READS) without calling
     `policy.` and isn't in EXEMPT with a reason; this catches NEW doors nobody registered;
  3. anything outside core/policy.py (and the type registry that defines it) decides restriction
     itself: `object_types.is_restricted(`, `restricted_types(` or the retired `_not_restricted`.
     `db.list_restricted` (the admin's list of restricted items) is the one allowed exception.
  1b. (#604 follow-up 7) a door that OPENS one item (ACCESS_LOGGED) no longer records a sensitive
     item's access with `policy.note_access(...)`.
  5. (#604 step 3) a known CARD door (CARD_DOORS) stops asking the card policy (private cards and
     hobbies), or db.insert_change_log stops running the private-card write guard (policy.check_write).
  4. (#746) EVERY MCP tool function (visible tools and hidden aliases; no name-prefix heuristic) is
     in MCP_TOOLS with a kind, and the kind holds. The tool's body is followed into the server
     helpers it calls (not into core/, except CORE_GUARDED):
       opens  - opens one item: reaches an item-policy gate AND calls policy.note_access, and is
                in ACCESS_LOGGED (which holds exactly these MCP tools);
       items  - hands out or changes existing items: reaches an item-policy gate (policy.require_view
                / filter_visible / viewable_rows / can_view ...), a DOORS function, or a core op in
                CORE_GUARDED (checked here to call its door);
       admin  - an admin-only listing or wipe: every ACTION_ROLES row of the tool (an alias: of the
                call it stands for) is admin;
       new    - creates the item it returns (nothing to check);
       none   - hands out no item rows: no ITEM_READS anywhere it reaches (MCP_SHAPERS aside).
Since #603 "restricted" means sensitive: a restricted type OR an item flagged "This is sensitive";
visible to admins and the item's uploader (scripts/test_ownership_sensitive.py proves the matrix).

What it CAN'T check, honestly:
  * that the policy call is on the right rows (it only sees that the function body mentions
    `policy.`), or that a door calling a helper which calls the policy is covered;
  * doors that reach items through a helper not named in ITEM_READS (e.g. a shaper that loads
    rows itself), templates, or JS;
  * write routes the net doesn't reach (it nets GET routes; MCP tools are rule 4). Writes are
    role-gated by web/roles.py; the item-gated ones that matter (#709: bulk delete, add-to-card,
    tagging, and the service ops behind them) are listed in DOORS by hand;
  * WHICH branch of a merged MCP tool reaches the gate (#746): list_items' tag and brand branches
    filter, and that also satisfies rule 4 for its redacted / restricted branches (admin listings by
    design); the role table (ACTION_ROLES) gates those per action. Nor whether the gate comes
    before the read. Core internals are not followed (only CORE_GUARDED's one-call check).
The behaviour itself is proven by scripts/test_role_policy.py (flip the switch, every door refuses).

    python scripts/check_policy_doors.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, function) -> what it serves. Each must call policy.* itself.
DOORS = {
    ("web/routes/pages.py", "object_detail_page"): "/object/{slug}",
    ("web/routes/pages.py", "project_detail_page"): "/project/{slug} (the card's items)",
    ("web/routes/pages.py", "hobby_detail_page"): "/hobby/{slug} (member cards' items, loose items)",
    ("web/routes/pages.py", "_home_context"): "/ (reference objects; home_page itself only renders it, #624)",
    # #553: every file grid (home Files, /unfiled, /gallery/user/{uploader}, a hobby's loose objects) and
    # GET /api/items/grid + /api/home/files build their list here (#624), so /unfiled and the uploader
    # gallery read no items themselves any more (they were doors of their own until #553).
    ("web/files_feed.py", "load"): "the file grids' visible list: home, /unfiled, user gallery, hobby loose; GET /api/items/grid",
    ("web/routes/items.py", "api_get_image"): "GET /api/image/{slug}",
    ("web/routes/items.py", "api_get_item_text"): "GET /api/image/{slug}/text (#607)",
    ("web/routes/items.py", "api_rendered_html"): "GET /api/image/{slug}/rendered (#607)",
    ("web/routes/items.py", "api_get_revisions"): "GET /api/image/{slug}/revisions",
    ("web/routes/items.py", "api_get_similar"): "GET /api/image/{slug}/similar",
    ("web/routes/items.py", "api_gallery"): "GET /api/gallery",
    ("web/routes/items.py", "api_search"): "GET /api/search",
    ("web/routes/files.py", "get_file"): "/f/{slug}",
    ("web/routes/files.py", "get_thumbnail"): "/f/{slug}/thumb",
    ("web/routes/files.py", "get_raw_file"): "/f/{slug}/raw (#703)",
    ("web/routes/files.py", "api_list_brand_assets"): "GET /api/brand-assets",
    ("web/routes/files.py", "api_list_wallpapers"): "GET /api/wallpapers",
    ("web/routes/hobbies.py", "api_get_hobby"): "GET /api/hobby/{id_or_slug} (its items)",
    ("web/shapes.py", "_to_blog_entry_detail"): "GET /api/blog-entries/{slug} items",
    ("mcp_server/server.py", "constructicon_get"): "MCP get (and its include=related / revisions)",
    ("mcp_server/server.py", "constructicon_download"): "MCP download",
    ("mcp_server/server.py", "constructicon_search"): "MCP search",
    ("mcp_server/server.py", "constructicon_get_project"): "MCP get_project (items, cover, write-up)",
    ("mcp_server/server.py", "_viewable_item"): "MCP one-item reads (the get_related / list_revisions aliases)",
    ("mcp_server/server.py", "_related_listed"): "MCP get(include=related), related, the related aliases",
    ("mcp_server/server.py", "_posts_for_tag"): "MCP list_items(which=tag), get_posts_for_tag",
    ("mcp_server/server.py", "_to_public_blog_entry"): "MCP get/list blog entry items",
    ("mcp_server/server.py", "_list_brand_assets"): "MCP list_items(which=brand_assets), list_brand_assets",
    ("mcp_server/server.py", "constructicon_card_files"): "MCP card_files (add/remove: every slug visible)",
    ("mcp_server/server.py", "constructicon_item_tags"): "MCP item_tags (detach)",
    ("mcp_server/server.py", "constructicon_set_caption"): "MCP set_caption",
    ("mcp_server/server.py", "constructicon_run_type_action"): "MCP run_type_action (#746: was ungated)",
    ("mcp_server/server.py", "_retry_ocr"): "MCP run_type_action(retry_ocr), retry_ocr (#746: was ungated)",
    ("core/site_export.py", "build_site"): "static site export",
    ("core/project_export.py", "export_project"): "project zip export",
    # #603: a flagged item can be in flight or captioned, so these name items now.
    ("web/routes/items.py", "api_processing"): "GET /api/processing (in-flight items' names)",
    ("web/routes/items.py", "api_captions_unreviewed"): "GET /api/captions/unreviewed (caption review page)",
    ("web/routes/items.py", "api_access_log"): "GET /api/image/{slug}/access-log (admin)",
    ("mcp_server/server.py", "constructicon_view"): "MCP view",
    ("core/captions.py", "needs_caption"): "MCP list_needs_caption (never a sensitive item)",
    ("core/curation_queue.py", "for_actor"): "the Curator queue (shared cache, filtered per actor)",
    ("core/decisions.py", "list_open"): "the decision queue",
    ("core/revisions.py", "chain_detail"): "revision chains on the item page / MCP",
    ("core/duplicates.py", "_build"): "#615 duplicate groups: a group with a copy the actor may not see is left out",
    # #709: the write side. Every slug a write names must be visible (else 404, nothing written),
    # checked in the service op itself so web, MCP and any future per-user token are covered.
    ("web/routes/items.py", "api_delete_selected"): "POST /api/delete (bulk delete)",
    ("web/routes/items.py", "api_bulk_add_to_project"): "POST /api/bulk/add-to-project",
    ("web/routes/items.py", "api_bulk_attach_tags"): "POST /api/bulk/attach-tags",
    ("web/routes/cards.py", "api_create_project_from_related"): "POST /api/projects/from-related (its related items)",
    ("core/items.py", "get_item"): "every single-item op (update, caption, relate, redact, retype, replace ...)",
    ("core/items.py", "delete"): "items.delete (web delete + bulk delete, MCP delete / delete_multiple)",
    ("core/items.py", "unrelate"): "items.unrelate (web + MCP remove_related)",
    ("core/items.py", "set_sensitive"): "items.set_sensitive (bulk + single, #603)",
    ("core/items.py", "set_provenance"): "items.set_provenance (bulk provenance, #553)",
    ("core/membership.py", "add_files"): "membership.add_files (bulk add-to-project, from-selection, MCP add_items_to_project ...)",
    ("core/membership.py", "remove_files"): "membership.remove_files (web + MCP remove_from_project)",
    ("core/tags.py", "_get_item"): "tags.attach / detach / set_item_tags (MCP attach_tags / detach_tag)",
    ("core/tags.py", "merge_item_tags"): "tags.merge_item_tags (bulk attach-tags)",
    ("mcp_server/server.py", "_relate"): "MCP related(add), add_related",
    ("mcp_server/server.py", "_add_to_project"): "MCP add_to_project",
    ("mcp_server/server.py", "_remove_from_project"): "MCP remove_from_project",
    ("mcp_server/server.py", "_detach_tag"): "MCP detach_tag",
    ("mcp_server/server.py", "_set_writeup"): "MCP update_project(fields.writeup_slug), set_project_writeup",
}

# #604 step 3: doors that hand out CARDS or HOBBIES (or name them: titles in a tag tree, a breadcrumb,
# a queue group) must ask the card policy (CARD_GATES), or go through cards.get_card / get_hobby, which
# do. Rule 5 below. The behaviour (owner / editor member / viewer member / outsider / admin / token, per
# door) is proven by scripts/test_private_cards_604.py.
CARD_DOORS = {
    ("web/routes/pages.py", "_home_context"): "/ (cards, hobby pills, timeline)",
    ("web/routes/pages.py", "project_detail_page"): "/project/{slug} (the card, its parts, members, links, home)",
    ("web/routes/pages.py", "hobbies_page"): "/hobbies",
    ("web/routes/pages.py", "hobby_detail_page"): "/hobby/{slug} (the hobby and its cards)",
    ("web/routes/cards.py", "api_card"): "GET /api/cards/{slug}",
    ("web/routes/cards.py", "api_projects"): "GET /api/projects",
    ("web/routes/cards.py", "api_create_project"): "POST /api/projects (its parent)",
    ("web/routes/cards.py", "_related_projects_public"): "the Related-projects widget",
    ("web/routes/hobbies.py", "api_list_hobbies"): "GET /api/hobbies",
    ("web/routes/hobbies.py", "api_hobbies_html"): "GET /api/hobbies/html",
    ("web/routes/hobbies.py", "api_get_hobby"): "GET /api/hobby/{id}",
    ("web/routes/hobbies.py", "api_add_project_to_hobby"): "POST /api/hobby/{id}/add-project (its list)",
    ("web/routes/hobbies.py", "api_remove_project_from_hobby"): "POST /api/hobby/{id}/remove-project (its list)",
    ("web/routes/curator.py", "api_curator_queue"): "GET /api/curator/queue?card=",
    ("web/routes/curator.py", "api_curator_queue_html"): "GET /api/curator/queue/html (a group's card face)",
    ("web/routes/items.py", "api_tags"): "GET /api/tags (card and hobby tag names)",
    ("web/routes/items.py", "api_set_item_date"): "POST /api/image/{slug}/date?project= (the card's span)",
    ("web/shapes.py", "_to_hobby_face"): "a hobby's card face (its cards)",
    ("web/shapes.py", "_to_blog_entry_detail"): "a blog entry's cards",
    ("web/common.py", "_build_breadcrumbs"): "the item page's ?from=project: breadcrumb",
    ("core/cards.py", "get_card"): "every card op (reads and writes)",
    ("core/cards.py", "get_hobby"): "every hobby op",
    ("core/membership.py", "get_card"): "files on / off a card",
    ("core/cards.py", "family_fields"): "families / members of a card",
    ("core/cards.py", "list_links"): "a card's links (and its face's relationship lines)",
    ("core/cards.py", "resolve_home"): "a card's home (a manual override may name any card)",
    ("core/cards.py", "list_needs_decision"): "card needs (MCP, the queue)",
    ("core/cards.py", "explain_card"): "explain_card (children, links, home)",
    ("core/cards.py", "list_changes"): "the change log (row images)",
    ("core/cards.py", "resolve_hobby_cover"): "a hobby's automatic cover (its cards)",
    ("core/cards.py", "create"): "a new card's parent",
    ("core/curation_queue.py", "for_actor"): "the Curator queue (card / hobby groups, options)",
    ("core/decisions.py", "list_open"): "the decision queue (card questions, project_match candidates)",
    ("core/curator_needs.py", "list_needs"): "Curator nudges",
    ("core/site_export.py", "build_site"): "the static site export (nothing private is published)",
    ("core/project_export.py", "export_project"): "the project zip",
    ("mcp_server/server.py", "constructicon_list_projects"): "MCP list_projects",
    ("mcp_server/server.py", "constructicon_get_project"): "MCP get_project",
    ("mcp_server/server.py", "constructicon_list_hobbies"): "MCP list_hobbies",
    ("mcp_server/server.py", "constructicon_list_tags"): "MCP list_tags",
    ("mcp_server/server.py", "constructicon_update_project"): "MCP update_project",
    ("mcp_server/server.py", "constructicon_card_files"): "MCP card_files",
    ("mcp_server/server.py", "constructicon_add_project_to_hobby"): "MCP add_project_to_hobby (alias)",
    ("mcp_server/server.py", "_to_public_blog_entry"): "MCP blog entry cards",
}
CARD_GATES = {"can_view_card", "can_view_hobby", "require_card_view", "filter_visible_cards", "filter_visible_hobbies",
              "card_access", "access_scope", "outsider_access", "filter_tag_tree", "hidden_tag_ids"}
# #604 step 3: the write guard on private cards runs for every logged write.
WRITE_GUARD = ("core/db.py", "insert_change_log", "check_write")

# #604 follow-up 7: doors that OPEN one item must record a sensitive item's access
# (`policy.note_access(row, how)`, a no-op for an ordinary item).
ACCESS_LOGGED = {
    ("web/routes/pages.py", "object_detail_page"): "/object/{slug}",
    ("web/routes/items.py", "api_get_image"): "GET /api/image/{slug}",
    ("web/routes/files.py", "get_file"): "/f/{slug}",
    ("web/routes/files.py", "get_thumbnail"): "/f/{slug}/thumb",
    ("web/routes/files.py", "get_raw_file"): "/f/{slug}/raw (#703)",
    ("mcp_server/server.py", "constructicon_get"): "MCP get",
    ("mcp_server/server.py", "constructicon_download"): "MCP download",
    ("mcp_server/server.py", "constructicon_view"): "MCP view",
}

# db reads that hand out item rows.
ITEM_READS = {"get_by_slug", "list_project_items", "list_related", "search", "list_entry_items",
              "list_posts_for_tag", "list_loose_hobby_objects", "list_unfiled_items",
              "list_recent_items_by_type", "list_loose_reference_objects", "list_brand_assets",
              "list_wallpapers", "list_unaccepted_captions", "get_processing_rows_by_slugs"}

# Net exemptions: (file, function) -> why it needs no item policy. An entry the net no longer
# reaches is stale and fails the check (so this list can't quietly rot).
EXEMPT = {
    # (#603 retired the two old entries: the processing drawer and the caption-review list now name
    # items a flagged image can be among, so they are DOORS above.)
}

NET_FILES = ["web/routes/pages.py", "web/routes/items.py", "web/routes/files.py", "web/routes/cards.py",
             "web/routes/hobbies.py", "web/routes/curator.py", "web/routes/blog_export.py", "web/routes/admin.py",
             "web/routes/meta.py"]

# --- #746: rule 4, every MCP tool ---------------------------------------------------------------------
MCP = "mcp_server/server.py"
# Core service ops that guard themselves through a door: (file, op) -> the door function its body
# must call (checked). A tool reaching one of these counts as gated.
CORE_GUARDED = {
    ("core/items.py", "update"): "get_item",
    ("core/items.py", "set_caption"): "get_item",
    ("core/items.py", "relate"): "get_item",
    ("core/items.py", "redact"): "get_item",
    ("core/items.py", "unredact"): "get_item",
    ("core/items.py", "recover_redacted"): "get_item",
    ("core/items.py", "delete_redacted_file"): "get_item",
    ("core/items.py", "replace_file"): "get_item",
    ("core/tags.py", "attach"): "_get_item",
    ("core/tags.py", "detach"): "_get_item",
    ("core/duplicates.py", "plan"): "_build",   # #615: plans through the visibility filter
    ("core/duplicates.py", "apply"): "_build",  # #615: re-plans through it, then policy.viewable_rows
}
# How server.py names the core modules (for `items.delete(...)` -> core/items.py:delete).
MODULE_FILES = {"items": "core/items.py", "tags_svc": "core/tags.py", "membership": "core/membership.py",
                "captions": "core/captions.py", "curation_queue": "core/curation_queue.py",
                "decisions": "core/decisions.py", "revisions": "core/revisions.py", "cards": "core/cards.py",
                "hobbies": "core/hobbies.py", "blog": "core/blog.py", "reset": "core/reset.py",
                "duplicates": "core/duplicates.py"}
# The policy calls that decide visibility (`policy.restriction_reason` in a shaper is not a gate).
POLICY_GATES = {"require_view", "filter_visible", "viewable_rows", "can_view", "viewable_item", "require_file",
                "filter_exportable"}
# Server helpers the walk does not descend into: why each hands out no item it wasn't given.
MCP_SHAPERS = {
    "_to_public": "shapes one row the caller already holds",
    "_to_public_listed": "shapes one row the caller already holds",
    "_to_public_project": "reads a card's items only to date the card (its effective span); hands out none",
}
# Every MCP tool function (visible tools and #746's hidden aliases): (kind, what it hands out / why).
O, I, ADM, NEW, NONE = "opens", "items", "admin", "new", "none"
MCP_TOOLS = {
    "constructicon_get": (O, "one item, whole"),
    "constructicon_download": (O, "one item's bytes"),
    "constructicon_view": (O, "one item's picture"),
    "constructicon_upload": (NEW, "the item it creates"),
    "constructicon_import": (NEW, "the item it creates"),
    "constructicon_add_content": (NEW, "the item it creates"),
    "constructicon_search": (I, "matching items"),
    "constructicon_replace_file": (I, "the item it changed (items.replace_file)"),
    "constructicon_get_related": (I, "related items (alias)"),
    "constructicon_list_revisions": (I, "a revision chain (alias)"),
    "constructicon_update": (I, "the item it changed"),
    "constructicon_list_needs_caption": (I, "items needing a caption (alias; captions.needs_caption)"),
    "constructicon_set_caption": (I, "the item it changed"),
    "constructicon_undated": (I, "undated items and their guesses (filter_visible); the items it dated (#725)"),
    "constructicon_redact": (I, "the item it changed (items.redact / unredact / recover / delete_redacted_file)"),
    "constructicon_unredact": (I, "alias"),
    "constructicon_recover_redacted": (I, "alias"),
    "constructicon_delete_redacted_file": (I, "alias"),
    "constructicon_list_items": (I, "items of one kind (tag / brand branches filter; redacted / restricted are "
                                    "admin listings, gated by ACTION_ROLES per action)"),
    "constructicon_list_redacted": (ADM, "every redacted item: the admin's list (alias)"),
    "constructicon_list_restricted": (ADM, "every sensitive item: the admin's list (alias)"),
    "constructicon_delete": (I, "deletes items (items.delete), a card, an entry, or everything (admin)"),
    "constructicon_delete_multiple": (I, "alias (items.delete)"),
    "constructicon_delete_all": (ADM, "the whole-archive wipe (alias); counts only"),
    "constructicon_trash": (ADM, "deleted items' names and sizes; the purge"),
    "constructicon_list_trash": (ADM, "alias"),
    "constructicon_empty_trash": (ADM, "alias"),
    "constructicon_backup": (ADM, "a backup file's name"),
    "constructicon_duplicates": (I, "duplicate groups (duplicates.plan / apply, filtered per actor); the copies it trashed"),
    "constructicon_set_sensitive": (I, "alias (items.set_sensitive)"),
    "constructicon_related": (I, "related items"),
    "constructicon_add_related": (I, "alias"),
    "constructicon_remove_related": (I, "alias (items.unrelate)"),
    "constructicon_card_files": (I, "the files it moved"),
    "constructicon_add_to_project": (I, "alias"),
    "constructicon_remove_from_project": (I, "alias"),
    "constructicon_add_items_to_project": (I, "alias (membership.add_files)"),
    "constructicon_set_project_writeup": (I, "alias"),
    "constructicon_update_project": (I, "a card; its write-up item (fields.writeup_slug)"),
    "constructicon_get_project": (I, "a card's items, cover and write-up"),
    "constructicon_get_posts_for_tag": (I, "alias"),
    "constructicon_item_tags": (I, "the item it changed"),
    "constructicon_attach_tags": (I, "alias (tags.attach)"),
    "constructicon_detach_tag": (I, "alias"),
    "constructicon_retry_ocr": (I, "alias"),
    "constructicon_run_type_action": (I, "the item it ran on"),
    "constructicon_set_agent_notes": (I, "the item it changed (items.update); card= (#745) touches no item"),
    "constructicon_set_provenance": (I, "alias (items.update)"),
    "constructicon_set_content_date": (I, "alias (items.update)"),
    "constructicon_set_highlight": (I, "alias (items.update)"),
    "constructicon_set_brand_asset": (I, "alias (items.update)"),
    "constructicon_list_brand_assets": (I, "alias"),
    "constructicon_create_blog_entry": (I, "an entry's items"),
    "constructicon_get_blog_entry": (I, "an entry's items"),
    "constructicon_update_blog_entry": (I, "an entry's items"),
    "constructicon_set_blog_entry_projects": (I, "alias"),
    "constructicon_set_blog_entry_items": (I, "alias"),
    "constructicon_needs": (I, "the queue (curation_queue.for_actor), questions (decisions.list_open), "
                               "caption candidates (captions.needs_caption)"),
    "constructicon_curation_queue": (I, "alias"),
    "constructicon_list_pending_decisions": (I, "alias"),
    "constructicon_version": (NONE, "build info"),
    "constructicon_list_import": (NONE, "inbox file names (not items)"),
    "constructicon_list_projects": (NONE, "cards"),
    "constructicon_create_project": (NONE, "a card"),
    "constructicon_explain_card": (NONE, "a card's facts and counts (no item rows)"),
    "constructicon_list_hobbies": (NONE, "hobbies (cover slugs resolved through policy.can_view in core)"),
    "constructicon_create_hobby": (NONE, "a hobby"),
    "constructicon_update_hobby": (NONE, "a hobby (hobbies.set_cover checks policy.can_view in core)"),
    "constructicon_unmark_hobby": (NONE, "a hobby"),
    "constructicon_card_hobby": (NONE, "card membership"),
    "constructicon_convert": (NONE, "a card or hobby"),
    "constructicon_convert_project_to_hobby": (NONE, "alias"),
    "constructicon_convert_hobby_to_card": (NONE, "alias"),
    "constructicon_add_project_to_hobby": (NONE, "alias"),
    "constructicon_set_hobby_status": (NONE, "alias"),
    "constructicon_set_hobby_code": (NONE, "alias"),
    "constructicon_set_hobby_cover": (NONE, "alias"),
    "constructicon_list_blog_entries": (NONE, "entries without their items"),
    "constructicon_delete_blog_entry": (NONE, "alias"),
    "constructicon_list_tags": (NONE, "the tag tree"),
    "constructicon_create_tag": (NONE, "a tag"),
    "constructicon_list_provenance_options": (NONE, "option lists"),
    "constructicon_set_project_status": (NONE, "alias"),
    "constructicon_set_status": (NONE, "alias"),
    "constructicon_set_kind": (NONE, "alias"),
    "constructicon_set_whereabouts": (NONE, "alias"),
    "constructicon_set_card_text": (NONE, "alias"),
    "constructicon_set_card_provenance": (NONE, "alias"),
    "constructicon_set_card_highlight": (NONE, "alias"),
    "constructicon_set_home": (NONE, "alias"),
    "constructicon_nest": (NONE, "card nesting"),
    "constructicon_unnest": (NONE, "alias"),
    "constructicon_family": (NONE, "card membership"),
    "constructicon_add_to_family": (NONE, "alias"),
    "constructicon_remove_from_family": (NONE, "alias"),
    "constructicon_link": (NONE, "card links"),
    "constructicon_sharing": (NONE, "a card's / hobby's privacy and members (core/sharing.py; hidden cards are not_found)"),
    "constructicon_unlink": (NONE, "alias"),
    "constructicon_retype_link": (NONE, "alias"),
    "constructicon_retype_links": (NONE, "card links"),
    "constructicon_list_links": (NONE, "alias"),
    "constructicon_list_needs_decision": (NONE, "alias (card needs)"),
    # Card reshaping moves files by slug in core/cards.py, which has no item-policy check of its own
    # (it predates #709's write rule; a follow-up). They hand out slugs, not item rows.
    "constructicon_split_card": (NONE, "cards and file slugs"),
    "constructicon_merge_cards": (NONE, "cards and file slugs"),
    "constructicon_delete_project": (NONE, "alias"),
    "constructicon_move_files": (NONE, "alias"),
    "constructicon_copy_files": (NONE, "alias"),
    "constructicon_add_to_hobby": (NONE, "alias"),
    "constructicon_remove_from_hobby": (NONE, "alias"),
    "constructicon_bulk_edit": (NONE, "card changes"),
    "constructicon_resolve_decisions": (NONE, "card changes"),
    "constructicon_resolve_pending_decision": (NONE, "what the answer applied"),
    "constructicon_sweep_stale_decisions": (NONE, "resolved question ids"),
    # The change log's row images carry whatever was written (item fields too). Since #604 steps 3 / 5
    # (per-user tokens) a non-admin reads only rows that touch nothing hidden from them, and can't undo
    # one that does (core/sharing.py visible_change_rows / check_undo_allowed, policy.change_row_visible).
    "constructicon_undo": (NONE, "change rows"),
    "constructicon_list_changes": (NONE, "change rows"),
    "constructicon_list_needs": (NONE, "alias (nudges)"),
    "constructicon_queue_item": (NONE, "{ok, changed}"),
    "constructicon_dismiss_need": (NONE, "alias"),
    "constructicon_defer": (NONE, "alias"),
    "constructicon_bring_back": (NONE, "alias"),
    # Revision chains by slug: core/revisions.py has no item-policy check (predates #709; a follow-up).
    "constructicon_revision": (NONE, "slugs of a chain"),
    "constructicon_mark_superseded": (NONE, "alias"),
    "constructicon_remove_from_revisions": (NONE, "alias"),
}

SELF_DECIDING = ("is_restricted(", "restricted_types(", "_not_restricted")
SELF_DECIDING_ALLOWED = {"core/policy.py", "core/object_types/__init__.py", "scripts/check_policy_doors.py"}


def _functions(tree):
    return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_policy(fn):
    return any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "policy"
               for n in ast.walk(fn))


def _calls_card_policy(fn):
    """#604 step 3: the function asks the card policy, resolves its card through get_card / get_hobby, or
    filters through curation_queue.for_actor (itself a card door)."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "policy"                 and n.attr in CARD_GATES:
            return True
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if name in ("get_card", "get_hobby", "for_actor") and not (isinstance(f, ast.Attribute)
                                                          and isinstance(f.value, ast.Name) and f.value.id == "db"):
                return True
    return False


def _db_reads(fn):
    return {n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "db" and n.func.attr in ITEM_READS}


def _is_get_route(fn):
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "get":
            return True
    return False


def _is_mcp_tool(fn):
    return any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
               for d in fn.decorator_list)


def main():
    failures = []
    trees = {}

    def tree(rel):
        if rel not in trees:
            trees[rel] = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        return trees[rel]

    # 1. known doors
    for (rel, name), what in DOORS.items():
        fn = _functions(tree(rel)).get(name)
        if fn is None:
            failures.append(f"{rel}:{name} ({what}): door not found (renamed? update DOORS)")
        elif not _calls_policy(fn):
            failures.append(f"{rel}:{fn.lineno} {name} ({what}): doesn't call core/policy.py")

    # 1b. #604 follow-up 7: the single-item doors log a sensitive item's access
    for (rel, name), what in ACCESS_LOGGED.items():
        fn = _functions(tree(rel)).get(name)
        if fn is None:
            failures.append(f"{rel}:{name} ({what}): access-logged door not found (renamed? update ACCESS_LOGGED)")
        elif not any(isinstance(n, ast.Attribute) and n.attr == "note_access" and isinstance(n.value, ast.Name)
                     and n.value.id == "policy" for n in ast.walk(fn)):
            failures.append(f"{rel}:{fn.lineno} {name} ({what}): doesn't record sensitive access (policy.note_access)")

    # 5. #604 step 3: card / hobby doors ask the card policy
    for (rel, name), what in CARD_DOORS.items():
        fn = _functions(tree(rel)).get(name)
        if fn is None:
            failures.append(f"{rel}:{name} ({what}): card door not found (renamed? update CARD_DOORS)")
        elif not _calls_card_policy(fn):
            failures.append(f"{rel}:{fn.lineno} {name} ({what}): doesn't ask the card policy "
                            f"(policy.{'/'.join(sorted(CARD_GATES))[:60]}..., or cards.get_card / get_hobby)")
    rel, name, gate = WRITE_GUARD
    fn = _functions(tree(rel)).get(name)
    if fn is None or not any(isinstance(n, ast.Attribute) and n.attr == gate for n in ast.walk(fn)):
        failures.append(f"{rel}:{name}: no longer calls policy.{gate} (the private-card write guard, #604 step 3)")

    # 2. the net
    netted = 0
    netted_names = set()
    for rel in NET_FILES:
        for name, fn in _functions(tree(rel)).items():
            if not _is_get_route(fn):
                continue
            reads = _db_reads(fn)
            if not reads:
                continue
            netted += 1
            netted_names.add((rel, name))
            if (rel, name) in EXEMPT or (rel, name) in DOORS:
                continue
            if not _calls_policy(fn):
                failures.append(f"{rel}:{fn.lineno} {name}: reads items ({', '.join(sorted(reads))}) without "
                                "calling core/policy.py (add the policy call, or EXEMPT it here with a reason)")
    for key in EXEMPT:
        if key not in netted_names:
            failures.append(f"{key[0]}:{key[1]}: stale EXEMPT entry (the net no longer reaches it); remove it")

    # 4. #746: every MCP tool, classified, and the class holds
    mcp_counts = _check_mcp_tools(tree, failures)

    # 3. nobody else decides restriction
    for path in list((ROOT / "core").rglob("*.py")) + list((ROOT / "web").rglob("*.py")) + \
            list((ROOT / "mcp_server").rglob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in SELF_DECIDING_ALLOWED or rel.startswith("scripts/test_"):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(tok in line for tok in SELF_DECIDING):
                if rel == "core/db.py" and "restricted_types()" in line and _in_function(tree(rel), i, "list_restricted"):
                    continue
                failures.append(f"{rel}:{i}: decides restriction itself ({line.strip()[:90]}); ask core/policy.py")

    print(f"known doors: {len(DOORS)}; card doors: {len(CARD_DOORS)}; access-logged doors: {len(ACCESS_LOGGED)}; "
          f"GET routes reading items (net): {netted}; exempt: {len(EXEMPT)}; MCP tools classified: "
          + ", ".join(f"{k} {v}" for k, v in sorted(mcp_counts.items())))
    if failures:
        print(f"FAIL: {len(failures)} problem(s):")
        for f in failures:
            print("  " + f)
        return 1
    print("OK: every known door calls the item policy, and nothing else decides restriction")
    return 0


def _mcp_walk(name, fns, tools, seen):
    """(item reads, gated) for server.py function `name` and the server helpers it calls (not into
    other tools or MCP_SHAPERS). Gated = it calls a POLICY_GATES function, a DOORS function, or a
    CORE_GUARDED op."""
    fn = fns[name]
    reads, gated = set(_db_reads(fn)), False
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "policy" \
                and n.attr in POLICY_GATES:
            gated = True
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in MODULE_FILES:
            key = (MODULE_FILES[f.value.id], f.attr)
            gated |= key in DOORS or key in CORE_GUARDED
        elif isinstance(f, ast.Name) and f.id in fns:
            gated |= (MCP, f.id) in DOORS
            if f.id not in seen and f.id not in tools and f.id not in MCP_SHAPERS:
                seen.add(f.id)
                r, g = _mcp_walk(f.id, fns, tools, seen)
                reads |= r
                gated |= g
    return reads, gated


def _mcp_roles(tree):
    """{tool: [roles of its ACTION_ROLES rows]} and {alias: (tool, action)}, read from server.py."""
    rows, aliases, names = {}, {}, {"V": "viewer", "E": "editor", "A": "admin"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ACTION_ROLES" for t in n.targets):
            for k, v in zip(n.value.keys, n.value.values):
                tool, action = (e.value for e in k.elts)
                rows.setdefault(tool, {})[action] = names.get(getattr(v, "id", ""), "?")
        if isinstance(n, ast.FunctionDef):
            for d in n.decorator_list:
                for kw in getattr(d, "keywords", []):
                    if kw.arg == "alias_of":
                        aliases[n.name] = tuple(e.value for e in kw.value.elts)
    return rows, aliases


def _check_mcp_tools(tree, failures):
    """Rule 4 (#746): every MCP tool function is in MCP_TOOLS and its kind holds."""
    t = tree(MCP)
    fns = {n.name: n for n in ast.walk(t) if isinstance(n, ast.FunctionDef)}
    tools = {name for name, fn in fns.items() if _is_mcp_tool(fn)}
    for name in sorted(tools - set(MCP_TOOLS)):
        failures.append(f"{MCP}:{fns[name].lineno} {name}: an MCP tool not in MCP_TOOLS (classify it: opens / "
                        "items / admin / new / none, with what it hands out)")
    for name in sorted(set(MCP_TOOLS) - tools):
        failures.append(f"{MCP}:{name}: stale MCP_TOOLS entry (no such tool); remove it")
    for (rel, op), door in CORE_GUARDED.items():
        fn = _functions(tree(rel)).get(op)
        if fn is None or not any(isinstance(c, ast.Call) and getattr(c.func, "id", getattr(c.func, "attr", None)) == door
                                 for c in ast.walk(fn)):
            failures.append(f"{rel}:{op}: CORE_GUARDED says it calls {door}(), and it doesn't (any more)")
    roles, aliases = _mcp_roles(t)
    logged = {name for (rel, name) in ACCESS_LOGGED if rel == MCP}
    counts = {}
    for name in sorted(tools & set(MCP_TOOLS)):
        kind, why = MCP_TOOLS[name]
        counts[kind] = counts.get(kind, 0) + 1
        reads, gated = _mcp_walk(name, fns, tools, {name})
        where = f"{MCP}:{fns[name].lineno} {name} ({kind}: {why})"
        if kind in ("opens", "items") and not gated:
            failures.append(f"{where}: reaches no item-policy gate, door or guarded core op")
        if kind == "opens" and not any(isinstance(n, ast.Attribute) and n.attr == "note_access" for n in ast.walk(fns[name])):
            failures.append(f"{where}: opens one item but doesn't record it (policy.note_access)")
        if (kind == "opens") != (name in logged):
            failures.append(f"{where}: ACCESS_LOGGED must list exactly the MCP tools of kind 'opens'")
        if kind == "none" and reads:
            failures.append(f"{where}: reads items ({', '.join(sorted(reads))}) though classified as handing out none")
        if kind == "admin":
            target = aliases.get(name, (name, None))
            got = [roles.get(target[0], {}).get(target[1])] if name in aliases else list(roles.get(name, {}).values())
            if not got or any(r != "admin" for r in got):
                failures.append(f"{where}: an admin listing, but its ACTION_ROLES rows say {got or 'nothing'}")
        if kind not in ("opens", "items", "admin", "new", "none"):
            failures.append(f"{where}: unknown kind")
    return counts


def _in_function(tree, lineno, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name and n.lineno <= lineno <= (n.end_lineno or n.lineno):
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
