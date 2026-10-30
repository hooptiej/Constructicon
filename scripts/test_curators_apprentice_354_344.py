#!/usr/bin/env python3
"""Self-contained check for "The Curator's Apprentice": file provenance by gross rule (#354,
core/provenance_rules.py) and the project-correlation nudge (#344, core/correlations.py), both on the pure
rules in core/apprentice_rules.py, plus the provenance-diversity nudge (core/curator_needs.py).

Throwaway DB and storage (_testenv). Text lengths and embeddings are written straight onto the rows (the
pipeline's own columns), so the rules see exactly what they would after OCR, with no real PDFs or model.
It checks: the rule (positive and negative cases, name hints); provenance already set never proposed;
a sensitive file hidden from an editor and counted; confirm (single, bulk, `shown`) writes provenance in
ONE undoable batch; "not this one" remembered and undoable; the correlation score, its parts and the
threshold; exclusions (linked, nested, same family, group kinds); a private card's pairs hidden and
counted; confirm creates a `related` link (one batch) and undo removes it; "not related" remembered;
viewers refused everywhere (core, MCP, web) and their buttons greyed; the Curator needs and the MCP
tool; the diversity nudge; and the measurement script on a read-only copy.

    python scripts/test_curators_apprentice_354_344.py
"""

import array
import math
import os
import re
import sqlite3
import subprocess
import sys
import time

import _testenv  # noqa: E402
TMP = _testenv.isolate("apprentice-354-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import (actor, apprentice_rules as ar, cards, correlations, curation_queue, curator_needs, db,  # noqa: E402
                  ingest, items, membership, paths, provenance_rules, roles, sharing, storage, tags, users)
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:700]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code
    return None


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def raw(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.execute(sql, args)
    c.commit()
    c.close()


TABLES = ("capture_events", "project_relations", "curator_dismissals", "projects", "project_items", "post_tags")


def snapshot():
    out = {}
    for t in TABLES:
        rows = q(f"SELECT * FROM {t}")
        for r in rows:
            r.pop("embedding", None)
            r.pop("created_at", None) if t == "project_relations" else None
        out[t] = sorted(repr(sorted(r.items())) for r in rows)
    return out


def mk(slug, filename, media_type, text="", ts=None):
    sf = f"{slug}.bin"
    (paths.storage_dir() / sf).write_bytes(b"x-" + slug.encode())
    storage.thumb_path_for(slug).write_bytes(b"thumb-" + slug.encode())
    db.insert_upload(slug, filename, sf, "tester", media_type=media_type, file_size=10)
    if text:
        raw("UPDATE capture_events SET extracted_text = ? WHERE slug = ?", text, slug)
    if ts:
        raw("UPDATE capture_events SET content_date = ? WHERE slug = ?", ts, slug)
    return slug


def vec(*vals, dim=8):
    v = list(vals) + [0.0] * (dim - len(vals))
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array.array("f", [x / n for x in v]).tobytes()


# ---- 1. the pure rules ------------------------------------------------------------------------------
print("--- 1. apprentice_rules: the rule, name hints, scoring ---")
check("name_words splits camelCase, punctuation and the extension",
      ar.name_words("UserManual_v2.pdf") == ["user", "manual", "v", "2"], ar.name_words("UserManual_v2.pdf"))
check("manual hints: manual, datasheet, 'data sheet', a User Guide",
      ar.manual_hint("Router Manual.pdf") == "manual" and ar.manual_hint("LM317 datasheet.pdf") == "datasheet"
      and ar.manual_hint("lm317-data-sheet.pdf") == "data sheet" and ar.manual_hint("UserGuide.docx") in ("guide", "user guide"))
check("no hint in an ordinary name, nor 'manually' / 'emanuel'",
      ar.manual_hint("poster final.pdf") is None and ar.manual_hint("manually edited.pdf") is None
      and ar.manual_hint("Emanuel.pdf") is None)
R = ar.FILE_RULES[0]
check("ONE rule in the first cut: text-heavy PDFs and manuals -> reference",
      len(ar.FILE_RULES) == 1 and R.key == "text_heavy_reference" and R.provenance == "reference")
T = ar.TEXT_HEAVY_MIN_CHARS
check("a PDF at the text threshold is text-heavy", R.test({"media_type": "pdf", "text_chars": T}) is not None)
check("a PDF just under it with an ordinary name is not", R.test({"media_type": "pdf", "text_chars": T - 1, "filename": "flyer.pdf"}) is None)
check("a PDF named 'manual' with a little text is proposed",
      (R.test({"media_type": "pdf", "text_chars": ar.MANUAL_HINT_MIN_CHARS, "filename": "Router Manual.pdf"}) or ("", {}))[1].get("test") == "manual_name")
check("a 'manual' PDF with almost no text (a bare scan) is not",
      R.test({"media_type": "pdf", "text_chars": ar.MANUAL_HINT_MIN_CHARS - 1, "filename": "Router Manual.pdf"}) is None)
check("a Word file named 'guide' is proposed, a long Word file without a hint is not",
      R.test({"media_type": "word", "text_chars": 500, "filename": "Setup guide.docx"}) is not None
      and R.test({"media_type": "word", "text_chars": 50000, "filename": "novel.docx"}) is None)
check("an image named 'manual' is never proposed", R.test({"media_type": "image", "text_chars": 99999, "filename": "manual.png"}) is None)
check("time_overlap: same day 1, disjoint 0, undated None",
      ar.time_overlap((0, 0), (0, 0)) == 1.0 and ar.time_overlap((0, 0), (400 * 86400, 400 * 86400)) == 0.0
      and ar.time_overlap((None, None), (0, 0)) is None)
fa = {"vec": [1.0, 0.0], "tags": {1, 2, 3}, "span": (0, 0), "files": {"x", "y"}}
fb = {"vec": [1.0, 0.0], "tags": {1, 2, 3}, "span": (0, 0), "files": {"x", "y"}}
s = ar.score_pair(fa, fb, {1: "Router", 2: "Car", 3: "Desk"})
check("identical cards score 1 with every part full", s["score"] == 1.0 and all(v == 1.0 for v in s["parts"].values()), s)
fc = {"vec": [0.0, 1.0], "tags": set(), "span": (0, 0), "files": set()}
s2 = ar.score_pair(fa, fc)
check("orthogonal text, no tags, same day: time only, under the threshold and not evidence",
      s2["parts"]["text"] == 0 and s2["score"] == ar.W_TIME and s2["score"] < ar.SCORE_THRESHOLD and not ar.has_evidence(s2), s2)
check("weights sum to 1", abs(ar.W_TEXT + ar.W_TAGS + ar.W_TIME + ar.W_SHARED - 1.0) < 1e-9)

# ---- fixtures ---------------------------------------------------------------------------------------
db.init_db()
NOW = time.time()
DAY = 86400
T0 = NOW - 900 * DAY
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("ap_view", "ap-view-password-1", roles.VIEWER)
    users.create_user("ap_ed", "ap-ed-password-123", roles.EDITOR)
ED, VIEW = "user:ap_ed", "user:ap_view"
LONG = "word " * (T // 5 + 400)  # comfortably over the threshold
with actor.acting_as(actor.ACTOR_MCP):
    mk("p_long", "Spec sheet scan.pdf", "pdf", LONG)
    mk("p_long2", "board notes.pdf", "pdf", LONG + "more")
    mk("p_short", "flyer.pdf", "pdf", "a little text " * 20)
    mk("p_manual", "Router Manual.pdf", "pdf", "x" * 400)
    mk("p_bare", "Old manual scan.pdf", "pdf", "")
    mk("w_guide", "Setup guide.docx", "word", "y" * 500)
    mk("w_plain", "novel.docx", "word", LONG)
    mk("p_set", "already set.pdf", "pdf", LONG)
    items.set_provenance(["p_set"], "created")
    mk("i_manual", "manual.png", "image", LONG)
    mk("p_secret", "secret manual.pdf", "pdf", LONG)
    items.set_sensitive(["p_secret"], True)
    mk("p_gone", "redacted.pdf", "pdf", LONG)
    raw("UPDATE capture_events SET redacted = 1 WHERE slug = 'p_gone'")

    # Cards for the correlation pass.
    def card(title, **kw):
        cards.create(title, **kw)
        return next(p for p in db.list_projects() if p["title"] == title)
    X, Y, Z = vec(1, 0.05), vec(0, 1), vec(0.1, 0.1, 1)
    CA, CB, CC = card("Router build"), card("Router case"), card("Garden beds")
    CD, CPRIV = card("Router linked"), card("Router secret")
    CE = card("Router part", parent=CA["id"])
    FAM = card("Lamp family", kind="family")
    CM1, CM2 = card("Lamp one"), card("Lamp two")
    cards.add_to_family(FAM["id"], CM1["id"])
    cards.add_to_family(FAM["id"], CM2["id"])
    for c, slug, v, ts in ((CA, "ca1", X, T0), (CA, "ca2", X, T0 + DAY), (CB, "cb1", vec(1, 0.1), T0 + 10 * DAY),
                           (CC, "cc1", Y, None), (CD, "cd1", X, T0), (CPRIV, "cp1", X, T0), (CE, "ce1", X, T0),
                           (CM1, "cm1", Z, None), (CM2, "cm2", Z, None)):
        mk(slug, f"{slug}.txt", "text", "text", ts=ts)
        db.set_embedding(slug, v)
        membership.add_files(c["id"], [slug], **membership.UI_EFFECTS)
    tags.attach("ca1", ["Routers"])
    tags.attach("cb1", ["Routers"])
    cards.link(CA["slug"], CD["slug"], "related")
    sharing.set_private("card", CPRIV["id"], True)

# ---- 2. provenance proposals ------------------------------------------------------------------------
print("--- 2. provenance proposals: positive and negative cases ---")


def proposed(who=actor.ACTOR_MCP):
    return {p["slug"] for r in provenance_rules.plan(actor=who)["rules"] for p in r["proposals"]}


PA = proposed()
check("proposed: the text-heavy PDFs, the manual PDF, the guide .docx",
      {"p_long", "p_long2", "p_manual", "w_guide", "p_secret"} <= PA, sorted(PA))
check("not proposed: a short flyer, a bare 'manual' scan, a long Word file with no hint, an image, a redacted file",
      not ({"p_short", "p_bare", "w_plain", "i_manual", "p_gone"} & PA), sorted(PA))
check("a file whose provenance is already set is never proposed", "p_set" not in PA)
plan_ed = provenance_rules.plan(actor=ED)
check("an editor doesn't see the sensitive file's proposal, only its count",
      "p_secret" not in proposed(ED) and plan_ed["summary"]["hidden"] == 1, plan_ed["summary"])
one = next(p for r in provenance_rules.plan(actor=actor.ACTOR_MCP)["rules"] for p in r["proposals"] if p["slug"] == "p_manual")
check("a proposal says why in plain words", "Router Manual" not in one["reason"] and "“manual”" in one["reason"], one["reason"])

print("--- 3. confirm: single, bulk, shown; one batch, one undo ---")
before = snapshot()
r = provenance_rules.confirm(["p_long"], actor=ED, dry_run=True)
check("confirm dry run writes nothing", r.dry_run and r.data["applied"] == ["p_long"] and snapshot() == before, r.to_dict())
r = provenance_rules.confirm(["p_long"], actor=ED)
check("single confirm writes Reference", db.get_by_slug("p_long")["provenance"] == "reference" and r.batch_id)
check("... as ONE change-log row in its batch",
      [x["op"] for x in q("SELECT op FROM audit_log WHERE batch_id = ?", r.batch_id)] == [provenance_rules.OP_APPLY])
check("a confirmed file is no longer proposed", "p_long" not in proposed())
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("undo restores it exactly (and it is proposed again)", snapshot() == before and "p_long" in proposed())
r = provenance_rules.confirm(actor=ED)
done = r.data["applied"]
check("bulk confirm writes every proposal the editor can see, not the hidden one",
      set(done) == {"p_long", "p_long2", "p_manual", "w_guide"} and all(db.get_by_slug(s)["provenance"] == "reference" for s in done)
      and db.get_by_slug("p_secret")["provenance"] is None, done)
check("bulk = ONE change-log row", len(q("SELECT id FROM audit_log WHERE batch_id = ?", r.batch_id)) == 1)
check("the already-set file was not touched", db.get_by_slug("p_set")["provenance"] == "created")
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("one undo takes the whole bulk answer back", snapshot() == before, [t for t in TABLES if snapshot()[t] != before[t]])
with actor.acting_as(actor.ACTOR_MCP):
    items.set_provenance(["p_long2"], "design")  # answered by hand meanwhile
r = provenance_rules.confirm(shown=["p_long", "p_long2"], actor=ED)
check("'shown' writes the ones still proposed and names the one answered meanwhile",
      r.data["applied"] == ["p_long"] and [x["slug"] for x in r.data["skipped"]] == ["p_long2"]
      and db.get_by_slug("p_long2")["provenance"] == "design", r.data)
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("naming a file that isn't proposed is refused, nothing written",
      code_of(provenance_rules.confirm, ["p_long2"], actor=ED) == "not_proposed"
      and db.get_by_slug("p_long2")["provenance"] == "design")
check("an unknown file: not_found", code_of(provenance_rules.confirm, ["nope"], actor=ED) == "not_found")
check("an editor naming the sensitive file: forbidden (saying why)",
      code_of(provenance_rules.confirm, ["p_secret"], actor=ED) == "forbidden")
check("an unknown rule: bad_rule", code_of(provenance_rules.confirm, ["p_long"], rule="nope", actor=ED) == "bad_rule")
check("slugs and shown together: bad_request", code_of(provenance_rules.confirm, ["p_long"], shown=["p_manual"], actor=ED) == "bad_request")

print("--- 4. not this one: remembered ---")
before = snapshot()
r = provenance_rules.dismiss(["p_manual"], actor=ED)
check("dismiss stores the (rule, file) key",
      q("SELECT action FROM curator_dismissals WHERE nudge_key = ?", ar.provenance_skip_key(R.key, "p_manual")) == [{"action": "dismiss"}])
check("the dismissed file is never proposed again", "p_manual" not in proposed())
check("dismissing it again: not_proposed", code_of(provenance_rules.dismiss, ["p_manual"], actor=ED) == "not_proposed")
check("confirming it now: not_proposed", code_of(provenance_rules.confirm, ["p_manual"], actor=ED) == "not_proposed")
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("undoing the answer brings the proposal back", snapshot() == before and "p_manual" in proposed())

print("--- 5. viewers are refused ---")
check("a viewer may not confirm", code_of(provenance_rules.confirm, ["p_long"], actor=VIEW) == "forbidden")
check("a viewer may not dismiss", code_of(provenance_rules.dismiss, ["p_long"], actor=VIEW) == "forbidden")
check("a viewer may not link", code_of(correlations.confirm, [[CA["slug"], CB["slug"]]], actor=VIEW) == "forbidden")
check("a viewer may not say not related", code_of(correlations.dismiss, [[CA["slug"], CB["slug"]]], actor=VIEW) == "forbidden")
try:
    provenance_rules.confirm(["p_long"], actor=VIEW)
except AppError as e:
    check("the refusal names the role needed", "editor role" in e.message and e.details.get("need") == "editor", e.message)

# ---- 6. correlations --------------------------------------------------------------------------------
print("--- 6. correlations: scoring, threshold, exclusions ---")


def pairs_of(who=actor.ACTOR_MCP):
    return {(p["a"], p["b"]): p for p in correlations.plan(actor=who)["pairs"]}


def pk(a, b):
    return ar.pair(a["slug"], b["slug"])


CP = pairs_of()
ab = CP.get(pk(CA, CB))
check("Router build / Router case are proposed (alike text, a shared tag, overlapping dates)",
      ab is not None and ab["score"] >= ar.SCORE_THRESHOLD and ab["parts"]["text"] > 0.9
      and ab["evidence"]["shared_tags"] == ["Routers"] and ab["parts"]["time"] > 0, ab)
check("the why says it in words", ab and "read alike" in ab["why"] and "Routers" in ab["why"], ab and ab["why"])
check("an unrelated card is not proposed", pk(CA, CC) not in CP and pk(CB, CC) not in CP)
summ = correlations.plan(actor=actor.ACTOR_MCP)["summary"]
check("already linked (any type) is excluded and counted", pk(CA, CD) not in CP and summ["excluded"].get("already_linked") == 1, summ)
check("a card and its nested part are excluded", pk(CA, CE) not in CP and summ["excluded"].get("nested", 0) >= 1, summ)
check("two members of one family are excluded", pk(CM1, CM2) not in CP and summ["excluded"].get("same_family") == 1, summ)
check("a family card is never considered", not any(FAM["slug"] in k for k in CP))
check("the admin sees the private card's pairs", pk(CA, CPRIV) in CP)
EP = correlations.plan(actor=ED)
check("an editor doesn't see pairs with the private card, only their count",
      not any(CPRIV["slug"] in (p["a"], p["b"]) for p in EP["pairs"]) and EP["summary"]["hidden_pairs"] >= 1, EP["summary"])
check("an editor naming the private card: not_found",
      code_of(correlations.confirm, [[CA["slug"], CPRIV["slug"]]], actor=ED) == "not_found")
check("pairs are sorted best first", [p["score"] for p in EP["pairs"]] == sorted((p["score"] for p in EP["pairs"]), reverse=True))

print("--- 7. Link them: one batch, undo; not related: remembered ---")
before = snapshot()
r = correlations.confirm([[CA["slug"], CB["slug"]]], actor=ED, dry_run=True)
check("confirm dry run writes nothing", r.dry_run and snapshot() == before)
r = correlations.confirm([[CA["slug"], CB["slug"]]], actor=ED)
rel = q("SELECT slug_a, slug_b, type FROM project_relations WHERE type = 'related' AND "
        "((slug_a = ? AND slug_b = ?) OR (slug_a = ? AND slug_b = ?))", CA["slug"], CB["slug"], CB["slug"], CA["slug"])
check("confirm adds a related link (both rows, the card op's way)", len(rel) == 2, rel)
check("its change-log rows are op correlation_link in one batch",
      {x["op"] for x in q("SELECT op FROM audit_log WHERE batch_id = ?", r.batch_id)} == {correlations.OP_LINK})
check("a linked pair is not proposed again", pk(CA, CB) not in pairs_of())
check("confirming it again: not_proposed", code_of(correlations.confirm, [[CA["slug"], CB["slug"]]], actor=ED) == "not_proposed")
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("undo removes the link exactly, and the suggestion is back", snapshot() == before and pk(CA, CB) in pairs_of())
shown = [f"{a}:{b}" for (a, b) in pairs_of(ED)]
r = correlations.confirm(shown=shown, actor=ED)
check("bulk (shown) links every pair on screen in ONE batch",
      len(r.data["linked"]) == len(shown) and len({x["batch_id"] for x in q("SELECT batch_id FROM audit_log WHERE batch_id = ?", r.batch_id)}) == 1,
      r.data)
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("one undo unlinks them all", snapshot() == before, [t for t in TABLES if snapshot()[t] != before[t]])
r = correlations.dismiss([[CB["slug"], CA["slug"]]], actor=ED)
check("'not related' is remembered under the pair's key (either order)",
      q("SELECT action FROM curator_dismissals WHERE nudge_key = ?", ar.correlation_skip_key(CA["slug"], CB["slug"])) == [{"action": "dismiss"}]
      and pk(CA, CB) not in pairs_of() and correlations.plan(actor=actor.ACTOR_MCP)["summary"]["answered_not_related"] == 1)
check("answering it again: not_proposed", code_of(correlations.dismiss, [[CA["slug"], CB["slug"]]], actor=ED) == "not_proposed")
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("undoing 'not related' brings it back", pk(CA, CB) in pairs_of())
check("a malformed pair: bad_request", code_of(correlations.confirm, ["just-one"], actor=ED) == "bad_request")

# ---- 8. the Curator needs ---------------------------------------------------------------------------
print("--- 8. the Curator queue ---")


def queue_group(gid, who=actor.ACTOR_MCP):
    curation_queue._cache.update(fp=None, q=None)
    with actor.acting_as(who):
        qq = curation_queue.cached_queue()
    return [i for g in qq["groups"] if g["id"] == gid for i in g["items"]]


PQ = queue_group("provenance_rules")
check("one provenance need, its files listed", len(PQ) == 1 and PQ[0]["kind"] == "provenance_rule"
      and {m["slug"] for m in PQ[0]["members"]} >= {"p_long", "p_secret"}, PQ)
check("its key is Defer-able", curation_queue.valid_key(PQ[0]["key"]), PQ and PQ[0]["key"])
PQE = queue_group("provenance_rules", ED)
check("the editor's queue drops the sensitive file from the need",
      PQE and "p_secret" not in {m["slug"] for m in PQE[0]["members"]} and "p_long" in {m["slug"] for m in PQE[0]["members"]})
CQ = queue_group("correlations")
check("one correlation need, its pairs listed", len(CQ) == 1 and CQ[0]["kind"] == "correlation"
      and any({p["a"], p["b"]} == {CA["slug"], CB["slug"]} for p in CQ[0]["member_pairs"]), CQ)
check("its key is Defer-able", curation_queue.valid_key(CQ[0]["key"]))
CQE = queue_group("correlations", ED)
check("the editor's queue drops the private card's pairs",
      CQE and not any(CPRIV["slug"] in (p["a"], p["b"]) for p in CQE[0]["member_pairs"]))
with actor.acting_as(ED):
    curation_queue.defer(PQ[0]["key"])
check("Defer moves the need to the Deferred list", not queue_group("provenance_rules"))
with actor.acting_as(ED):
    curation_queue.bring_back(PQ[0]["key"])

print("--- 9. the provenance-diversity nudge ---")
with actor.acting_as(actor.ACTOR_MCP):
    DIV = card("Diverse build")
    for slug in ("dv1", "dv2", "dv3"):
        mk(slug, f"{slug}.txt", "text", "t")
        membership.add_files(DIV["id"], [slug], link_tag=True, auto_cover=False)


def div_nudges(c):
    return [n for n in curator_needs.project_nudges(db.get_project(c["id"]), {}) if n["kind"] == "no_provenance_diversity"]


check("no provenance at all: no diversity nudge (never-classified cards aren't nagged)", div_nudges(DIV) == [])
with actor.acting_as(actor.ACTOR_MCP):
    items.set_provenance(["dv1", "dv2"], "reference")
dn = div_nudges(DIV)
check("all with a provenance say the same thing: the nudge fires, naming it and the gap",
      len(dn) == 1 and "Reference" in dn[0]["summary"] and "1 say nothing yet" in dn[0]["summary"], dn)
with actor.acting_as(actor.ACTOR_MCP):
    items.set_provenance(["dv3"], "created")
check("two distinct values: the nudge is gone", div_nudges(DIV) == [])

# ---- 10. MCP ----------------------------------------------------------------------------------------
print("--- 10. MCP constructicon_proposals ---")
from mcp_server import auth as mcp_auth  # noqa: E402
from mcp_server import server as S  # noqa: E402


def as_user(name, fn, *a, **kw):
    token = mcp_auth.IDENTITY.set({"actor": f"user:{name}", "user_id": None, "username": name})
    try:
        return fn(*a, **kw)
    finally:
        mcp_auth.IDENTITY.reset(token)


def err(out):
    return out.get("error", {}).get("code") if isinstance(out, dict) and out.get("ok") is False else None


check("ACTION_ROLES: list viewer, confirm/dismiss editor",
      S.role_for("constructicon_proposals", "list") == roles.VIEWER
      and S.role_for("constructicon_proposals", "confirm") == roles.EDITOR
      and S.role_for("constructicon_proposals", "dismiss") == roles.EDITOR)
p1 = S.constructicon_proposals(which="provenance", limit=1)
check("provenance list is paged with a summary and the rule", len(p1["items"]) == 1 and p1["more"] and p1["next_cursor"]
      and p1["rules"][0]["key"] == R.key and "summary" in p1, p1)
seen, cur = [x["slug"] for x in p1["items"]], p1["next_cursor"]
while cur:
    pg = S.constructicon_proposals(which="provenance", limit=1, cursor=cur)
    seen += [x["slug"] for x in pg["items"]]
    cur = pg["next_cursor"]
check("paging walks every proposal once", sorted(seen) == sorted(proposed()), (seen, sorted(proposed())))
c1 = S.constructicon_proposals(which="correlation")
check("correlation list", any({x["a"], x["b"]} == {CA["slug"], CB["slug"]} for x in c1["items"]) and "summary" in c1, c1)
_before = snapshot()
out = S.constructicon_proposals(which="provenance", action="confirm", slugs=["p_long"])
check("MCP confirm is a dry run by default", out.get("ok") and out["dry_run"] and out["applied"] == ["p_long"] and snapshot() == _before, out)
out = S.constructicon_proposals(which="correlation", action="confirm", pairs=[[CA["slug"], CB["slug"]]], dry_run=False)
check("MCP correlation confirm with dry_run=false links them", out.get("ok") and out["linked"] == [list(pk(CA, CB))], out)
cards.undo(out["batch_id"], actor=actor.ACTOR_MCP)
out = S.constructicon_proposals(which="provenance", action="dismiss", slugs=["p_manual"], dry_run=False)
check("MCP dismiss records it", out.get("ok") and out["slugs"] == ["p_manual"] and out["batch_id"], out)
cards.undo(out["batch_id"], actor=actor.ACTOR_MCP)
check("a viewer's MCP confirm is refused (forbidden)",
      err(as_user("ap_view", S.constructicon_proposals, which="provenance", action="confirm", slugs=["p_long"])) == "forbidden")
check("a viewer may list", "items" in as_user("ap_view", S.constructicon_proposals, which="correlation"))
check("pairs= does not apply to provenance", err(S.constructicon_proposals(which="provenance", pairs=[["a", "b"]])) == "bad_request")
check("slugs= does not apply to correlation", err(S.constructicon_proposals(which="correlation", slugs=["a"])) == "bad_request")
check("MCP not_proposed comes through in the shared shape",
      err(S.constructicon_proposals(which="provenance", action="confirm", slugs=["p_short"])) == "not_proposed")

# ---- 11. web routes ---------------------------------------------------------------------------------
print("--- 11. web routes, and the viewer's greyed buttons ---")
from starlette.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app)
rr = client.get("/api/curator/provenance-rules")
check("GET /api/curator/provenance-rules", rr.status_code == 200 and rr.json()["rules"][0]["proposals"], rr.text[:300])
rr = client.post("/api/curator/provenance-rules/confirm", data={"shown": ["p_long", "p_manual"], "dry_run": "true"})
check("POST confirm (dry run, shown)", rr.status_code == 200 and sorted(rr.json()["applied"]) == ["p_long", "p_manual"], rr.text[:300])
rr = client.post("/api/curator/provenance-rules/confirm", data={"slugs": ["p_short"]})
check("POST confirm refusal: 409 not_proposed in the shared shape",
      rr.status_code == 409 and rr.json()["error"]["code"] == "not_proposed" and rr.json()["detail"], rr.text[:300])
rr = client.get("/api/curator/correlations")
check("GET /api/curator/correlations", rr.status_code == 200 and rr.json()["pairs"], rr.text[:300])
rr = client.post("/api/curator/correlations/confirm", data={"pairs": [f"{CA['slug']}:{CB['slug']}"], "dry_run": "true"})
check("POST correlation confirm (dry run)", rr.status_code == 200 and rr.json()["linked"], rr.text[:300])
rr = client.post("/api/curator/correlations/dismiss", data={})
check("POST dismiss with nothing: 400 with a message", rr.status_code == 400 and rr.json()["error"]["message"], rr.text[:300])
curation_queue._cache.update(fp=None, q=None)
html = client.get("/api/curator/queue/html?group=provenance_rules&section=open").text
check("the fragment lists the files with Mark / Not this one / Mark all shown",
      'data-cq-action="prov-yes"' in html and 'data-cq-action="prov-not"' in html and 'data-cq-action="prov-all"' in html
      and 'data-cq-slug="p_long"' in html and "Mark all" in html and 'role-locked' not in html, html[:500])
html = client.get("/api/curator/queue/html?group=correlations&section=open").text
check("the fragment lists the pairs with Link them / Not related / Link all shown",
      'data-cq-action="corr-yes"' in html and 'data-cq-action="corr-not"' in html and 'data-cq-action="corr-all"' in html
      and f'data-cq-pair="{CA["slug"]}:{CB["slug"]}"' in html, html[:500])
shell = client.get("/api/curator/queue/html").text
check("the queue shell lists both groups", "Provenance by rule" in shell and "Related projects?" in shell)

BASE = "http://testhost.local"
vc = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
users.limiter.reset()
lr = vc.post("/api/auth/login", json={"username": "ap_view", "password": "ap-view-password-1"}, headers={"Origin": BASE})
check("the viewer signs in", lr.status_code == 200, lr.text[:200])
m = re.search(r'<meta name="csrf-token" content="([^"]+)"', vc.get("/").text)
VH = {"Origin": BASE, "X-CSRF-Token": m.group(1) if m else ""}
curation_queue._cache.update(fp=None, q=None)
vh = vc.get("/api/curator/queue/html?group=provenance_rules&section=open").text
btns = re.findall(r'<button[^>]*data-cq-action="prov-(?:yes|not|all)"[^>]*>', vh)
check("a viewer sees the buttons, every one greyed with the role it needs",
      btns and all("role-locked" in b and "data-locked-message" in b for b in btns), vh[:600])
vr = vc.post("/api/curator/provenance-rules/confirm", data={"slugs": ["p_long"]}, headers=VH)
check("a viewer's web confirm is 403 saying the role", vr.status_code == 403 and "editor" in vr.json()["error"]["message"], vr.text[:300])
vr = vc.post("/api/curator/correlations/confirm", data={"pairs": [f"{CA['slug']}:{CB['slug']}"]}, headers=VH)
check("a viewer's web link is 403", vr.status_code == 403, vr.text[:300])

# ---- 12. the measurement script --------------------------------------------------------------------
print("--- 12. scripts/measure_apprentice_354_344.py on a read-only copy ---")
copy = os.path.join(str(TMP), "copy.db")
src = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
dst = sqlite3.connect(copy)
src.backup(dst)
src.close()
dst.close()
before_bytes = open(copy, "rb").read()
script = os.path.join(HERE, "measure_apprentice_354_344.py")
proc = subprocess.run([sys.executable, script, copy, "--show", "5"], capture_output=True, text=True, timeout=120)
outp = proc.stdout
check("the script exits 0", proc.returncode == 0, proc.stderr[-800:])
check("it prints the provenance counts and a sample",
      "PROPOSED:" in outp and "text_heavy_reference" in outp and "p_long" in outp and "near misses" in outp, outp[:1500])
check("it prints the correlation counts, histogram and a sample",
      "pairs scored" in outp and "score histogram" in outp and CA["slug"] in outp and "already_linked 1" in outp, outp[-1500:])
check("its proposal count matches the app's (admin view)",
      re.search(r"PROPOSED: (\d+)", outp) and int(re.search(r"PROPOSED: (\d+)", outp).group(1)) == len(proposed()), outp[:800])
check("it wrote nothing to the copy", open(copy, "rb").read() == before_bytes)
proc = subprocess.run([sys.executable, script, copy, "--threshold", "0.99", "--text-min-chars", "999999"],
                      capture_output=True, text=True, timeout=120)
check("the flags retune a run", proc.returncode == 0 and "SCORE_THRESHOLD 0.99" in proc.stdout
      and "TEXT_HEAVY_MIN_CHARS=999,999" in proc.stdout, proc.stdout[-600:] + proc.stderr[-400:])
check("a missing database exits 2",
      subprocess.run([sys.executable, script, os.path.join(str(TMP), "nope.db")], capture_output=True).returncode == 2)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all Curator's Apprentice checks passed")
