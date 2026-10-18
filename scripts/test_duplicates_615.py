#!/usr/bin/env python3
"""Self-contained check for duplicate detection and cleanup (#615, core/duplicates.py) and the
upload-time guard for a renamed byte-identical copy (core/revisions.py identical_items).

Throwaway DB and storage (_testenv). It builds byte-identical groups and look-alikes, then checks:
the grouping (bytes only, never look-alikes, empty files ignored, shared stored files skipped); the
survivor rule (real date before the first upload first, earliest wins, then more cards, then more
metadata, then oldest upload); what the survivor inherits (cards, tags in both stores, blank text
fields, the sensitive flag, related links, blog placements, card and blog covers); the sensitive
filter (a group with a copy the actor may not see is hidden, counted, never named); dry run writes
nothing and moves no file; non-admin refused; expect_trash mismatch refused (plan_changed) with
nothing written; the real apply (copies in the trash, one batch); a second run finds nothing
(idempotent); one undo restores every table and every file exactly; and the upload guard.

    python scripts/test_duplicates_615.py
"""

import io
import os
import sqlite3
import sys
import time

import _testenv  # noqa: E402
TMP = _testenv.isolate("duplicates-615-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, blog, cards, db, duplicates, ingest, items, paths, revisions, storage, users  # noqa: E402
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code
    return None


def png(color, size=8):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def mk(slug, data, filename=None, stored=None, ts=None):
    sf = stored or f"{slug}.png"
    p = paths.storage_dir() / sf
    if not p.exists():
        p.write_bytes(data)
    storage.thumb_path_for(slug).write_bytes(b"thumb-" + slug.encode())
    db.insert_upload(slug, filename or f"{slug}.png", sf, "tester", media_type="image", file_size=len(data))
    if ts is not None:
        c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
        c.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (ts, slug))
        c.commit()
        c.close()
    return slug


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


TABLES = ("capture_events", "project_items", "post_tags", "blog_tags", "capture_event_relations",
          "blog_entry_items", "blog_entries", "projects", "hobby_settings", "item_revisions",
          "pending_decisions", "trash")


def snapshot():
    out = {}
    for t in TABLES:
        rows = q(f"SELECT * FROM {t}")
        for r in rows:
            r.pop("embedding", None)
            r.pop("rowid", None)
        out[t] = sorted(repr(sorted(r.items())) for r in rows)
    return out


def files():
    return sorted(p.name for p in paths.storage_dir().iterdir() if p.is_file())


db.init_db()
NOW = time.time()
DAY = 86400
RED, BLUE, GREEN, GREY = png((200, 0, 0)), png((0, 0, 200)), png((0, 150, 0)), png((90, 90, 90))

with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("dup_view", "dup-view-pw-1234567", "viewer")
    users.create_user("dup_ed", "dup-ed-pw-12345678", "editor")

with actor.acting_as(actor.ACTOR_MCP):
    # The first upload (the cutoff) is the oldest timestamp: a year ago.
    FIRST = NOW - 365 * DAY
    # Group 1 (red): r_old has a real 2010 date; r_new a date after the first upload; r_nodate none.
    mk("r_nodate", RED, "603 - KtitaRK.jpg", ts=FIRST)
    mk("r_old", RED, "54220475_10215381.jpg", ts=FIRST + 10)
    mk("r_new", RED, "606 - 43FXTtR.jpg", ts=FIRST + 20)
    items.update("r_old", content_date=1262304000)           # 2010-01-01
    items.update("r_new", content_date=NOW - 30 * DAY, description="new copy's note")
    # Group 2 (blue): same date on both; b_two is on 2 cards, b_one on 1 -> b_two survives.
    mk("b_one", BLUE, ts=FIRST + 30)
    mk("b_two", BLUE, ts=FIRST + 40)
    items.update("b_one", content_date=1400000000, display_date_override=None)
    items.update("b_two", content_date=1400000000 + 3600)  # same Mountain day
    # Look-alike (different bytes, same size class): never grouped.
    mk("g_one", GREEN, ts=FIRST + 50)
    # Empty files: never grouped.
    mk("e_one", b"", ts=FIRST + 60)
    mk("e_two", b"", ts=FIRST + 61)
    # Two rows on ONE stored file (grey): skipped, never folded.
    mk("s_one", GREY, stored="shared.png", ts=FIRST + 70)
    mk("s_two", GREY, stored="shared.png", ts=FIRST + 71)

CA = cards.create("Card Alpha", actor=actor.ACTOR_MCP)
CB = cards.create("Card Beta", actor=actor.ACTOR_MCP)
CC = cards.create("Card Gamma", actor=actor.ACTOR_MCP)


def card_id(res):
    return res.data["card"]["id"]


A, B, C = card_id(CA), card_id(CB), card_id(CC)
check("three cards created", all([A, B, C]), (A, B, C))

from core import membership, tags as tags_svc  # noqa: E402
with actor.acting_as(actor.ACTOR_MCP):
    membership.add_files(A, ["r_new"], link_tag=True, merge_free_tags=True, auto_cover=False)
    membership.add_files(B, ["r_nodate", "r_old"], link_tag=False, merge_free_tags=False, auto_cover=False)
    membership.add_files(A, ["b_two"], link_tag=False, merge_free_tags=False, auto_cover=False)
    membership.add_files(B, ["b_two"], link_tag=False, merge_free_tags=False, auto_cover=False)
    membership.add_files(C, ["b_one"], link_tag=False, merge_free_tags=False, auto_cover=False)
    tags_svc.attach("r_nodate", ["wix-import"])
    items.update("r_nodate", tags=["imgur"])
    items.relate("r_new", "g_one")
    cards.update(C, cover_slug="b_one")
    EID = blog.create("Entry", cover_slug="r_new").data["entry"]["id"]
    blog.set_items(EID, [("r_new", "the red one")])
    items.set_sensitive(["b_one"], True)

# ---- plan --------------------------------------------------------------------------------
print("--- plan ---")
with actor.acting_as(actor.ACTOR_MCP):
    P = duplicates.plan()
G = {g["keep"]["slug"]: g for g in P["groups"]}
check("two groups (red, blue); look-alikes and empty files never grouped", sorted(G) == ["b_two", "r_old"], sorted(G))
check("summary counts", P["summary"]["groups"] == 2 and P["summary"]["copies_to_trash"] == 3, P["summary"])
check("shared stored file is skipped with a reason", len(P["skipped"]) == 1
      and set(P["skipped"][0]["slugs"]) == {"s_one", "s_two"} and "share" in P["skipped"][0]["reason"], P["skipped"])
red = G.get("r_old") or {}
check("red: the 2010 copy survives over the post-first-upload and undated copies",
      sorted(t["slug"] for t in red.get("trash", [])) == ["r_new", "r_nodate"], red.get("trash"))
check("red: the reason names the date", "before Constructicon" in red.get("why", ""), red.get("why"))
check("red: inherits Card Alpha, the wix-import tag, the imgur chip and the description",
      [c["title"] for c in red["inherit"]["cards"]] == ["Card Alpha"]
      and "wix-import" in red["inherit"]["tags"] and "imgur" in red["inherit"]["free_text_tags"]
      and "description" in red["inherit"]["fields"], red.get("inherit"))
check("red: post-Constructicon copy flagged, dates differ", red["has_post_constructicon_copy"] and red["dates_differ"])
blue = G.get("b_two") or {}
check("blue: same day, more cards wins", "more cards" in blue.get("why", "") and not blue["dates_differ"], blue.get("why"))
check("blue: survivor inherits the sensitive flag", "sensitive" in blue["inherit"]["flags"], blue.get("inherit"))
check("the plan carries no internal ops", all("_ops" not in g for g in P["groups"]))
check("summary text is one short screen", 2 <= len(duplicates.summary_text(P).splitlines()) <= 6,
      duplicates.summary_text(P))

with actor.acting_as("user:dup_view"):
    PV = duplicates.plan()
check("viewer: the group holding a sensitive copy is hidden and counted, never named",
      [g["keep"]["slug"] for g in PV["groups"]] == ["r_old"] and PV["summary"]["hidden_groups"] == 1
      and "b_one" not in repr(PV), PV["summary"])

# ---- refusals and dry run ----------------------------------------------------------------
print("--- dry run and refusals ---")
before, before_files = snapshot(), files()
check("editor may not apply (admin only)", code_of(duplicates.apply, dry_run=True, actor="user:dup_ed") == "forbidden")
with actor.acting_as(actor.ACTOR_MCP):
    D = duplicates.apply(dry_run=True)
check("dry run reports the copies it would trash", sorted(D.data["trashed"]) == ["b_one", "r_new", "r_nodate"], D.data["trashed"])
check("dry run has changes and a batch id", D.dry_run and D.changes and D.batch_id)
check("dry run wrote nothing and moved no file", snapshot() == before and files() == before_files)
with actor.acting_as(actor.ACTOR_MCP):
    code = code_of(duplicates.apply, dry_run=False, expect_trash=["r_new", "r_nodate"])
check("expect_trash mismatch refuses with plan_changed", code == "plan_changed", code)
check("the refused apply wrote nothing", snapshot() == before and files() == before_files)

# ---- apply ---------------------------------------------------------------------------------
print("--- apply ---")
with actor.acting_as(actor.ACTOR_MCP):
    R = duplicates.apply(dry_run=False, expect_trash=D.data["trashed"])
check("apply trashed the three copies", sorted(R.data["trashed"]) == ["b_one", "r_new", "r_nodate"] and not R.dry_run)
check("the copies are gone; survivors and the rest stay",
      all(db.get_by_slug(s) is None for s in ("b_one", "r_new", "r_nodate"))
      and all(db.get_by_slug(s) for s in ("r_old", "b_two", "g_one", "s_one", "s_two", "e_one")))
tr = q("SELECT slug, batch_id, expires_at, reason FROM trash")
check("trash rows: one batch, 7-day expiry, never a hold",
      len(tr) == 3 and len({t["batch_id"] for t in tr}) == 1 and tr[0]["batch_id"] == R.batch_id
      and all(t["expires_at"] and t["reason"] == "delete" for t in tr), tr)
check("files moved to the trash dir", (items.trash_dir(R.batch_id) / "r_new.png").is_file()
      and not (paths.storage_dir() / "r_new.png").exists())
check("change log: merge rows and the delete share the batch",
      {r["op"] for r in q("SELECT op FROM audit_log WHERE batch_id = ?", R.batch_id)} == {"duplicates_merge", "item_delete"})
keep = db.get_by_slug("r_old")
check("survivor is on every card its copies were on", {p["id"] for p in db.list_projects_for_post("r_old")} == {A, B})
check("survivor has the post_tags tag and the free-text chip",
      "wix-import" in {t["name"] for t in db.list_tags_for_post("r_old")} and "imgur" in (keep.get("tags") or []), keep.get("tags"))
check("survivor took the blank description, kept its own date",
      keep["description"] == "new copy's note" and keep["content_date"] == 1262304000)
check("survivor took the related link", {r["slug"] for r in db.list_related("r_old")} == {"g_one"},
      db.list_related("r_old"))
check("survivor took the blog placement and the blog cover",
      q("SELECT note FROM blog_entry_items WHERE entry_id = ? AND post_slug = 'r_old'", EID) == [{"note": "the red one"}]
      and q("SELECT cover_slug FROM blog_entries WHERE id = ?", EID)[0]["cover_slug"] == "r_old")
check("card cover moved to the blue survivor", db.get_project(C)["cover_slug"] == "b_two")
check("blue survivor inherited the sensitive flag and Card Gamma",
      db.get_by_slug("b_two")["sensitive"] == 1 and C in {p["id"] for p in db.list_projects_for_post("b_two")})

with actor.acting_as(actor.ACTOR_MCP):
    again = duplicates.apply(dry_run=False)
check("a second run finds nothing to do (idempotent)", again.data["trashed"] == [] and not again.changes
      and again.data["summary"]["groups"] == 0)

# ---- undo ------------------------------------------------------------------------------------
print("--- undo ---")
with actor.acting_as(actor.ACTOR_MCP):
    U = cards.undo(R.batch_id)
check("one undo reverses the whole batch", U.ok, U.to_dict())
check("every table is back exactly", snapshot() == before,
      [t for t in TABLES if snapshot()[t] != before[t]])
check("every file is back", files() == before_files)

# ---- upload-time guard -----------------------------------------------------------------------
print("--- upload guard ---")
with actor.acting_as(actor.ACTOR_MCP):
    mk("u_new", GREEN, "totally-different-name.png")
    twins = revisions.identical_items("u_new")
    check("identical_items finds the byte-identical copy under another name",
          [t["slug"] for t in twins] == ["g_one"], [t["slug"] for t in twins])
    did = revisions.queue_replace_question("u_new")
    dec = [d for d in q("SELECT payload FROM pending_decisions WHERE id = ?", did)]
    import json  # noqa: E402
    payload = json.loads(dec[0]["payload"]) if dec else {}
    check("the question offers 'same file: keep one' and suggests it",
          payload.get("suggested") == "same:g_one" and any(o["key"] == "same:g_one" for o in payload.get("options", []))
          and not any(o["key"] == "g_one" for o in payload.get("options", [])), payload)
    check("the question says it is the same file", "same file" in payload.get("question", ""), payload.get("question"))
    res = revisions.resolve_decision(db.get_pending_decision(did), "same:g_one")
    check("answering it trashes the new upload, keeps the old", res.get("trashed") == "u_new"
          and db.get_by_slug("u_new") is None and db.get_by_slug("g_one") is not None, res)
    mk("u_other", png((1, 2, 3)), "unrelated.png")
    check("no question for a file with no twin and no name match", revisions.queue_replace_question("u_other") is None)

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
