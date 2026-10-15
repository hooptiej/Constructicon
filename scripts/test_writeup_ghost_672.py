#!/usr/bin/env python3
"""#672: giving a card a different write-up takes its old BLANK write-up off the card and trashes it.

Throwaway DB (scripts/_testenv.py), the real core ops, the MCP tool and the web route. No server:

    python scripts/test_writeup_ghost_672.py

Checks: cards.update / MCP constructicon_set_project_writeup / POST /api/projects/{id} each retire
the blank write-up in the same batch; undo restores it (and the old pointer); a previous write-up
with text is left alone; a blank one another card still holds only leaves this card; dry_run writes
nothing; timeline.resolve_project_span ignores an empty write-up document wherever it sits.
"""
import os
import sys
import types

import _testenv  # noqa: E402
TMP = _testenv.isolate("writeupghost672-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from core import actor, cards, db, timeline  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
N = [0]


def dated_item(ts):
    N[0] += 1
    slug = f"g672-img-{N[0]}"
    db.insert_upload(slug, None, None, "test", media_type="image")
    conn = db.get_conn()
    conn.execute("UPDATE capture_events SET timestamp = ?, content_date = ? WHERE slug = ?", (ts, ts, slug))
    conn.commit()
    conn.close()
    return slug


def document(body):
    N[0] += 1
    slug = f"g672-doc-{N[0]}"
    db.insert_content(slug, "test", "document", content_description=f"Doc {N[0]}", type_metadata={"body": body})
    return slug


def on_card(card_id, slug):
    return any(r["post_slug"] == slug for r in db.list_project_item_rows(card_id))


def in_trash(slug):
    """Deleted through items.delete (the trash op): a logged, undoable item_delete row naming it.
    A write-up document has no file, so it leaves no `trash` table row (that holds files)."""
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT affected_slugs FROM audit_log WHERE op = 'item_delete' AND undone_by IS NULL"
                            ).fetchall()
        return any(slug in (r[0] or "") for r in rows)
    finally:
        conn.close()


def add_to_card(card_id, slug):
    conn = db.get_conn()
    conn.execute("INSERT INTO project_items (project_id, post_slug, sort_order) VALUES (?, ?, 0)", (card_id, slug))
    conn.commit()
    conn.close()


def new_card(title):
    """A card made the normal way (with its blank write-up) holding two dated files."""
    card = cards.create(title).data["card"]
    a, b = dated_item(1_000_000.0), dated_item(2_000_000.0)
    conn = db.get_conn()
    for s in (a, b):
        conn.execute("INSERT INTO project_items (project_id, post_slug, sort_order) VALUES (?, ?, 0)", (card["id"], s))
    conn.commit()
    conn.close()
    return db.get_project(card["id"])


def span(card_id):
    card = db.get_project(card_id)
    return timeline.resolve_project_span(card, db.list_project_items(card_id))


with actor.acting_as(actor.ACTOR_UI):
    # --- 1. the plain core op: blank old write-up -> off the card, in the trash, one batch ---
    card = new_card("Ghost Card")
    blank = card["writeup_slug"]
    check("a new card has a blank write-up", bool(blank) and db.blank_document_body(blank))
    real = document("# The real story\n\nIt happened.")
    res = cards.update(card["id"], writeup_slug=real)
    fresh = db.get_project(card["id"])
    check("card now points at the new write-up", fresh["writeup_slug"] == real)
    check("old blank write-up is off the card", not on_card(card["id"], blank))
    check("old blank write-up row is gone", db.get_by_slug(blank) is None)
    check("old blank write-up is in the trash", in_trash(blank))
    conn = db.get_conn()
    ops = {r[0] for r in conn.execute("SELECT op FROM audit_log WHERE batch_id = ? AND op IS NOT NULL", (res.batch_id,))}
    conn.close()
    check("pointer change and trashing share one batch", {"update_card"} <= ops and len(ops) >= 2, ops)
    check("span comes from the real files only", span(card["id"]) == (1_000_000.0, 2_000_000.0), span(card["id"]))

    # --- 2. undo brings it back, pointer included ---
    u = cards.undo(res.batch_id)
    back = db.get_project(card["id"])
    check("undo restores the old pointer", back["writeup_slug"] == blank, back["writeup_slug"])
    check("undo restores the blank write-up row", db.get_by_slug(blank) is not None)
    check("undo puts the blank write-up back on the card", on_card(card["id"], blank))

    # --- 3. a previous write-up WITH text is never touched ---
    card2 = new_card("Story Card")
    told = document("Some real words the owner wrote.")
    add_to_card(card2["id"], told)
    cards.update(card2["id"], writeup_slug=told)  # its own blank goes; `told` is now current
    other = document("A second draft.")
    cards.update(card2["id"], writeup_slug=other)
    check("non-empty previous write-up still exists", db.get_by_slug(told) is not None)
    check("non-empty previous write-up stays on the card", on_card(card2["id"], told))
    check("non-empty previous write-up is not trashed", not in_trash(told))

    # --- 4. dry_run writes nothing ---
    card3 = new_card("Dry Card")
    blank3 = card3["writeup_slug"]
    cards.update(card3["id"], writeup_slug=document("x"), dry_run=True)
    check("dry_run leaves the pointer", db.get_project(card3["id"])["writeup_slug"] == blank3)
    check("dry_run leaves the blank write-up", db.get_by_slug(blank3) is not None and on_card(card3["id"], blank3))

    # --- 5. a blank write-up another card also holds only leaves THIS card ---
    card4 = new_card("Shared Blank")
    blank4 = card4["writeup_slug"]
    holder = new_card("Holder")
    conn = db.get_conn()
    conn.execute("INSERT INTO project_items (project_id, post_slug, sort_order) VALUES (?, ?, 0)", (holder["id"], blank4))
    conn.commit()
    conn.close()
    cards.update(card4["id"], writeup_slug=document("y"))
    check("shared blank: off this card", not on_card(card4["id"], blank4))
    check("shared blank: still exists and still on the other card",
          db.get_by_slug(blank4) is not None and on_card(holder["id"], blank4))

    # --- 6. clearing or re-setting the same write-up leaves it alone ---
    card5 = new_card("Same Card")
    blank5 = card5["writeup_slug"]
    cards.update(card5["id"], writeup_slug=blank5)
    check("re-setting the same write-up keeps it", db.get_by_slug(blank5) is not None and on_card(card5["id"], blank5))

    # --- 7. the span skips an empty write-up document wherever it sits (belt and braces) ---
    card6 = new_card("Orphan Card")
    orphan = card6["writeup_slug"]
    conn = db.get_conn()  # an orphan from some other path: the card points elsewhere, the blank stays
    conn.execute("UPDATE projects SET writeup_slug = NULL WHERE id = ?", (card6["id"],))
    conn.execute("UPDATE capture_events SET timestamp = 9_000_000_000 WHERE slug = ?", (orphan,))
    conn.commit()
    conn.close()
    check("span ignores an orphaned blank write-up", span(card6["id"]) == (1_000_000.0, 2_000_000.0), span(card6["id"]))
    check("is_blank_writeup_document: dict / JSON / text / other types",
          timeline.is_blank_writeup_document({"media_type": "document", "type_metadata": {"body": "  \n"}})
          and timeline.is_blank_writeup_document({"media_type": "document", "type_metadata": '{"body": ""}'})
          and timeline.is_blank_writeup_document({"media_type": "document", "type_metadata": None})
          and not timeline.is_blank_writeup_document({"media_type": "document", "type_metadata": {"body": "hi"}})
          and not timeline.is_blank_writeup_document({"media_type": "image", "type_metadata": {}})
          and not timeline.is_blank_writeup_document({"media_type": "document", "type_metadata": "{not json"}))

# --- 8. the MCP tool ---
from mcp_server import server as mcp  # noqa: E402
with actor.acting_as(actor.ACTOR_UI):
    card7 = new_card("MCP Card")
blank7 = card7["writeup_slug"]
out = mcp.constructicon_set_project_writeup(card7["id"], document("Written through the MCP."))
check("MCP: tool succeeds", isinstance(out, dict) and out.get("batch_id"), out)
check("MCP: old blank write-up trashed and off the card", db.get_by_slug(blank7) is None and in_trash(blank7)
      and not on_card(card7["id"], blank7))
with actor.acting_as(actor.ACTOR_UI):
    cards.undo(out["batch_id"])
check("MCP: one undo restores it", db.get_by_slug(blank7) is not None and on_card(card7["id"], blank7)
      and db.get_project(card7["id"])["writeup_slug"] == blank7)

# --- 9. the web route ---
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app, raise_server_exceptions=False)
with actor.acting_as(actor.ACTOR_UI):
    card8 = new_card("Web Card")
blank8 = card8["writeup_slug"]
with client:
    r = client.post(f"/api/projects/{card8['id']}", data={"writeup_slug": document("Written in the page.")})
check("web: Save succeeds", r.status_code == 200, (r.status_code, r.text[:300]))
check("web: old blank write-up trashed and off the card", db.get_by_slug(blank8) is None and in_trash(blank8))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
