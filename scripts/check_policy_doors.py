"""Every item door asks core/policy.py (#557, groundwork for auth #467).

A static check (AST, no server, no DB). It fails when:
  1. a KNOWN door below no longer calls `policy.<something>` in its body;
  2. (the net) a web GET route, or an MCP read tool (constructicon_get*/download*/search*/list*/
     explain*), calls a db read that hands out items (ITEM_READS) without calling `policy.` and
     isn't in EXEMPT with a reason; this catches NEW doors nobody registered;
  3. anything outside core/policy.py (and the type registry that defines it) decides restriction
     itself: `object_types.is_restricted(`, `restricted_types(` or the retired `_not_restricted`.
     `db.list_restricted` (the admin's list of restricted items) is the one allowed exception.
  1b. (#604 follow-up 7) a door that OPENS one item (ACCESS_LOGGED) no longer records a sensitive
     item's access with `policy.note_access(...)`.
Since #603 "restricted" means sensitive: a restricted type OR an item flagged "This is sensitive";
visible to admins and the item's uploader (scripts/test_ownership_sensitive.py proves the matrix).

What it CAN'T check, honestly:
  * that the policy call is on the right rows (it only sees that the function body mentions
    `policy.`), or that a door calling a helper which calls the policy is covered;
  * doors that reach items through a helper not named in ITEM_READS (e.g. a shaper that loads
    rows itself), templates, or JS;
  * write routes (they're role-gated by web/roles.py, not item-gated here).
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
    ("web/files_feed.py", "load"): "/ files panel and GET /api/home/files (#624: the one visible list both build from)",
    ("web/routes/pages.py", "unfiled_page"): "/unfiled",
    ("web/routes/pages.py", "user_gallery_page"): "/gallery/user/{uploader}",
    ("web/routes/items.py", "api_get_image"): "GET /api/image/{slug}",
    ("web/routes/items.py", "api_get_item_text"): "GET /api/image/{slug}/text (#607)",
    ("web/routes/items.py", "api_rendered_html"): "GET /api/image/{slug}/rendered (#607)",
    ("web/routes/items.py", "api_get_revisions"): "GET /api/image/{slug}/revisions",
    ("web/routes/items.py", "api_get_similar"): "GET /api/image/{slug}/similar",
    ("web/routes/items.py", "api_gallery"): "GET /api/gallery",
    ("web/routes/items.py", "api_search"): "GET /api/search",
    ("web/routes/files.py", "get_file"): "/f/{slug}",
    ("web/routes/files.py", "get_thumbnail"): "/f/{slug}/thumb",
    ("web/routes/files.py", "api_list_brand_assets"): "GET /api/brand-assets",
    ("web/routes/files.py", "api_list_wallpapers"): "GET /api/wallpapers",
    ("web/routes/hobbies.py", "api_get_hobby"): "GET /api/hobby/{id_or_slug} (its items)",
    ("web/shapes.py", "_to_blog_entry_detail"): "GET /api/blog-entries/{slug} items",
    ("mcp_server/server.py", "constructicon_get"): "MCP get",
    ("mcp_server/server.py", "constructicon_download"): "MCP download",
    ("mcp_server/server.py", "constructicon_search"): "MCP search",
    ("mcp_server/server.py", "constructicon_get_project"): "MCP get_project (items, cover, write-up)",
    ("mcp_server/server.py", "constructicon_get_related"): "MCP get_related",
    ("mcp_server/server.py", "constructicon_list_revisions"): "MCP list_revisions",
    ("mcp_server/server.py", "constructicon_get_posts_for_tag"): "MCP get_posts_for_tag",
    ("mcp_server/server.py", "_to_public_blog_entry"): "MCP get/list blog entry items",
    ("mcp_server/server.py", "constructicon_list_brand_assets"): "MCP list_brand_assets",
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
}

# #604 follow-up 7: doors that OPEN one item must record a sensitive item's access
# (`policy.note_access(row, how)`, a no-op for an ordinary item).
ACCESS_LOGGED = {
    ("web/routes/pages.py", "object_detail_page"): "/object/{slug}",
    ("web/routes/items.py", "api_get_image"): "GET /api/image/{slug}",
    ("web/routes/files.py", "get_file"): "/f/{slug}",
    ("web/routes/files.py", "get_thumbnail"): "/f/{slug}/thumb",
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
             "web/routes/meta.py", "mcp_server/server.py"]
MCP_READ_PREFIXES = ("constructicon_get", "constructicon_download", "constructicon_search", "constructicon_list",
                     "constructicon_explain")

SELF_DECIDING = ("is_restricted(", "restricted_types(", "_not_restricted")
SELF_DECIDING_ALLOWED = {"core/policy.py", "core/object_types/__init__.py", "scripts/check_policy_doors.py"}


def _functions(tree):
    return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_policy(fn):
    return any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "policy"
               for n in ast.walk(fn))


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

    # 2. the net
    netted = 0
    netted_names = set()
    for rel in NET_FILES:
        for name, fn in _functions(tree(rel)).items():
            is_door = _is_get_route(fn) if rel.startswith("web/") else (_is_mcp_tool(fn) and name.startswith(MCP_READ_PREFIXES))
            if not is_door:
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

    print(f"known doors: {len(DOORS)}; access-logged doors: {len(ACCESS_LOGGED)}; "
          f"GET routes / MCP read tools reading items (net): {netted}; exempt: {len(EXEMPT)}")
    if failures:
        print(f"FAIL: {len(failures)} problem(s):")
        for f in failures:
            print("  " + f)
        return 1
    print("OK: every known door calls the item policy, and nothing else decides restriction")
    return 0


def _in_function(tree, lineno, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name and n.lineno <= lineno <= (n.end_lineno or n.lineno):
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
