"""Layering check (#541 phase D): every archive write goes through the service layer.

    python scripts/check_layering.py          exit 0 = clean, 1 = violations (listed)
    python scripts/check_layering.py --list   also print every db.py writer and its class

The rule (CLAUDE.md, "Service layer"): all writes go through
core/{items,membership,tags,cards,hobbies,blog,decisions,reset}.py; the raw writers in core/db.py
are private (`_`-prefixed); this script enforces it. No server, no DB: it reads the source (AST).

What fails:
  1. A module outside SERVICE_MODULES / RAW_ALLOWED calls (or imports) a private `db._name`.
  2. Anything uses one of the old public names in RETIRED_PUBLIC (they went private or away in
     phase D), or core/db.py defines one of them again.
  3. core/db.py has a PUBLIC function that writes (SQL INSERT/UPDATE/DELETE/REPLACE, an ImageLog,
     or a call to another writer) that isn't classified in PUBLIC_WRITERS. A new writer must either
     be private (owned by a service module) or be listed there with the reason it is public.
Read functions stay public and are never flagged.
"""

import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The service layer: the only modules that may call db's private writers (and private helpers).
SERVICE_MODULES = {
    "core/items.py": "item fields, redact, retype, delete-to-trash, related links (phase B/C)",
    "core/membership.py": "files on/off a card (phase C)",
    "core/tags.py": "item tags and tag creation (phase C)",
    "core/cards.py": "V2 card ops, card create/update, undo (pieces 1-7, phase D)",
    "core/hobbies.py": "hobbies: create, unmark, convert both ways (phase D)",
    "core/blog.py": "blog entries (phase D)",
    "core/decisions.py": "answering questions + the explicit stale sweep (phase D)",
    "core/reset.py": "the one delete-all (phase C)",
    "core/revisions.py": "revision chains (#477) and their questions",
    "core/install_config.py": "install identity: owner, site title, publish targets (#562)",
    "core/users.py": "user accounts, passwords and sessions (#467 step 1)",
}

# Raw writes kept on purpose, outside the service layer. Each needs a reason.
RAW_ALLOWED = {
    "core/db.py": "the writers themselves, plus the run-once migrations (db.MIGRATIONS)",
    "core/captions.py": "pipeline: caption status/results in type_metadata (machine bookkeeping; imaging "
                        "it would make every later undo of a real edit conflict with the pipeline)",
    "core/embedded_metadata.py": "pipeline: upload-time file metadata (title, ID3 tags, EXIF date), fill-only",
    "core/object_types/youtube.py": "pipeline: a YouTube row's fetched publish date (embedded metadata)",
    "core/automatch.py": "pipeline: the upload-time automatch tagging (#240)",
    "core/ocr.py": "pipeline: OCR client-domain tags",
    "core/card_migration.py": "migrations: the one-time V2 card migration (actor 'migration', refused by undo)",
    # scripts/ one-offs that write directly against the DB inside the container (kept runnable).
    "scripts/apply_project_groupings.py": "one-off 2026-09 grouping import, already applied; kept runnable",
    "scripts/seed_example_projects.py": "one-off seed of example cards on an empty dev DB",
    "scripts/backfill_from_hooptiej_site.py": "one-off backfill from the old static site (tag tree + tags)",
    "scripts/link_lil_dragon_brand.py": "one-off brand-asset relation links, already applied",
    "scripts/check_layering.py": "this checker (names the private writers in strings only)",
}
# Throwaway-DB test fixtures: they set up raw state on a temp DB before exercising the services.
RAW_ALLOWED_PATTERNS = [(re.compile(r"^scripts/test_[\w]+\.py$"), "throwaway-DB test fixtures")]

# Old public names that went private (or were removed) in phase D. Nothing may use them.
RETIRED_PUBLIC = {
    # items
    "set_type_metadata", "update_content_metadata", "rename_object", "set_agent_notes", "set_provenance",
    "set_highlight", "set_brand_asset", "set_display_date_override", "set_content_date", "set_media_type",
    "mark_redacted", "unmark_redacted", "delete_upload", "set_trash_embedding", "mark_trash_purged",
    # tags / relations / membership
    "update_tags", "sync_real_tags_for_post", "add_tags", "get_or_create_tag", "attach_tags", "detach_tag",
    "add_relation", "remove_relation", "add_item_to_project", "remove_item_from_project",
    # cards
    "create_project", "update_project", "set_project_date_overrides", "update_card_columns", "write_card_row",
    "insert_card_hobby", "delete_card_hobby", "add_family_member", "remove_family_member", "clear_family_members",
    "write_project_links", "update_hobby_columns", "delete_project", "convert_project_to_hobby",
    # hobbies
    "mark_tag_as_hobby", "unmark_hobby", "set_hobby_status", "add_project_to_hobby", "remove_project_from_hobby",
    # blog
    "create_blog_entry", "update_blog_entry", "delete_blog_entry", "set_entry_projects", "set_entry_items",
    # decisions / reset
    "resolve_pending_decision", "clear_tables",
}

# Public db.py functions that write, and why they stay public (not archive curation, or infra the
# service layer itself is built on).
PUBLIC_WRITERS = {
    "init_db": "schema DDL + run-once migrations (web process at boot)",
    "run_pending_migrations": "migrations",
    "ensure_special_clients": "vestigial imagerepo client list, seeded by sync_clients.py (no longer at boot, #562)",
    "sync_hudu_clients": "vestigial imagerepo client sync (sync_clients.py)",
    "add_test_client": "vestigial imagerepo client fixture",
    "insert_upload": "ingest pipeline: a new item row (upload / MCP upload / import)",
    "insert_content": "ingest pipeline: a new file-less item row (YouTube link, authored document)",
    "set_ocr_status": "OCR pipeline state (queue, watchdog, retry)",
    "set_extracted_text": "OCR pipeline result",
    "set_perceptual_hash": "similarity pipeline",
    "set_embedding": "similarity pipeline (a BLOB; never imaged)",
    "set_client_if_empty": "OCR pipeline: vestigial client fill",
    "enqueue_caption": "caption queue (#549: MCP enqueues)",
    "dequeue_caption": "caption queue (the web worker drains)",
    "set_setting": "app settings (keys, export config); not archive content",
    "insert_audit_log": "the request log (web middleware)",
    "insert_access_log": "the sensitive-item access log (core/access_log.py, #604 follow-up 7); a read log, not content",
    "insert_change_log": "the change log itself (core/changes.py, undo)",
    "mark_change_rows_undone": "undo bookkeeping (cards.undo)",
    "invert_image": "undo: applies one row image's inverse",
    "write_images": "ImageLog helper: imaged writes for service ops",
    "add_pending_decision": "asking a question (ingest/automatch pipeline); answering it is core/decisions.py",
    "queue_decision_once": "asking a question once (migrations, revisions)",
    "set_curator_state": "Curator queue snooze/dismiss state (core/curation_queue.py); view state, not content",
    "clear_curator_defer": "Curator queue snooze state (core/curation_queue.py)",
}

_SQL_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b|\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b",
                        re.I)


def _rel(p):
    return Path(p).resolve().relative_to(ROOT).as_posix()


def _py_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".claude", "__pycache__", "node_modules", "storage")]
        for f in files:
            if f.endswith(".py"):
                yield Path(base) / f


def db_functions():
    """{name: is_writer} for every top-level function in core/db.py."""
    src = (ROOT / "core" / "db.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    direct = {}
    calls = {}
    for name, node in funcs.items():
        writes = False
        called = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and _SQL_WRITE.search(sub.value):
                writes = True
            elif isinstance(sub, ast.JoinedStr):
                text = "".join(v.value for v in sub.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if _SQL_WRITE.search(text):
                    writes = True
            elif isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                    if f.id == "ImageLog":
                        writes = True
        direct[name] = writes
        calls[name] = called & set(funcs)
    writer = dict(direct)
    changed = True
    while changed:  # a function that calls a writer writes too
        changed = False
        for name in funcs:
            if not writer[name] and any(writer[c] for c in calls[name] if c != name):
                writer[name] = True
                changed = True
    return writer


def raw_reason(rel):
    if rel in SERVICE_MODULES:
        return "service"
    if rel in RAW_ALLOWED:
        return RAW_ALLOWED[rel]
    for pat, why in RAW_ALLOWED_PATTERNS:
        if pat.match(rel):
            return why
    return None


def main(argv):
    funcs = db_functions()
    problems = []

    # 3. db.py's public writers must be classified; retired names must not come back.
    for name, writes in sorted(funcs.items()):
        if name.startswith("_"):
            continue
        if name in RETIRED_PUBLIC:
            problems.append(f"core/db.py defines retired public writer {name}(): make it private (_{name}) "
                            "and call it from its service module")
        elif writes and name not in PUBLIC_WRITERS:
            problems.append(f"core/db.py: public function {name}() writes but is not classified: make it private "
                            "(owned by a service module) or add it to PUBLIC_WRITERS with a reason")

    # 1 + 2. callers.
    for path in _py_files():
        rel = _rel(path)
        if rel == "core/db.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as e:
            problems.append(f"{rel}: can't parse ({e})")
            continue
        allowed = raw_reason(rel)
        db_aliases = {"db", "_db"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("core.db", "db") or (
                    isinstance(node, ast.ImportFrom) and node.level and node.module == "db"):
                for a in node.names:
                    if a.name.startswith("_") and not allowed:
                        problems.append(f"{rel}:{node.lineno} imports private db.{a.name}")
                    if a.name in RETIRED_PUBLIC:
                        problems.append(f"{rel}:{node.lineno} imports retired db.{a.name}")
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "core.db" and a.asname:
                        db_aliases.add(a.asname)
            if isinstance(node, ast.ImportFrom) and node.module in ("core", None):
                for a in node.names:
                    if a.name == "db" and a.asname:
                        db_aliases.add(a.asname)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in db_aliases:
                attr = node.attr
                if attr in RETIRED_PUBLIC:
                    problems.append(f"{rel}:{node.lineno} uses retired db.{attr} (it went private in #541 phase D: "
                                    "call the service op instead)")
                elif attr.startswith("_") and attr in funcs and not allowed:
                    kind = "writer" if funcs[attr] else "helper"
                    problems.append(f"{rel}:{node.lineno} calls private db.{attr} ({kind}) outside the service layer")

    if "--list" in argv:
        print("core/db.py functions that write:")
        for name, writes in sorted(funcs.items()):
            if writes:
                cls = "private" if name.startswith("_") else f"public: {PUBLIC_WRITERS.get(name, 'UNCLASSIFIED')}"
                print(f"  {name:36} {cls}")
        print("\nraw-write allowlist:")
        for k, v in {**RAW_ALLOWED, **{p.pattern: w for p, w in RAW_ALLOWED_PATTERNS}}.items():
            print(f"  {k:44} {v}")
    n_private = sum(1 for n, w in funcs.items() if w and n.startswith("_"))
    n_public = sum(1 for n, w in funcs.items() if w and not n.startswith("_"))
    if problems:
        print(f"check_layering: {len(problems)} violation(s)")
        for p in problems:
            print("  " + p)
        return 1
    print(f"check_layering: OK ({n_private} private writers, {n_public} classified public writers, "
          f"{len(SERVICE_MODULES)} service modules, {len(RAW_ALLOWED) + len(RAW_ALLOWED_PATTERNS)} raw-write allowlist entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
