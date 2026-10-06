#!/usr/bin/env python3
"""Self-contained check for revision tracking (#477).

Builds a throwaway SQLite DB with the real `db.init_db()` (CONSTRUCTICON_DB_PATH points at a temp
file before core is imported, so no real database is touched) and exercises core/revisions.py:
chain building and current detection, cycle / self / redacted / conflict rejection, remove-from-chain
re-linking, filename stem normalization, the upload-time "does this replace ...?" decision (created,
resolved, never auto-linked), browse-listing filters, delete cleanup and undo. No server needed:

    python scripts/test_revisions.py

Exits 1 if any check fails.
"""

import os
import sys
import tempfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("revisions-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine (e.g. Windows): core.decisions imports every object type, svg included
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import card_rules, cards, changes, db, decisions, revisions  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths

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


def pairs():
    return db.revision_pairs()


def mk(slug, filename, media_type="document", **kw):
    db.insert_upload(slug, filename, "stored-" + slug, "tester", media_type=media_type, **kw)
    return slug


db.init_db()
db.init_db()  # idempotent
conn = db.get_conn()
check("item_revisions table exists after init_db",
      conn.execute("SELECT name FROM sqlite_master WHERE name = 'item_revisions'").fetchone() is not None)
conn.close()

# --- stem normalization ---------------------------------------------------------
N = revisions.normalize_stem
check("stem: extension dropped", N("Panel Layout.pdf") == "panel layout")
check("stem: _revB", N("Panel_Layout_revB.pdf") == "panel layout", N("Panel_Layout_revB.pdf"))
check("stem: -rev2 / rev 3", N("floor-plan-rev2.dwg") == "floor plan" and N("floor plan rev 3.dwg") == "floor plan")
check("stem: -v2 and v2.1", N("manual-v2.pdf") == "manual" and N("manual_v2.1.pdf") == "manual")
check("stem: version 3", N("manual version 3.pdf") == "manual", N("manual version 3.pdf"))
check("stem: ' (1)'", N("Site Plan (1).pdf") == "site plan")
check("stem: _2026-10-01 and 20261001", N("riser_2026-10-01.pdf") == "riser" and N("riser_20261001.pdf") == "riser")
check("stem: stacked suffixes", N("as_built_revC_2026-10-01 (2).pdf") == "as built", N("as_built_revC_2026-10-01 (2).pdf"))
check("stem: final / copy", N("riser diagram FINAL.pdf") == "riser diagram" and N("riser diagram - Copy.pdf") == "riser diagram")
check("stem: case and separators fold", N("AS-BUILT.PDF") == N("as_built.pdf") == "as built")
check("stem: a name merely ending in v is untouched", N("HDTV.pdf") == "hdtv", N("HDTV.pdf"))
check("stem: 'review' is not 'rev' + letter", N("design_review.pdf") == "design review", N("design_review.pdf"))
check("stem: too generic -> ''", N("IMG_2026-10-01.jpg") == "" and N("Screenshot.png") == "" and N("a.pdf") == "")
check("stem: camera counters stay distinct", N("IMG_0001.jpg") != N("IMG_0002.jpg") and N("IMG_0001.jpg") != "")
check("stem: empty / None", N("") == "" and N(None) == "")

# --- chain building -------------------------------------------------------------
A, B, C, D = (mk(s, f"{s}.pdf") for s in ("a", "b", "c", "d"))
r = revisions.mark_superseded(A, B)
check("A -> B recorded", pairs() == {A: B} and r["chain"] == [A, B], pairs())
r = revisions.mark_superseded(B, C)
check("B -> C extends the chain", pairs() == {A: B, B: C} and r["chain"] == [A, B, C])
check("chain_slugs from any member", all(revisions.chain_slugs(s) == [A, B, C] for s in (A, B, C)))
check("current = item with no successor", all(revisions.current_slug(s) == C for s in (A, B, C)))
check("an unrelated item is its own chain", revisions.chain_slugs(D) == [D] and revisions.current_slug(D) == D)
info = revisions.info_map()
check("info_map rev numbers", (info[A]["rev"], info[B]["rev"], info[C]["rev"]) == (1, 2, 3) and info[A]["of"] == 3)
check("info_map superseded_by points at current",
      info[A]["superseded_by"] == C and info[B]["superseded_by"] == C and info[C]["superseded_by"] is None)
check("info_map omits items in no chain", D not in info)
items = revisions.decorate([{"slug": A}, {"slug": C}, {"slug": D}])
check("decorate adds superseded_by / rev",
      (items[0]["superseded_by"], items[0]["rev"]) == (C, 1) and items[1]["superseded_by"] is None
      and items[1]["rev"] == 3 and items[2]["rev"] is None)
view = revisions.revision_view(B)
check("revision_view for a middle item", view["superseded"] and view["current"]["slug"] == C and view["rev"] == 2
      and view["of"] == 3 and [c["slug"] for c in view["chain"]] == [A, B, C])
check("revision_view for an unchained item", revisions.revision_view(D)["in_chain"] is False)

# --- rejections -----------------------------------------------------------------
check("self-link rejected", raises("bad_revision", revisions.mark_superseded, D, D))
check("unknown item rejected", raises("not_found", revisions.mark_superseded, D, "nope"))
check("cycle rejected (current superseded by the oldest)", raises("revision_cycle", revisions.mark_superseded, C, A))
check("cycle rejected (2-chain)", (lambda: (revisions.mark_superseded(D, mk("e", "e.pdf")),
                                            raises("revision_cycle", revisions.mark_superseded, "e", D))[1])())
revisions.remove_from_chain("e")  # tidy: D, e independent again
check("second successor rejected", raises("revision_conflict", revisions.mark_superseded, A, D))
check("second predecessor rejected", raises("revision_conflict", revisions.mark_superseded, D, B))
mk("r", "r.pdf")
db._mark_redacted("r")
check("redacted item rejected (as new)", raises("bad_revision", revisions.mark_superseded, D, "r"))
check("redacted item rejected (as old)", raises("bad_revision", revisions.mark_superseded, "r", D))
check("failed validations wrote nothing", pairs() == {A: B, B: C}, pairs())

# --- dry run --------------------------------------------------------------------
before = pairs()
res = revisions.mark_superseded(C, D, dry_run=True)
check("dry run validates and returns the chain but writes nothing",
      res["dry_run"] and res["chain"] and pairs() == before and not revisions.chain_slugs(D)[1:])

# --- listings -------------------------------------------------------------------
unfiled = {r["slug"] for r in db.list_unfiled_items()}
check("unfiled lists the current revision only", C in unfiled and A not in unfiled and B not in unfiled, unfiled)
check("unfiled include_superseded=True lists all", {A, B, C} <= {r["slug"] for r in db.list_unfiled_items(include_superseded=True)})
check("count_unfiled follows the same rule",
      db.count_unfiled_items() == len(db.list_unfiled_items()) and
      db.count_unfiled_items(include_superseded=True) == len(db.list_unfiled_items(include_superseded=True)))
by_type = {r["slug"] for rows in db.list_recent_items_by_type().values() for r in rows}
check("recent-by-type lists current only", C in by_type and A not in by_type, by_type)
check("search (default) still finds old revisions", A in {r["slug"] for r in db.search(query="a.pdf")})
check("search(include_superseded=False) hides them", A not in {r["slug"] for r in db.search(query="a.pdf", include_superseded=False)})
check("superseded_slugs", db.superseded_slugs() == {A, B})

# --- remove from chain re-links -------------------------------------------------
res = revisions.remove_from_chain(B)
check("remove middle: A -> C", pairs() == {A: C} and res["removed"] and res["chain"] == [A, C], pairs())
check("remove from no chain is a no-op", revisions.remove_from_chain(D)["removed"] is False)
revisions.mark_superseded(C, D)  # A -> C -> D
revisions.remove_from_chain(D)
check("remove current: previous becomes current", pairs() == {A: C} and revisions.current_slug(A) == C, pairs())
revisions.remove_from_chain(A)
check("remove the last pair: nothing left", pairs() == {})
revisions.mark_superseded(A, B)
revisions.mark_superseded(B, C)
revisions.remove_from_chain(A)
check("remove oldest: B -> C stands", pairs() == {B: C}, pairs())
revisions.mark_superseded(A, B)  # A -> B -> C again

# --- undo -----------------------------------------------------------------------
snapshot = pairs()
res = revisions.remove_from_chain(B)
check("remove B again re-links", pairs() == {A: C})
undone = cards.undo(res["batch_id"], actor=changes.ACTOR_UI)
check("undo of remove-from-chain restores A -> B -> C", pairs() == snapshot and undone.ok, pairs())
res = revisions.mark_superseded(C, D)
check("mark adds C -> D", pairs() == {**snapshot, C: D})
cards.undo(res["batch_id"], actor=changes.ACTOR_UI)
check("undo of mark removes the link", pairs() == snapshot, pairs())

# --- delete cleans the chain ----------------------------------------------------
x = mk("x", "x.pdf")
revisions.mark_superseded(C, x)  # A -> B -> C -> x
db._delete_upload(B)
check("deleting a middle item closes the gap", pairs() == {A: C, C: x}, pairs())
db._delete_upload(x)
check("deleting the current item makes the previous current", pairs() == {A: C} and revisions.current_slug(A) == C, pairs())
db._delete_upload(A)
check("deleting down to one item leaves no chain", pairs() == {}, pairs())

# --- upload-time question -------------------------------------------------------
old1 = mk("old1", "Riser Diagram_revA.pdf")
old2 = mk("old2", "Riser Diagram_revB.pdf")
other = mk("oth", "Riser Diagram_revB.png", media_type="image")   # same stem, other type: not a candidate
unrelated = mk("unr", "Floor Plan.pdf")
revisions.mark_superseded(old1, old2)                              # old1 superseded: only old2 is current
new = mk("new1", "Riser Diagram_revC_2026-10-03.pdf")
check("candidates: current, same type, same stem only", [c["slug"] for c in revisions.candidates_for(new)] == [old2],
      [c["slug"] for c in revisions.candidates_for(new)])
check("generic names ask nothing", revisions.queue_replace_question(mk("g1", "IMG_2026-10-03.pdf")) is None)
check("no match asks nothing", revisions.queue_replace_question(unrelated) is None)
did = revisions.queue_replace_question(new)
check("a matching upload queues an item_supersedes decision", did is not None)
check("...and never links by itself", pairs() == {old1: old2}, pairs())
dec = db.get_pending_decision(did)
check("decision shape", dec["kind"] == "item_supersedes" and dec["post_slug"] == new
      and [o["key"] for o in dec["payload"]["options"]] == [old2, "none"] and dec["payload"]["suggested"] == old2)
check("asking twice doesn't re-ask", revisions.queue_replace_question(new) is None)
listed = [e for e in decisions.list_open() if e["id"] == did]
check("decisions.list_open carries it (not stale)", len(listed) == 1 and listed[0]["question"].startswith("Does"), listed)
try:
    decisions.resolve(did, choice="bogus")
    bad = False
except decisions.InvalidChoice:
    bad = True
check("a choice outside the options is refused (decision stays open)",
      bad and db.get_pending_decision(did)["resolved_at"] is None)
res = decisions.resolve(did, choice=old2)
check("resolving with a candidate creates the link", pairs() == {old1: old2, old2: new} and res["applied"] == [old2], pairs())
check("...and resolves the decision", db.get_pending_decision(did)["resolved_at"] is not None)
undone = cards.undo(res["batch_id"], actor=changes.ACTOR_UI)
check("undo reverses the link AND reopens the decision",
      pairs() == {old1: old2} and db.get_pending_decision(did)["resolved_at"] is None, pairs())
res = decisions.resolve(did, choice="none")
check("'No, it's separate' links nothing and resolves", pairs() == {old1: old2} and res["applied"] == []
      and db.get_pending_decision(did)["resolved_at"] is not None)
check("a resolved question is not re-asked", revisions.queue_replace_question(new) is None)

# stale: the owner links by hand before answering
n2 = mk("new2", "Riser Diagram_revD.pdf")
did2 = revisions.queue_replace_question(n2)
check("second upload asks too (candidate is the current rev)", did2 is not None)
revisions.mark_superseded(old2, n2)
# #551 item 3: a read only leaves the stale question out; the explicit sweep resolves it.
check("hand-linking makes the question stale: reads leave it out without resolving it",
      not [e for e in decisions.list_open() if e["id"] == did2] and db.get_pending_decision(did2)["resolved_at"] is None)
swept = decisions.sweep_stale()
check("the sweep resolves it away (stale: no candidates left)",
      [r["id"] for r in swept.data["resolved"]] == [did2] and db.get_pending_decision(did2)["resolved_at"] is not None)

# candidate superseded meanwhile: resolving an old option is rejected, decision stays open
n3 = mk("new3", "Floor Plan_rev2.pdf")
did3 = revisions.queue_replace_question(n3)
check("another match queues", did3 is not None)
revisions.mark_superseded(unrelated, mk("fp3", "elsewhere.pdf"))
try:
    decisions.resolve(did3, choice=unrelated)
    code = None
except card_rules.CardError as e:
    code = e.code
check("a candidate that got superseded meanwhile is a rule violation; decision stays open",
      code == "revision_conflict" and db.get_pending_decision(did3)["resolved_at"] is None, code)

print()
print("FAILED: %d" % len(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
