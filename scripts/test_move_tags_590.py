#!/usr/bin/env python3
"""Self-contained check for #590: moving / copying files between cards swaps the linked tag.

Throwaway SQLite DB + storage (see _testenv). Covers, in BOTH tag stores (post_tags and the
free-text chips):
  - move: files gain the destination's linked tag and lose the source's own linked tag; a tag
    added by hand stays; the cover is untouched; one batch, one undo restores everything;
  - the exception: a file still on another card with the same linked tag keeps it;
  - move to a card with no linked tag, and a dry run (shows the tag rows, writes nothing);
  - copy: gains the destination tag, keeps the source tag; one undo;
  - merge: files take the kept card's tag and shed the absorbed card's; one undo;
  - split: left alone (the new card has no linked tag), tags unchanged;
  - the MCP move tool does the same as cards.move_files.

    python scripts/test_move_tags_590.py

Exits 1 if any check fails.
"""

import io
import os
import sys

import _testenv  # noqa: E402
TMP = _testenv.isolate("move-tags-590-")
os.environ.setdefault("CAPTION_DISABLED", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows)
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, cards, db, membership, paths, storage  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def mk(slug):
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, "PNG")
    (paths.storage_dir() / sf).write_bytes(buf.getvalue())
    storage.thumb_path_for(slug).write_bytes(b"thumb-" + slug.encode())
    db.insert_upload(slug, f"{slug}.png", sf, "tester", media_type="image")
    return slug


def card(title, with_tag=True, tag_id=None):
    if with_tag and tag_id is None:
        tag_id = db._get_or_create_tag(title)["id"]
    return db._create_project(title, tag_id=tag_id if with_tag else None, with_writeup=False)


def snap(slugs, card_ids=()):
    """Both tag stores for each file, plus membership and covers: what the ops can touch."""
    out = {"post_tags": {s: sorted(t["name"] for t in db.list_tags_for_post(s)) for s in slugs},
           "chips": {s: sorted(db.get_by_slug(s)["tags"]) for s in slugs}}
    for cid in card_ids:
        p = db.get_project(cid)
        out[f"card{cid}"] = ([r["post_slug"] for r in db.list_project_item_rows(cid)], p["cover_slug"])
    return out


db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()
from mcp_server import server  # noqa: E402

F1, F2, F3 = mk("f1"), mk("f2"), mk("f3")


def fresh(a_title, b_title):
    A, B = card(a_title), card(b_title)
    membership.add_files(A["id"], [F1, F2, F3], **membership.UI_EFFECTS)
    return A, B


# ---- move -------------------------------------------------------------------------------
A, B = fresh("Pinball Desk", "Desk Build")
from core import tags as tags_svc  # noqa: E402
tags_svc.set_item_tags(F1, sorted(db.get_by_slug(F1)["tags"] + ["hand-made"]))
before = snap([F1, F2, F3], [A["id"], B["id"]])
check("setup: all three files carry A's tag in both stores; F1 also a hand tag",
      all("Pinball Desk" in before["post_tags"][s] and "Pinball Desk" in before["chips"][s] for s in (F1, F2, F3))
      and "hand-made" in before["chips"][F1] and "hand-made" in before["post_tags"][F1])
res = cards.move_files([F1, F2], A["id"], B["id"])
after = snap([F1, F2, F3], [A["id"], B["id"]])
for s in (F1, F2):
    check(f"move: {s} has B's tag and not A's in post_tags", "Desk Build" in after["post_tags"][s]
          and "Pinball Desk" not in after["post_tags"][s], after["post_tags"][s])
    check(f"move: {s} has B's chip and not A's", "Desk Build" in after["chips"][s]
          and "Pinball Desk" not in after["chips"][s], after["chips"][s])
check("move: the hand-added tag survives in both stores",
      "hand-made" in after["post_tags"][F1] and "hand-made" in after["chips"][F1])
check("move: the file left behind (F3) is untouched",
      after["post_tags"][F3] == before["post_tags"][F3] and after["chips"][F3] == before["chips"][F3])
check("move: membership moved and the cover is untouched",
      after[f"card{B['id']}"][0] == [F1, F2] and after[f"card{A['id']}"] == ([F3], before[f"card{A['id']}"][1]))
check("move: the answer says what the tag swap did",
      res.data["tags_added"] == 2 and res.data["tags_removed"] == 2
      and {r["field"] for r in res.changes} >= {"file", "tag"}, res.data)
ops = db.get_conn().execute("SELECT op, COUNT(*) FROM audit_log WHERE batch_id = ? AND op IS NOT NULL GROUP BY op",
                            (res.batch_id,)).fetchall()
check("move: every change-log row of the batch is a move_files row", [tuple(r)[0] for r in ops] == ["move_files"], ops)
cards.undo(res.batch_id)
check("move: ONE undo restores membership, covers and both tag stores exactly",
      snap([F1, F2, F3], [A["id"], B["id"]]) == before)

# ---- dry run ----------------------------------------------------------------------------
res = cards.move_files([F1], A["id"], B["id"], dry_run=True)
check("move dry run: shows the tag rows, writes nothing",
      res.dry_run and any(r["field"] == "tag" for r in res.changes) and snap([F1, F2, F3], [A["id"], B["id"]]) == before)

# ---- the exception: still on another card with the same tag ----------------------------------
C = card("Pinball Desk copy", tag_id=A["tag_id"])
membership.write(C["id"], [F1], [], "test_seed", None)
res = cards.move_files([F1, F2], A["id"], B["id"])
s = snap([F1, F2])
check("exception: F1 is still on C (same linked tag), so it keeps A's tag in both stores",
      "Pinball Desk" in s["post_tags"][F1] and "Pinball Desk" in s["chips"][F1]
      and "Desk Build" in s["post_tags"][F1] and "Desk Build" in s["chips"][F1])
check("exception: F2 (nowhere else) loses it",
      "Pinball Desk" not in s["post_tags"][F2] and "Pinball Desk" not in s["chips"][F2])
cards.undo(res.batch_id)
check("exception: undo restores", snap([F1, F2, F3], [A["id"], B["id"]]) == before)
membership.write(C["id"], [], [F1], "test_seed", None)

# ---- move to a card with no linked tag -------------------------------------------------------
N = card("No Tag Card", with_tag=False)
res = cards.move_files([F3], A["id"], N["id"])
s = snap([F3])
check("move to a tagless card: the source tag goes, nothing is gained",
      s["post_tags"][F3] == [] and s["chips"][F3] == [], s)
cards.undo(res.batch_id)
check("move to a tagless card: undo restores", snap([F1, F2, F3], [A["id"], B["id"]]) == before)

# ---- copy -------------------------------------------------------------------------------
res = cards.copy_files([F1], A["id"], B["id"])
s = snap([F1])
check("copy: F1 keeps A's tag and gains B's, in both stores",
      s["post_tags"][F1] == sorted(["Pinball Desk", "Desk Build", "hand-made"])
      and s["chips"][F1] == sorted(["Pinball Desk", "Desk Build", "hand-made"]), s)
cards.undo(res.batch_id)
check("copy: one undo restores", snap([F1, F2, F3], [A["id"], B["id"]]) == before)

# ---- merge ------------------------------------------------------------------------------
res = cards.merge_cards(B["id"], [A["id"]])
s = snap([F1, F2, F3])
check("merge: files take the kept card's tag and shed the absorbed card's (both stores)",
      all("Desk Build" in s["post_tags"][f] and "Pinball Desk" not in s["post_tags"][f]
          and "Desk Build" in s["chips"][f] and "Pinball Desk" not in s["chips"][f] for f in (F1, F2, F3)), s)
check("merge: the hand tag stays", "hand-made" in s["post_tags"][F1] and "hand-made" in s["chips"][F1])
cards.undo(res.batch_id)
check("merge: one undo restores the absorbed card, its files and their tags",
      db.get_project(A["id"]) is not None and snap([F1, F2, F3], [A["id"], B["id"]]) == before)

# ---- split: left alone ---------------------------------------------------------------------
res = cards.split_card(A["id"], [{"title": "Split Part", "file_slugs": [F3]}])
check("split: tags unchanged (the new card has no linked tag)", snap([F1, F2, F3])["post_tags"] == before["post_tags"]
      and snap([F1, F2, F3])["chips"] == before["chips"])
cards.undo(res.batch_id)

# ---- the MCP tool --------------------------------------------------------------------------
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_move_files([F1, F2], A["slug"], B["slug"])
s = snap([F1, F2])
check("MCP move_files: same swap", out.get("ok") and all("Desk Build" in s["post_tags"][f]
      and "Pinball Desk" not in s["post_tags"][f] and "Pinball Desk" not in s["chips"][f] for f in (F1, F2)), out)
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_undo(out["batch_id"])
check("MCP move_files: constructicon_undo restores", snap([F1, F2, F3], [A["id"], B["id"]]) == before)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
