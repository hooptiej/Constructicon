#!/usr/bin/env python3
"""Self-contained check for the editable provenance lists (#529).

Builds a throwaway SQLite DB with the real `db.init_db()` (CONSTRUCTICON_DB_PATH points at a
temp file before core is imported, so no real database is ever touched), then exercises
core/provenance_options.py and the validators that read it. Runs anywhere, no server:

    python scripts/test_provenance_options.py

Exits 1 if any check fails.
"""

import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="provopts-")
os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(TMP, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core import card_rules, cards, changes, db, provenance_options as po  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def raises(code, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except card_rules.CardError as e:
        return e.code == code
    return False


def keys(scope, retired=False):
    return [o["key"] for o in po.list_options(scope, include_retired=retired)]


def dump():
    conn = db.get_conn()
    try:
        return [tuple(r) for r in conn.execute("SELECT * FROM provenance_options ORDER BY scope, key")]
    finally:
        conn.close()


db.init_db()
first = dump()

# --- seeding ---------------------------------------------------------------
OLD_CARD = ["created", "found", "collected", "referenced", "client_owned"]
OLD_FILE = ["found", "created", "documented", "result", "reference", "design"]
check("card seed = old list + purchased, in order", keys("card") == OLD_CARD + ["purchased"], keys("card"))
check("file seed = old list + purchased, in order", keys("file") == OLD_FILE + ["purchased"], keys("file"))
check("card seed labels equal the old labels",
      all(po.label("card", k) == card_rules.CARD_PROVENANCE_LABELS[k] for k in OLD_CARD))
check("file list covers db.PROVENANCE_TYPES exactly (+purchased)",
      sorted(keys("file")) == sorted(db.PROVENANCE_TYPES + ["purchased"]))
check("Purchased label in both scopes", po.label("card", "purchased") == "Purchased" == po.label("file", "purchased"))
db.init_db()
check("init_db twice leaves the table identical (idempotent seed)", dump() == first)

# An owner's edits survive a re-init (seed is INSERT OR IGNORE only).
po.rename("card", "found", "Scavenged")
po.retire("card", "client_owned")
db.init_db()
check("re-init keeps a rename", po.label("card", "found") == "Scavenged")
check("re-init keeps a retirement", "client_owned" not in keys("card") and "client_owned" in keys("card", True))
po.rename("card", "found", "Found")
po.unretire("card", "client_owned")

# --- add / rename / retire / unretire / move --------------------------------
added = po.add("card", "gifted_in", "  Gifted   in ")
check("add normalises the label and appends", added["label"] == "Gifted in" and keys("card")[-1] == "gifted_in")
check("duplicate key in scope -> provenance_conflict", raises("provenance_conflict", po.add, "card", "gifted_in", "Again"))
check("same key is fine in the other scope", po.add("file", "gifted_in", "Gifted in")["key"] == "gifted_in")
check("uppercase / spaced key rejected", raises("bad_provenance_key", po.add, "card", "Gifted In", "x"))
check("empty key rejected", raises("bad_provenance_key", po.add, "card", "", "x"))
check("empty label rejected", raises("bad_provenance_label", po.add, "card", "new_one", "   "))
check("empty rename rejected", raises("bad_provenance_label", po.rename, "card", "found", " "))
check("unknown scope rejected", raises("bad_provenance_scope", po.list_options, "nope"))
check("rename unknown key -> not_found", raises("not_found", po.rename, "card", "zzz", "Z"))

po.rename("card", "gifted_in", "Given to me")
check("rename changes the label, never the key", po.get_option("card", "gifted_in")["label"] == "Given to me")
check("labels resolve (active)", po.label("card", "gifted_in") == "Given to me")
check("card_rules label resolves through the table", card_rules.card_provenance_label("gifted_in") == "Given to me")
check("unknown key label falls back to the key", po.label("card", "mystery") == "mystery")

order = keys("card", True)
po.move("card", "gifted_in", "up")
check("move up swaps with the neighbour",
      keys("card", True)[-2:] == ["gifted_in", order[-2]] and len(keys("card", True)) == len(order))
po.move("card", "gifted_in", "down")
check("move down restores", keys("card", True) == order)
check("move at the end is a no-op", po.move("card", "gifted_in", "down") and keys("card", True) == order)
check("bad direction rejected", raises("bad_provenance_move", po.move, "card", "gifted_in", "left"))

# --- validation: active / retired / unknown -----------------------------------
check("active key accepted", card_rules.validate_provenance("purchased") == "purchased")
check("None / '' clear", card_rules.validate_provenance(None) is None and card_rules.validate_provenance("") is None)
check("unknown key -> bad_provenance (card)", raises("bad_provenance", card_rules.validate_provenance, "nope"))
try:
    card_rules.validate_provenance("nope")
except card_rules.CardError as e:
    check("error lists the active keys", "purchased" in e.message and e.details.get("active_keys") == keys("card"))

# Cards: a retired key is refused for new writes but stays valid on the card holding it.
card = db.create_project("Provenance probe card", kind="thing", with_writeup=False)
cid = card["id"] if isinstance(card, dict) else card
cards.set_provenance(cid, "purchased", actor="test")
check("card accepts the new purchased key", db.get_project(cid)["provenance"] == "purchased")
check("card label for purchased resolves", cards.whereabouts_fields(db.get_project(cid))["provenance_label"] == "Purchased")

po.retire("card", "purchased")
check("retired key leaves the active list", "purchased" not in keys("card"))
check("retired key is refused for a NEW write",
      raises("bad_provenance", card_rules.validate_provenance, "purchased"))
check("retired key refused through cards.set_provenance on another card",
      raises("bad_provenance", cards.set_provenance, db.create_project("Provenance probe card 2", kind="thing", with_writeup=False)["id"],
             "purchased", actor="test"))
check("a card holding the retired key still validates it unchanged",
      card_rules.validate_provenance("purchased", current="purchased") == "purchased")
check("...and can be re-saved (credit edit) without losing it",
      cards.set_provenance(cid, "purchased", "Acme", actor="test") and db.get_project(cid)["provenance"] == "purchased")
check("retired key still displays with its label", card_rules.card_provenance_label("purchased") == "Purchased")
pk = po.picker_options("card", "purchased")
check("picker keeps the card's own retired value, marked retired",
      pk[-1]["key"] == "purchased" and pk[-1]["retired"] and all(not o["retired"] for o in pk[:-1]))
check("picker for another card hides it", all(o["key"] != "purchased" for o in po.picker_options("card", None)))
needs = [n for n in cards.provenance_whereabouts_needs() if n["need"] == cards.NEED_MISSING_PROVENANCE]
check("missing_provenance need offers only active options (card 2 has none set)",
      bool(needs) and all(o["key"] != "purchased" for n in needs for o in n["options"]))
po.unretire("card", "purchased")
check("unretire makes it pickable again", card_rules.validate_provenance("purchased") == "purchased")

# Bad key on a card -> same code as before.
check("bad card key -> bad_provenance", raises("bad_provenance", cards.set_provenance, cid, "bogus", actor="test"))

# --- file provenance ---------------------------------------------------------
db.insert_upload("provprobe1", None, None, "tester", media_type="link", external_url="https://example.com/a", content_description="probe")
db.insert_upload("provprobe2", None, None, "tester", media_type="link", external_url="https://example.com/b", content_description="probe")
check("file accepts purchased", db.set_provenance("provprobe1", "purchased")["provenance"] == "purchased")
check("file accepts an old key", db.set_provenance("provprobe2", "design")["provenance"] == "design")
check("bad file key -> bad_provenance", raises("bad_provenance", db.set_provenance, "provprobe1", "bogus"))
check("file clear with None", db.set_provenance("provprobe2", None)["provenance"] is None)
po.retire("file", "purchased")
check("retired file key refused for a new write", raises("bad_provenance", db.set_provenance, "provprobe2", "purchased"))
check("row already holding it can be re-saved", db.set_provenance("provprobe1", "purchased")["provenance"] == "purchased")
check("file label resolves for a retired key", po.label("file", "purchased") == "Purchased")
check("asset-card label: legacy key keeps its card-vocabulary reading",
      card_rules.file_provenance_label("result") == "Created" and card_rules.file_provenance_label("reference") == "Referenced")
check("asset-card label: new key reads the table", card_rules.file_provenance_label("purchased") == "Purchased")
check("asset-card label: NULL inherits the card's", card_rules.file_provenance_label(None, "found") == "Found")
po.unretire("file", "purchased")

# --- change log + undo ---------------------------------------------------------
batch = changes.new_batch_id()
po.rename("file", "design", "Designed", batch_id=batch)
check("rename wrote a change-log row", len(changes.list_changes(batch_id=batch)) == 1)
cards.undo(batch, actor="test")
check("undo restores the old label", po.label("file", "design") == "Design")
batch = changes.new_batch_id()
po.add("file", "undo_me", "Undo me", batch_id=batch)
cards.undo(batch, actor="test")
check("undo of an add removes the option", po.get_option("file", "undo_me") is None)

# --- last-active guard ----------------------------------------------------------
for k in keys("file")[:-1]:
    po.retire("file", k)
check("cannot retire the last active option", raises("provenance_conflict", po.retire, "file", keys("file")[0]))

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "All provenance option checks passed.")
sys.exit(1 if FAILS else 0)
