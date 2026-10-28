#!/usr/bin/env python3
"""The MCP tool surface was folded from 108 tools into 55 without losing a capability (#746).
Throwaway DB, no server, no network.

    python scripts/test_mcp_consolidation_746.py

In-process (scripts/_testenv.py: temp DB + storage), the MCP tool functions called directly. One fixed
fixture (fixed item slugs, cards, hobbies, tags, a blog entry, a revision chain, a held redaction, a
sensitive item, a brand asset, a trashed item, open questions) is restored before every scenario, so
each scenario starts from the same state. Covers:
  1. ALIAS PARITY: every retired tool name (65) still answers with the SAME JSON as before #746:
     each scenario's (normalized) result equals scripts/data/mcp_aliases_golden_746.json, which was
     written by the pre-#746 server against this fixture (`--write-golden`, below);
  2. THE NEW CALL REPRODUCES IT: for every alias scenario, the matching new call (the issue's map)
     leaves the database exactly as the alias did (every table but the change log), and for a read
     returns the same items;
  3. tools/list shows the 55 tools and no alias; tools/call answers an alias; each alias call logs one
     line naming the old and the new tool; aliases keep their old defaults (set_highlight /
     set_card_highlight / set_brand_asset with no value clear);
  4. one change-log batch per merged call (update_project with five fields, update with fields,
     update_hobby, update_blog_entry, item_tags detach of two tags), and one undo restores it all;
  5. multi-field dry_run is the real thing rolled back: kind + stage together (stage legal only for
     the new kind) passes as a dry run and writes nothing;
  6. refusal codes per action: redact(delete_file) and trash(empty) need their confirm; delete's
     whole-archive wipe needs BOTH everything=true AND the phrase (neither alone wipes anything);
  7. merged `related` filters by the item policy; needs(view="queue") goes through
     curation_queue.for_actor;
  8. the (tool, action) -> role table: every visible tool has its rows; a non-admin role is refused
     per action (an editor may redact but not delete_file; a viewer may list links but not add one);
  9. descriptions and tools/list: no outputSchema, no issue numbers in a listed description, the
     destructive annotation on delete / redact / trash; server instructions carry the shared
     conventions.
Exits 1 if any check fails.

Writing the golden file (only when the pre-#746 behaviour must be captured again): put the old
server next to the new one and point the script at it, e.g.
    git show <pre-746 commit>:mcp_server/server.py > mcp_server/_old_server_746.py
    python scripts/test_mcp_consolidation_746.py --write-golden mcp_server/_old_server_746.py
    rm mcp_server/_old_server_746.py
"""

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("mcp746-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core import actor, db, paths, roles  # noqa: E402
_testenv.assert_isolated()

FAILS = []
GOLDEN = os.path.join(HERE, "data", "mcp_aliases_golden_746.json")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# No background work in this test: OCR / thumbnails / captions would write while scenarios run.
SPAWNED = []
actor.spawn = lambda fn, *a, **k: SPAWNED.append(getattr(fn, "__name__", str(fn)))

db.init_db()

WRITE_GOLDEN = "--write-golden" in sys.argv
if WRITE_GOLDEN:
    old_path = sys.argv[sys.argv.index("--write-golden") + 1]
    spec = importlib.util.spec_from_file_location("mcp_server.server", os.path.abspath(old_path))
    S = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server.server"] = S
    spec.loader.exec_module(S)
else:
    from mcp_server import server as S  # noqa: E402  (after the DB env is set)

from core import blog, cards, card_migration, hobbies, items, membership, revisions  # noqa: E402
from core import tags as tags_svc  # noqa: E402

# A real 1x1 PNG, so image items have a picture (hobby covers check that).
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")


def put(slug, filename, media_type, text="", data=None):
    ext = filename.rsplit(".", 1)[-1]
    (paths.storage_dir() / f"{slug}.{ext}").write_bytes(data if data is not None else f"{slug}\n".encode())
    db.insert_upload(slug, filename, f"{slug}.{ext}", "tester", media_type=media_type)
    if text:
        db.set_extracted_text(slug, text)


def build_fixture():
    with actor.acting_as(actor.ACTOR_SCRIPT):
        for slug, fn, mt, text in [("img1", "rover.png", "image", ""), ("img2", "wheel.png", "image", ""),
                                   ("doc1", "notes.md", "document", "hello world"),
                                   ("doc2", "notes v2.md", "document", "hello again"),
                                   ("doc3", "more notes.md", "document", "third doc"),
                                   ("red1", "secret.png", "image", ""), ("sens1", "key.md", "document", "k"),
                                   ("brand1", "logo.png", "image", ""), ("gone1", "old.png", "image", ""),
                                   ("pdf1", "manual.zip", "archive", "")]:
            put(slug, fn, mt, text, PNG if mt == "image" else None)
        rover = cards.create("Rover").data["card"]
        cards.create("Arm", parent=rover["id"])
        cards.create("Shelf Thing", kind="thing")
        cards.create("Fleet", kind="family")
        cards.create("Misc")
        membership.add_files("rover", ["img1", "doc1"], **membership.UI_EFFECTS)
        membership.add_files("misc", ["img2"], **membership.UI_EFFECTS)
        cards.add_to_family("fleet", "shelf-thing")
        cards.link("rover", "shelf-thing", "related")
        cards.link("arm", "shelf-thing", "used_in")
        hobbies.create("Robots")
        hobbies.create("Collecting")
        cards.add_to_hobby("rover", "robots")
        tags_svc.create("Hardware")
        tags_svc.create("Motors", "Hardware")
        tags_svc.attach("img1", ["Motors"])
        items.relate("img1", "img2")
        revisions.mark_superseded("doc1", "doc2")
        items.redact("red1")
        items.set_sensitive("sens1", True)
        items.update("brand1", is_brand_asset=True, brand_role="logo")
        items.update("img1", highlight=True)
        cards.set_highlight("rover", True)
        items.delete(["gone1"])
        entry = blog.create("Post").data["entry"]
        blog.set_projects(entry["id"], [("rover", "the rover")])
        blog.set_items(entry["id"], [("img1", "a photo")])
        opts = [{"key": "done", "label": "Done", "patch": [card_migration._set_status_patch("done")]},
                {"key": "paused", "label": "Paused", "patch": [card_migration._set_status_patch("paused")]}]
        db.add_pending_decision(cards.KIND_CARD_STATUS, cards.card_decision_slug("rover"),
                                {"question": "Still rolling?", "field": "stage", "suggested": "done", "options": opts})
        db.add_pending_decision("retype", "doc3", {"question": "What is it?",
                                                    "options": [{"key": "document", "label": "Document"},
                                                                {"key": "image", "label": "Image"}]})


SNAP_DB = os.path.join(TMP, "fixture.db")
SNAP_STORAGE = os.path.join(TMP, "fixture-storage")


def snapshot_fixture():
    src, dst = sqlite3.connect(db.DB_PATH), sqlite3.connect(SNAP_DB)
    src.backup(dst)
    src.close()
    dst.close()
    shutil.copytree(paths.storage_dir(), SNAP_STORAGE)


def restore():
    src, dst = sqlite3.connect(SNAP_DB), sqlite3.connect(db.DB_PATH)
    src.backup(dst)
    src.close()
    dst.close()
    shutil.rmtree(paths.storage_dir())
    shutil.copytree(SNAP_STORAGE, paths.storage_dir())


# ---- normalizing: what differs run to run (batch ids, the clock, auto-made write-up slugs) -------------
BATCH_RE = re.compile(r"\b[0-9a-f]{16}\b")
NOW_FLOOR = 1.6e9  # 2020-09: any number above it is "when this ran", masked (fixture dates are earlier)
FIXED_SLUGS = {"img1", "img2", "doc1", "doc2", "doc3", "red1", "sens1", "brand1", "gone1", "pdf1"}
RANDOM_SLUGS = {}  # an auto-made item's random slug -> "<item:ID>" (ids are the same every run)


def learn_slugs():
    """Remember every random item slug in the DB (items and the trash) by its id."""
    conn = sqlite3.connect(db.DB_PATH)
    rows = conn.execute("SELECT id, slug FROM capture_events").fetchall()
    conn.close()
    for i, s in rows:
        if s not in FIXED_SLUGS:
            RANDOM_SLUGS.setdefault(s, f"<item:{i}>")


def norm(value):
    if isinstance(value, dict):
        return {norm(k) if isinstance(k, str) else k: norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)) and value > NOW_FLOOR:
        return "<time>"
    if isinstance(value, str):
        for s, name in RANDOM_SLUGS.items():
            if s in value:
                value = value.replace(s, name)
        return BATCH_RE.sub("<batch>", value)
    return value


def as_json(value):
    learn_slugs()
    return json.loads(json.dumps(norm(value), default=str))


SKIP_TABLES = {"audit_log", "item_access_log", "sqlite_sequence", "schema_migrations", "sessions"}


def dump_db():
    """Every table's rows (not the change log), sorted, with the clock and batch ids masked."""
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    out = {}
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in names:
        if t in SKIP_TABLES or t.startswith("item_search"):
            continue
        rows = []
        for r in conn.execute(f"SELECT * FROM {t}"):
            d = {k: r[k] for k in r.keys() if k != "embedding"}
            for k, v in list(d.items()):
                if isinstance(v, str) and v[:1] in "[{":
                    try:
                        d[k] = json.loads(v)
                    except ValueError:  # silent-ok: a text column that merely starts with a bracket
                        pass
            rows.append(json.dumps(as_json(d), sort_keys=True))
        out[t] = sorted(rows)
    conn.close()
    return out


def dump_diff(a, b):
    out = []
    for t in sorted(set(a) | set(b)):
        if a.get(t) != b.get(t):
            only_a = [r for r in a.get(t, []) if r not in b.get(t, [])][:2]
            only_b = [r for r in b.get(t, []) if r not in a.get(t, [])][:2]
            out.append(f"{t}: alias-only {only_a} / new-only {only_b}")
    return "; ".join(out)[:1500]


def call(name, **kw):
    fn = getattr(S, "constructicon_" + name)
    return fn(**kw)


# ---- the scenarios: (label, alias, alias kwargs, new tool, new kwargs, how a read compares) ------------
# `read`: None = a write (compare the database after both); a function = a read (compare outputs:
# read(new_result) must equal the alias result).
def ident(r):
    return r


def key_of(k):
    return lambda r: r[k]


def scenarios(ctx):
    nudge = ctx["nudge_key"]
    d_card = ctx["decision_card"]
    return [
        ("list_needs_caption", "list_needs_caption", {"limit": 50}, "needs", {"view": "caption"}, ident),
        ("unredact (held: refused)", "unredact", {"slug": "red1"}, "redact", {"slug": "red1", "action": "unredact"}, ident),
        ("recover_redacted", "recover_redacted", {"slug": "red1"}, "redact", {"slug": "red1", "action": "recover"}, None),
        ("delete_redacted_file", "delete_redacted_file", {"slug": "red1", "confirm": True},
         "redact", {"slug": "red1", "action": "delete_file", "confirm": True}, None),
        ("delete_redacted_file (no confirm)", "delete_redacted_file", {"slug": "red1"},
         "redact", {"slug": "red1", "action": "delete_file"}, ident),
        ("list_redacted", "list_redacted", {}, "list_items", {"which": "redacted"}, ident),
        ("list_restricted", "list_restricted", {}, "list_items", {"which": "restricted"}, ident),
        ("delete_multiple", "delete_multiple", {"slugs": ["img2", "nope"]},
         "delete", {"slugs": ["img2", "nope"], "missing_ok": True}, None),
        ("list_trash", "list_trash", {}, "trash", {"action": "list"}, ident),
        ("empty_trash", "empty_trash", {"confirm": "EMPTY TRASH"}, "trash", {"action": "empty", "confirm": "EMPTY TRASH"}, None),
        ("empty_trash (wrong phrase)", "empty_trash", {"confirm": "nope"}, "trash", {"action": "empty", "confirm": "nope"}, ident),
        ("delete_all", "delete_all", {"confirm": "DELETE EVERYTHING"},
         "delete", {"everything": True, "confirm": "DELETE EVERYTHING"}, None),
        ("delete_all (no phrase)", "delete_all", {}, "delete", {"everything": True}, ident),
        ("set_sensitive", "set_sensitive", {"slug": "img2"}, "update", {"slug": "img2", "fields": {"sensitive": True}}, None),
        ("set_sensitive (clear)", "set_sensitive", {"slug": "sens1", "sensitive": False},
         "update", {"slug": "sens1", "fields": {"sensitive": False}}, None),
        ("add_related", "add_related", {"slug": "img1", "related_slug": "doc3"},
         "related", {"slug": "img1", "other": "doc3", "action": "add"}, None),
        ("remove_related", "remove_related", {"slug": "img1", "related_slug": "img2"},
         "related", {"slug": "img1", "other": "img2", "action": "remove"}, None),
        ("get_related", "get_related", {"slug": "img1"}, "get", {"slug": "img1", "include": ["related"]}, key_of("related")),
        ("add_to_project", "add_to_project", {"slug": "img2", "project_id": "rover"},
         "card_files", {"card": "rover", "slugs": ["img2"], "action": "add"}, None),
        ("remove_from_project", "remove_from_project", {"slug": "img1", "project_id": "rover"},
         "card_files", {"card": "rover", "slugs": ["img1"], "action": "remove"}, None),
        ("add_items_to_project", "add_items_to_project", {"project_id": "rover", "slugs": ["img2", "doc3", "nope"]},
         "card_files", {"card": "rover", "slugs": ["img2", "doc3", "nope"], "missing_ok": True}, None),
        ("set_project_writeup", "set_project_writeup", {"project_id": "rover", "slug": "doc3", "owner_words": True},
         "update_project", {"card": "rover", "fields": {"writeup_slug": "doc3", "owner_words": True}}, None),
        ("convert_project_to_hobby", "convert_project_to_hobby", {"project_slug": "misc"},
         "convert", {"direction": "card_to_hobby", "card": "misc"}, None),
        ("convert_hobby_to_card", "convert_hobby_to_card", {"hobby": "robots", "kind": "family"},
         "convert", {"direction": "hobby_to_card", "hobby": "robots", "kind": "family"}, None),
        ("add_project_to_hobby", "add_project_to_hobby", {"project_slug": "shelf-thing", "hobby_slug": "robots"},
         "card_hobby", {"card": "shelf-thing", "hobby": "robots"}, None),
        ("set_hobby_status", "set_hobby_status", {"hobby_slug": "robots", "status": "dormant"},
         "update_hobby", {"hobby": "robots", "status": "dormant"}, None),
        ("set_hobby_code", "set_hobby_code", {"hobby_slug": "robots", "group_code": "rob"},
         "update_hobby", {"hobby": "robots", "group_code": "rob"}, None),
        ("set_hobby_cover", "set_hobby_cover", {"hobby": "robots", "cover_slug": "img1"},
         "update_hobby", {"hobby": "robots", "cover_slug": "img1"}, None),
        ("delete_blog_entry", "delete_blog_entry", {"slug": "post"}, "delete", {"blog_entry": "post"}, None),
        ("set_blog_entry_projects", "set_blog_entry_projects",
         {"slug": "post", "items": [{"project_id": "shelf-thing", "note": "n"}, {"project_id": "rover"}]},
         "update_blog_entry", {"slug": "post", "projects": [{"project_id": "shelf-thing", "note": "n"},
                                                             {"project_id": "rover"}]}, None),
        ("set_blog_entry_items", "set_blog_entry_items", {"slug": "post", "items": [{"slug": "img2", "note": "w"}]},
         "update_blog_entry", {"slug": "post", "items": [{"slug": "img2", "note": "w"}]}, None),
        ("get_posts_for_tag", "get_posts_for_tag", {"tag_name": "Hardware"},
         "list_items", {"which": "tag", "tag": "Hardware"}, key_of("items")),  # #722: paged {items, next_cursor}
        ("attach_tags", "attach_tags", {"slug": "img2", "tag_names": ["Motors", "Brand New"]},
         "item_tags", {"slug": "img2", "tag_names": ["Motors", "Brand New"]}, None),
        ("detach_tag", "detach_tag", {"slug": "img1", "tag_name": "Motors"},
         "item_tags", {"slug": "img1", "tag_names": ["Motors"], "action": "detach"}, None),
        ("retry_ocr", "retry_ocr", {"slug": "pdf1"}, "run_type_action", {"slug": "pdf1", "action": "retry_ocr"}, None),
        ("retry_ocr (no OCR for the type)", "retry_ocr", {"slug": "doc3"},
         "run_type_action", {"slug": "doc3", "action": "retry_ocr"}, ident),
        ("retry_ocr (redacted)", "retry_ocr", {"slug": "red1"},
         "run_type_action", {"slug": "red1", "action": "retry_ocr"}, ident),
        ("set_provenance", "set_provenance", {"slug": "img1", "provenance": "created"},
         "update", {"slug": "img1", "fields": {"provenance": "created"}}, None),
        ("set_content_date", "set_content_date", {"slug": "img1", "date": "2017-07-31"},
         "update", {"slug": "img1", "fields": {"content_date": "2017-07-31"}}, None),
        ("set_highlight (old default unmarks)", "set_highlight", {"slug": "img1"},
         "update", {"slug": "img1", "fields": {"highlight": False}}, None),
        ("set_brand_asset (old default clears)", "set_brand_asset", {"slug": "brand1"},
         "update", {"slug": "brand1", "fields": {"is_brand_asset": False}}, None),
        ("set_brand_asset (mark)", "set_brand_asset", {"slug": "img2", "is_brand": True, "brand_role": "icon"},
         "update", {"slug": "img2", "fields": {"is_brand_asset": True, "brand_role": "icon"}}, None),
        ("list_brand_assets", "list_brand_assets", {}, "list_items", {"which": "brand_assets"}, ident),
        ("set_project_status", "set_project_status", {"id_or_slug": "rover", "status": "shelved"},
         "update_project", {"card": "rover", "status": "shelved"}, None),
        ("set_status", "set_status", {"card": "rover", "stage": "done"}, "update_project", {"card": "rover", "stage": "done"}, None),
        ("set_kind", "set_kind", {"card": "shelf-thing", "kind": "project"},
         "update_project", {"card": "shelf-thing", "kind": "project"}, None),
        ("set_whereabouts", "set_whereabouts", {"card": "shelf-thing", "whereabouts": "have_it", "note": "on the shelf"},
         "update_project", {"card": "shelf-thing", "fields": {"whereabouts": "have_it", "whereabouts_note": "on the shelf"}},
         None),
        ("set_card_text (card)", "set_card_text", {"card": "rover", "synopsis": "A rover.", "flavor": "Vroom."},
         "update_project", {"card": "rover", "fields": {"synopsis": "A rover.", "flavor": "Vroom."}}, None),
        ("set_card_text (hobby)", "set_card_text", {"hobby": "robots", "synopsis": "Robots I built."},
         "update_hobby", {"hobby": "robots", "synopsis": "Robots I built."}, None),
        ("set_card_provenance", "set_card_provenance", {"card": "rover", "provenance": "created", "credit": "me"},
         "update_project", {"card": "rover", "fields": {"provenance": "created", "provenance_credit": "me"}}, None),
        ("set_card_highlight (old default unmarks)", "set_card_highlight", {"card": "rover"},
         "update_project", {"card": "rover", "fields": {"highlight": False}}, None),
        ("unnest", "unnest", {"child": "arm"}, "nest", {"child": "arm", "action": "unnest"}, None),
        ("add_to_family", "add_to_family", {"family": "fleet", "member": "rover"},
         "family", {"family": "fleet", "member": "rover"}, None),
        ("remove_from_family", "remove_from_family", {"family": "fleet", "member": "shelf-thing"},
         "family", {"family": "fleet", "member": "shelf-thing", "action": "remove"}, None),
        ("unlink", "unlink", {"a": "rover", "b": "shelf-thing"}, "link", {"a": "rover", "b": "shelf-thing", "action": "remove"}, None),
        ("retype_link", "retype_link", {"a": "rover", "b": "shelf-thing", "from_type": "related", "to_type": "inspired_by"},
         "link", {"a": "rover", "b": "shelf-thing", "action": "retype", "from_type": "related", "to_type": "inspired_by"},
         None),
        ("list_links", "list_links", {"card": "shelf-thing"}, "link", {"a": "shelf-thing", "action": "list"}, ident),
        ("list_needs_decision", "list_needs_decision", {}, "needs", {"view": "card_decisions"}, ident),
        ("list_needs_decision (kind)", "list_needs_decision", {"kind": "thing"},
         "needs", {"view": "card_decisions", "card_kind": "thing"}, ident),
        ("delete_project", "delete_project", {"card": "misc"}, "delete", {"card": "misc"}, None),
        ("move_files", "move_files", {"slugs": ["img2"], "from_card": "misc", "to_card": "rover"},
         "card_files", {"card": "rover", "slugs": ["img2"], "action": "move", "from_card": "misc"}, None),
        ("copy_files", "copy_files", {"slugs": ["img2"], "from_card": "misc", "to_card": "rover"},
         "card_files", {"card": "rover", "slugs": ["img2"], "action": "copy", "from_card": "misc"}, None),
        ("add_to_hobby", "add_to_hobby", {"card": "shelf-thing", "hobby": "collecting"},
         "card_hobby", {"card": "shelf-thing", "hobby": "collecting"}, None),
        ("remove_from_hobby", "remove_from_hobby", {"card": "rover", "hobby": "robots"},
         "card_hobby", {"card": "rover", "hobby": "robots", "action": "remove"}, None),
        ("set_home", "set_home", {"card": "arm", "target": "card:shelf-thing"},
         "update_project", {"card": "arm", "fields": {"home": "card:shelf-thing"}}, None),
        ("list_needs", "list_needs", {}, "needs", {"view": "nudges"}, ident),
        ("dismiss_need", "dismiss_need", {"nudge_key": nudge}, "queue_item", {"key": nudge, "action": "dismiss"}, None),
        ("curation_queue", "curation_queue", {}, "needs", {"view": "queue"}, ident),
        ("defer", "defer", {"key": f"decision:{d_card}"}, "queue_item", {"key": f"decision:{d_card}", "action": "defer"}, None),
        ("bring_back", "bring_back", {"key": f"decision:{d_card}"},
         "queue_item", {"key": f"decision:{d_card}", "action": "bring_back"}, None),
        ("list_pending_decisions", "list_pending_decisions", {}, "needs", {"view": "pending"}, ident),
        ("mark_superseded", "mark_superseded", {"old": "doc2", "new": "doc3"},
         "revision", {"slug": "doc2", "action": "supersede", "new": "doc3"}, None),
        ("remove_from_revisions", "remove_from_revisions", {"slug": "doc2"}, "revision", {"slug": "doc2", "action": "remove"}, None),
        ("list_revisions", "list_revisions", {"slug": "doc1"}, "get", {"slug": "doc1", "include": ["revisions"]},
         lambda r: {"ok": True, **r["revisions"]}),
    ]


# ---- build the fixture -------------------------------------------------------------------------------
build_fixture()
learn_slugs()
from core import curator_needs  # noqa: E402
CTX = {}
with actor.acting_as(actor.ACTOR_MCP):
    _nudges = curator_needs.list_needs()
    CTX["nudge_key"] = _nudges[0]["nudge_key"] if _nudges else "no-nudge"
_conn = sqlite3.connect(db.DB_PATH)
CTX["decision_card"] = _conn.execute("SELECT id FROM pending_decisions WHERE kind = ?",
                                     (cards.KIND_CARD_STATUS,)).fetchone()[0]
_conn.close()
snapshot_fixture()
SCEN = scenarios(CTX)

if WRITE_GOLDEN:
    golden = {"_about": "#746: what each retired MCP tool name returned BEFORE the consolidation, on the fixture "
                        "in scripts/test_mcp_consolidation_746.py (batch ids and clock values masked). Written by "
                        "that script's --write-golden with the pre-#746 server; the test compares every alias to it.",
              "results": {}}
    for label, alias, akw, *_ in SCEN:
        restore()
        golden["results"][label] = as_json(call(alias, **akw))
    with open(GOLDEN, "w", encoding="utf-8", newline="\n") as f:
        json.dump(golden, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {GOLDEN}: {len(golden['results'])} scenarios")
    print("ALL PASS")
    sys.exit(0)

# ---- 1 + 2. alias parity and the new call ----------------------------------------------------------------
print("--- 1/2. every alias: same JSON as before, and the new call does the same ---")
GOLD = json.load(open(GOLDEN, encoding="utf-8"))["results"]


MASKED_725 = ("effective_date", "effective_start", "effective_end")


def golden_view(label, value):
    """What the golden comparison compares (see mask_725). Two scenarios also changed on purpose in #725:
    the nudge list is ordered by each card's most recent REAL date (a card with none falls back to its
    created_at, no longer an upload time), so list_needs compares as a set (sorted by key); and the queue
    gained `needs_date` items (an undated file with no card range to borrow), so curation_queue compares
    without them (empty groups dropped, counts recomputed)."""
    value = mask_555(mask_765(mask_725(value)))
    if label in ("move_files", "copy_files") and isinstance(value, dict):
        # #555: a transfer used to count a file that had the card's tag but not its CHIP as gaining the
        # tag (the #590 chip stamp). Chips are derived now, so only real post_tags changes count; the
        # tag rows and counts are left out here and covered by scripts/test_move_tags_590.py.
        value = {k: v for k, v in value.items() if k not in ("tags_added", "tags_removed")}
        value["changes"] = [c for c in value.get("changes", []) if c.get("field") != "tag"]
    return golden_view_rest(label, value)


def golden_view_rest(label, value):
    if label == "list_needs" and isinstance(value, list):
        return sorted(value, key=lambda n: n.get("nudge_key", ""))
    if label == "curation_queue" and isinstance(value, dict) and "groups" in value:
        out = dict(value)
        removed = 0
        for section in ("groups", "deferred"):
            kept = []
            for g in value.get(section, []):
                its = [i for i in g["items"] if i.get("kind") != "needs_date"]
                removed += len(g["items"]) - len(its)
                if its:
                    kept.append({**g, "items": its})
            out[section] = kept
        c = dict(value.get("counts") or {})
        n_open = sum(len(g["items"]) for g in out["groups"])
        c["needs"] = c.get("needs", 0) - (c.get("open", 0) - n_open)
        c["open"] = n_open
        c["deferred"] = sum(len(g["items"]) for g in out["deferred"])
        out["counts"] = c
        return out
    return value


# #765 (batch 4, "Family values"): a family / collection's write-up and dates checks are graded by its
# members, so the REASON of those two nudges on a group card is worded differently ("0 of 1 members are
# dated ..."). The same nudges still fire; only that text is masked, and only on group cards.
MASKED_765_KINDS = ("missing_writeup", "missing_dates")


def _group_card_ids():
    return {p["id"] for p in db.list_projects() if (p.get("kind") or "project") in ("family", "collection")}


def mask_555(value):
    """#555 changed an item's chips on purpose: they are derived from the tag tree (one store), never
    the linked tag of a card the item is on, and nothing writes the free-text column any more. So an
    item's `tags` list and change rows of capture_events.tags (the old #274 / #590 chip stamping) are
    left out of the golden comparison; the tree itself (post_tags rows) is still compared."""
    drop = object()

    def walk(v):
        if isinstance(v, dict):
            if v.get("table") == "capture_events" and v.get("field") == "tags":
                return drop
            return {k: ("<chips #555>" if k == "tags" and isinstance(x, list) else walk(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [w for w in (walk(x) for x in v) if w is not drop]
        return v
    return walk(value)


def mask_765(value):
    groups = {f":project:{i}" for i in _group_card_ids()}

    def walk(v):
        if isinstance(v, dict):
            key = str(v.get("nudge_key") or v.get("key") or "")
            hit = v.get("kind") in MASKED_765_KINDS and any(key.endswith(g) for g in groups)
            return {k: ("<reason>" if hit and k in ("summary", "detail") else walk(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(value)


def mask_725(value):
    """#725 changed what an item's `effective_date` is (its REAL date, or null when undated, never the
    upload time) and a card's `effective_start` / `effective_end` (real dates only, null when it has
    none, never its created_at), and added `date_shown`, after this golden was written by the pre-#746
    server (which no longer exists, so it can't be re-captured). For the golden comparison only, both
    sides mask those fields; the "new call: same answer" checks below still compare the full current
    answers."""
    if isinstance(value, dict):
        return {k: ("<date>" if k in MASKED_725 else mask_725(v)) for k, v in value.items() if k != "date_shown"}
    if isinstance(value, list):
        return [mask_725(v) for v in value]
    return value


# Schema added AFTER the golden file was written: a new column shows up in row images, a new table in
# delete-all's counts. Leave those keys out of the comparison (the golden file stays the pre-#746 record).
ADDED_SINCE_746 = {"private",        # #604 step 3: projects.private, hobby_settings.private
                   "share_members",  # #604 step 3: delete-all clears the member lists
                   "short_title",    # #542: capture_events.short_title, in item answers and row images
                   # #554 (batch 5): a hobby can borrow a member card's cover (hobby_settings.cover_project_id,
                   # the answers' picked_cover_card_id), and a renamed hobby's old slugs are kept.
                   "cover_project_id", "picked_cover_card_id", "picked_cover_card", "hobby_slug_history",
                   # #722: list results trim big type_metadata values and say so
                   "type_metadata_truncated", "type_metadata_cut_keys"}


def _strip_added(value):
    if isinstance(value, dict):
        return {k: _strip_added(v) for k, v in value.items() if k not in ADDED_SINCE_746}
    if isinstance(value, list):
        return [_strip_added(v) for v in value]
    return value


aliases_seen = set()
for label, alias, akw, new, nkw, read in SCEN:
    aliases_seen.add("constructicon_" + alias)
    restore()
    got = _strip_added(as_json(call(alias, **akw)))
    check(f"alias {label}: same JSON as before #746",
          golden_view(label, got) == golden_view(label, _strip_added(GOLD.get(label))),
          f"got {json.dumps(got)[:300]} want {json.dumps(GOLD.get(label))[:300]}")
    after_alias = dump_db()
    restore()
    new_out = call(new, **nkw)
    if read is None:
        ok = not (isinstance(new_out, dict) and new_out.get("ok") is False)
        check(f"new {new}({', '.join(f'{k}=' for k in nkw)}) for {label}: succeeds when the alias did",
              ok or (isinstance(got, dict) and got.get("ok") is False), new_out)
        check(f"new {new} for {label}: same database as the alias", dump_db() == after_alias,
              dump_diff(after_alias, dump_db()))
    else:
        new_err = isinstance(new_out, dict) and new_out.get("ok") is False
        if new_err or (isinstance(got, dict) and got.get("ok") is False):
            # a refusal: the same code (the new tool's message may name the new call)
            check(f"new {new} for {label}: refused with the same code",
                  new_err and new_out["error"]["code"] == (got.get("error") or {}).get("code"), new_out)
        else:
            # `got` had ADDED_SINCE_746 stripped; strip the same keys from the new call's answer
            check(f"new {new} for {label}: same answer", _strip_added(as_json(read(new_out))) == got,
                  f"{json.dumps(as_json(new_out))[:300]}")
all_aliases = {n for n in S._ALIAS_OF}
check("every alias has a parity scenario", all_aliases <= aliases_seen, sorted(all_aliases - aliases_seen))
check("65 retired names, all hidden", len(all_aliases) == 65 and all(a in S._HIDDEN_ALIASES for a in all_aliases),
      len(all_aliases))

# ---- 3. tools/list and tools/call ----------------------------------------------------------------------
print("--- 3. tools/list hides aliases, tools/call answers them, each alias call is logged ---")
restore()
listed = asyncio.run(S.mcp.list_tools())
names = {t.name for t in listed}
# #746 folded 108 into 55; #725 added constructicon_undated, #604 constructicon_sharing, #615
# constructicon_duplicates, #555 constructicon_tags, #556 constructicon_caption_review (60).
check("tools/list: 60 tools", len(names) == 60, len(names))
check("tools/list: no alias listed", not (names & set(S._HIDDEN_ALIASES)), sorted(names & set(S._HIDDEN_ALIASES)))
SNAPNAMES = json.load(open(os.path.join(HERE, "data", "mcp_tool_names_700.json"), encoding="utf-8"))["names"]
check("tools/list equals scripts/data/mcp_tool_names_700.json", sorted(names) == sorted(SNAPNAMES),
      sorted(set(names) ^ set(SNAPNAMES)))


class Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


grab = Grab()
logging.getLogger("constructicon.mcp.alias").addHandler(grab)
res = asyncio.run(S.mcp.call_tool("constructicon_list_links", {"card": "rover"}))
check("tools/call answers a hidden alias", not getattr(res, "is_error", False), res)
check("the alias call is logged, naming the old and the new tool",
      any("constructicon_list_links" in m and "constructicon_link" in m and "list" in m for m in grab.lines), grab.lines)
n = len(grab.lines)
S.constructicon_link("rover", action="list")
check("a call by the new name is not logged as an alias", len(grab.lines) == n, grab.lines[n:])
res = asyncio.run(S.mcp.call_tool("constructicon_search", {"query": "zzzznothing"}))
check("an empty list comes back as a visible [] (not zero content blocks)",
      [c.text for c in res.content] == ["[]"], res.content)
check("no tool publishes an outputSchema", all(t.output_schema is None for t in listed))
check("no listed description carries an issue number or history",
      not [t.name for t in listed if re.search(r"#\d{3}", t.description or "")],
      [t.name for t in listed if re.search(r"#\d{3}", t.description or "")])
destr = {t.name for t in listed if t.annotations and t.annotations.destructive_hint}
check("delete, redact and trash carry the destructive annotation",
      {"constructicon_delete", "constructicon_redact", "constructicon_trash"} <= destr, destr)
instr = S.mcp.instructions or ""
check("the server instructions say the shared conventions once (errors, undo, dry_run, policy)",
      all(w in instr for w in ('"ok": false', "batch_id", "constructicon_undo", "dry_run", "not_found", "forbidden")))

# ---- 4. one batch per merged call, one undo ----------------------------------------------------------------
print("--- 4. one change-log batch per merged call; one undo restores it ---")


def batches_since(n_before):
    conn = sqlite3.connect(db.DB_PATH)
    rows = conn.execute("SELECT DISTINCT batch_id FROM audit_log WHERE id > ? AND batch_id IS NOT NULL",
                        (n_before,)).fetchall()
    conn.close()
    return {r[0] for r in rows}


def last_audit():
    conn = sqlite3.connect(db.DB_PATH)
    n = conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]
    conn.close()
    return n


for label, tool, kw in [
    ("update_project, five fields", "update_project",
     {"card": "shelf-thing", "stage": "in_use", "title": "Shelf Thing II",
      "fields": {"whereabouts": "have_it", "provenance": "created", "highlight": True, "synopsis": "On a shelf."}}),
    ("update, fields", "update", {"slug": "img2", "description": "a wheel",
                                  "fields": {"provenance": "created", "highlight": True, "sensitive": True,
                                             "content_date": "2015-05-05"}}),
    ("update_hobby, four changes", "update_hobby", {"hobby": "robots", "status": "inactive", "group_code": "RBT",
                                                     "cover_slug": "img1", "synopsis": "All the robots."}),
    ("update_blog_entry, fields + cards + items", "update_blog_entry",
     {"slug": "post", "title": "Post II", "projects": [{"project_id": "shelf-thing"}], "items": [{"slug": "img2"}]}),
    ("item_tags detach of two tags", "item_tags", {"slug": "img1", "tag_names": ["Motors", "Hardware"], "action": "detach"}),
]:
    restore()
    if label.startswith("item_tags"):
        with actor.acting_as(actor.ACTOR_SCRIPT):
            tags_svc.attach("img1", ["Hardware"])
    before = dump_db()
    n0 = last_audit()
    out = call(tool, **kw)
    bs = batches_since(n0)
    check(f"{label}: succeeds", not (isinstance(out, dict) and out.get("ok") is False), out)
    check(f"{label}: ONE change-log batch, the one returned", len(bs) == 1 and out.get("batch_id") in bs, (bs, out.get("batch_id")))
    check(f"{label}: something changed", dump_db() != before)
    u = S.constructicon_undo(out.get("batch_id"))
    check(f"{label}: one undo restores every field", u.get("ok") is True and dump_db() == before,
          u if u.get("ok") is not True else dump_diff(before, dump_db()))

# ---- 5. multi-field dry run = the real thing, rolled back --------------------------------------------------
print("--- 5. multi-field dry_run runs for real inside a transaction and rolls back ---")
restore()
before = dump_db()
n0 = last_audit()
# an event can't be in_use; kind thing -> event plus stage done together: done is legal for an event.
dr = S.constructicon_update_project(card="shelf-thing", kind="event", stage="done", dry_run=True)
check("kind + stage together (legal only for the new kind): the dry run passes",
      dr.get("dry_run") is True and dr.get("kind") == "event" and dr.get("stage") == "done", dr)
check("the dry run wrote nothing (no row, no change-log entry)", dump_db() == before and last_audit() == n0)
bad = S.constructicon_update_project(card="shelf-thing", kind="event", stage="in_use", dry_run=True)
check("a combination the new kind refuses is refused by the dry run too",
      bad.get("ok") is False and bad["error"]["code"] == "bad_status", bad)
dr2 = S.constructicon_update(slug="img2", fields={"provenance": "created", "sensitive": True}, dry_run=True)
check("update dry run: the item as it would be, nothing written",
      dr2.get("dry_run") is True and dr2.get("provenance") == "created" and dr2.get("sensitive") is True
      and dump_db() == before, dr2)
check("unknown fields key: refused, named", S.constructicon_update(slug="img2", fields={"colour": "red"})["error"]["code"]
      == "unknown_field")
check("agent_notes is not a fields key (only set_agent_notes writes it)",
      S.constructicon_update(slug="img2", fields={"agent_notes": "x"})["error"]["code"] == "unknown_field")
check("a fields value of the wrong type is refused",
      S.constructicon_update_project(card="rover", fields={"highlight": "yes"})["error"]["code"] == "bad_fields")

# ---- 6. refusal codes per action, and the two-key wipe ------------------------------------------------------
print("--- 6. refusal codes per action; the whole-archive wipe needs both keys ---")
restore()
r = S.constructicon_redact("red1", action="delete_file")
check("redact(delete_file) without confirm: confirm_required", r.get("error", {}).get("code") == "confirm_required", r)
r = S.constructicon_trash(action="empty", confirm="nope")
check("trash(empty) with the wrong phrase: confirm_required", r.get("error", {}).get("code") == "confirm_required", r)
n_items = len(db.search(query=None, tags=None))
r = S.constructicon_delete(everything=True)
check("delete(everything=true) without the phrase: confirm_required, nothing wiped",
      r.get("error", {}).get("code") == "confirm_required" and len(db.search(query=None, tags=None)) == n_items, r)
r = S.constructicon_delete(confirm="DELETE EVERYTHING")
check("delete(confirm=phrase) without everything=true: refused, nothing wiped",
      r.get("ok") is False and len(db.search(query=None, tags=None)) == n_items, r)
r = S.constructicon_delete(slug="img2", confirm="DELETE EVERYTHING")
check("delete(slug, confirm=phrase): refused (the phrase never rides on another target)",
      r.get("ok") is False and db.get_by_slug("img2") is not None, r)
r = S.constructicon_delete(everything=True, confirm="DELETE EVERYTHING", slug="img2")
check("delete(everything + phrase + a slug): refused, nothing wiped",
      r.get("ok") is False and len(db.search(query=None, tags=None)) == n_items, r)
r = S.constructicon_delete(everything=True, confirm="DELETE EVERYTHING")
check("delete(everything=true, confirm=phrase): wipes the archive",
      bool(r.get("counts")) and len(db.search(query=None, tags=None)) == 0, r)
restore()
check("delete with no target: refused", S.constructicon_delete().get("ok") is False)
check("delete(slug=unknown): not_found", S.constructicon_delete(slug="nope")["error"]["code"] == "not_found")
check("delete(slugs=[unknown]) without missing_ok: not_found (loud)",
      S.constructicon_delete(slugs=["img2", "nope"])["error"]["code"] == "not_found")
r = S.constructicon_redact("img2", confirm=True)
check("a parameter that doesn't apply to the action is refused", r.get("ok") is False, r)
check("link(action=add) without type: refused", S.constructicon_link("rover", "arm").get("ok") is False)
check("needs: a filter of another view is refused",
      S.constructicon_needs(view="pending", nudge_kind="missing_cover").get("ok") is False)

# ---- 7. policy on the merged reads -------------------------------------------------------------------
print("--- 7. related filters by the item policy; the queue goes through for_actor ---")
restore()
real_filter = S.policy.filter_visible
S.policy.filter_visible = lambda rows, *a, **k: []
try:
    r = S.constructicon_related("img1", "doc3")
    check("related(add) answers through policy.filter_visible", r == [], r)
    r = S.constructicon_related("img1", "doc3", action="remove")
    check("related(remove) answers through policy.filter_visible", r == [], r)
finally:
    S.policy.filter_visible = real_filter
seen = []
real_for_actor = S.curation_queue.for_actor
S.curation_queue.for_actor = lambda q, *a, **k: (seen.append(1), real_for_actor(q, *a, **k))[1]
try:
    S.constructicon_needs(view="queue")
    check("needs(view=queue) goes through curation_queue.for_actor", seen == [1], seen)
finally:
    S.curation_queue.for_actor = real_for_actor

# ---- 8. the (tool, action) -> role table -----------------------------------------------------------------
print("--- 8. who may run which (tool, action) ---")
visible = {n for n in S._TOOL_FUNCS if n not in S._ALIAS_OF}
check("60 visible tool functions (#746's 55 + constructicon_undated #725, constructicon_sharing #604, "
      "constructicon_duplicates #615, constructicon_tags #555, constructicon_caption_review #556)",
      len(visible) == 60, len(visible))
check("every visible tool has its role rows (startup refuses otherwise)",
      all(any(k[0] == n for k in S.ACTION_ROLES) for n in visible))
check("permanent actions need admin",
      all(S.role_for(*k) == roles.ADMIN for k in [("constructicon_delete", "everything"), ("constructicon_redact", "delete_file"),
                                                   ("constructicon_trash", "empty"), ("constructicon_backup", None)]))
check("an unknown (tool, action) fails closed (admin)", S.role_for("constructicon_redact", "explode") == roles.ADMIN)
real_role_of = S.roles.role_of
try:
    S.roles.role_of = lambda a: roles.EDITOR
    restore()
    r = S.constructicon_redact("img2", action="delete_file", confirm=True)
    check("as an editor: redact(delete_file) is refused (forbidden)", r.get("error", {}).get("code") == "forbidden", r)
    r = S.constructicon_redact("img2")
    check("as an editor: redact(redact) runs", r.get("redacted") is True, r)
    r = S.constructicon_delete(everything=True, confirm="DELETE EVERYTHING")
    check("as an editor: the whole-archive wipe is refused", r.get("error", {}).get("code") == "forbidden", r)
    r = S.constructicon_delete_all("DELETE EVERYTHING")
    check("as an editor: the delete_all alias is refused too (its role is the call it stands for)",
          r.get("error", {}).get("code") == "forbidden", r)
    S.roles.role_of = lambda a: roles.VIEWER
    check("as a viewer: link(list) runs", isinstance(S.constructicon_link("rover", action="list"), list))
    r = S.constructicon_link("rover", "arm", "used_in")
    check("as a viewer: link(add) is refused", r.get("error", {}).get("code") == "forbidden", r)
finally:
    S.roles.role_of = real_role_of

# ---- 9. a few behaviours of the new calls ----------------------------------------------------------------
print("--- 9. new-call details ---")
restore()
g = S.constructicon_get("img1")
check("get without include: no related / revisions / agent_notes keys",
      not ({"related", "revisions", "agent_notes"} & set(g)), sorted(g))
S.constructicon_set_agent_notes("img1", "inventoried")
g = S.constructicon_get("img1", include=["agent_notes", "related", "revisions"])
check("get(include=...) adds each one asked for", g.get("agent_notes") == "inventoried" and isinstance(g.get("related"), list)
      and "in_chain" in g.get("revisions", {}), sorted(g))
check("get(include=[unknown]): bad_request", S.constructicon_get("img1", include=["bogus"])["error"]["code"] == "bad_request")
check("search / list_projects never carry agent notes",
      not any("agent_notes" in r for r in S.constructicon_search(query="rover"))
      and not any("agent_notes" in c for c in S.constructicon_list_projects()))
up = S.constructicon_update_project(project_id="rover", title="Rover II")
check("update_project still takes project_id (old callers)", up.get("title") == "Rover II", up)
check("update_project with both card and project_id: refused",
      S.constructicon_update_project(project_id="rover", card="rover").get("ok") is False)
r = S.constructicon_update_project(card="rover", fields={"owner_words": True})
check("fields.owner_words alone needs a write-up to mark (the card's blank one is used)",
      r.get("ok") is not False or r["error"]["code"] == "bad_request", r)
r = S.constructicon_run_type_action("pdf1", "retry_ocr")
check("run_type_action(retry_ocr): built in, returns {ok, action, item}",
      r.get("ok") is True and r.get("action") == "retry_ocr" and r["item"]["ocr_status"] == "pending", r)
r = S.constructicon_card_files("rover", ["nope"])
check("card_files(add) with an unknown slug: not_found unless missing_ok", r["error"]["code"] == "not_found", r)
r = S.constructicon_card_files("rover", ["img2"], action="move")
check("card_files(move) without from_card: refused", r.get("ok") is False, r)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
