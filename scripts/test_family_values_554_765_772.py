#!/usr/bin/env python3
"""Family values (#554 rules, #765, #772): group cards are graded by their members, and their dates
roll up from their members. Throwaway DB, no server, no network.

    python scripts/test_family_values_554_765_772.py

Owner decisions 2026-10-18 (#554): a family has no whereabouts and no provenance nag of its own; a
collection is graded by its members plus its own loose files; the stack pip for a group counts files
OR members. Covers:
  1. #772 spans: a family / collection with no dated files of its own spans its members' range (card
     face, card_span, the lent "≈ ... · from <card>" range of a loose file); its own dated files
     widen it; its own start/end overrides still win; a non-group card is unchanged;
  2. #765 one shared rule: the pips and the Curator checklist agree on cover / write-up / dates for
     every card, groups included; a group with well-curated members passes write-up and dates; a
     collection's undated loose files count as one unit;
  3. #554 rules: no missing_provenance / missing_provenance_credit need for a family, still one for a
     collection and a project; whereabouts stays refused on a family; the stack pip for a file-bucket
     collection (3+ files, 0 members) passes.
Prints PASS/FAIL per check and ends with ALL PASS or FAILED: <names>.
"""

import os
import sys
import time

import _testenv  # noqa: E402
TMP = _testenv.isolate("family772-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from core import (actor, card_level, card_rules, cards, curator, db, item_dates, items,  # noqa: E402
                  membership, paths)
from core.card_rules import CardError  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
DAY = 86400
Y2000 = int(time.mktime((2000, 6, 1, 12, 0, 0, 0, 0, -1)))
Y2010 = int(time.mktime((2010, 6, 1, 12, 0, 0, 0, 0, -1)))
Y2020 = int(time.mktime((2020, 6, 1, 12, 0, 0, 0, 0, -1)))
_n = [0]


def add_files(card_id, dates):
    """One file per entry: a real content_date, or None for an undated file."""
    slugs = []
    for d in dates:
        _n[0] += 1
        slug = f"fv772-{_n[0]}"
        (paths.storage_dir() / f"{slug}.txt").write_text(f"file {slug}\n")
        db.insert_upload(slug, f"{slug}.txt", f"{slug}.txt", "tester", media_type="image", content_date=d)
        slugs.append(slug)
    membership.add_files(card_id, slugs, link_tag=True, merge_free_tags=False, auto_cover=False)
    return slugs


def card(title):
    return next(p for p in db.list_projects() if p["title"] == title)


def checklist(score, item):
    return next(c for c in score["checklist"] if c["item"] == item)


def need_kinds(c):
    return sorted(r["need"] for r in cards.provenance_whereabouts_needs(only_id=c["id"]))


with actor.acting_as(actor.ACTOR_UI):
    for t in ("Car A", "Car B", "Car C", "Fleet", "Joe Builds", "Lego Bucket", "Override Fleet", "Plain"):
        cards.create(t)
    a, b, c = card("Car A"), card("Car B"), card("Car C")
    add_files(a["id"], [Y2000, Y2000 + 10 * DAY])
    add_files(b["id"], [Y2010])
    add_files(c["id"], [Y2020, Y2020 + 5 * DAY])
    for m in (a, b, c):
        items.update(m["writeup_slug"], type_metadata={"body": "w" * 250})
    cards.set_kind(card("Fleet")["id"], "collection")
    cards.set_provenance(card("Fleet")["id"], "created")  # not a silent reference collection
    cards.set_kind(card("Joe Builds")["id"], "family")
    cards.set_kind(card("Lego Bucket")["id"], "collection")
    cards.set_provenance(card("Lego Bucket")["id"], "collected")
    cards.set_kind(card("Override Fleet")["id"], "collection")
    cards.set_provenance(card("Override Fleet")["id"], "created")
    for m in (a, b, c):
        cards.add_to_family(card("Fleet")["id"], m["id"])
        cards.add_to_family(card("Override Fleet")["id"], m["id"])
    cards.add_to_family(card("Joe Builds")["id"], a["id"])
    cards.add_to_family(card("Joe Builds")["id"], c["id"])
    loose = add_files(card("Fleet")["id"], [None])  # one undated loose file on the collection
    add_files(card("Lego Bucket")["id"], [None, None, None, None])
    cards.update(card("Override Fleet")["id"], start=Y2010, end=Y2010 + DAY)
    add_files(card("Plain")["id"], [Y2010])

fleet, joe, lego, ovr, plain = (card(t) for t in ("Fleet", "Joe Builds", "Lego Bucket", "Override Fleet", "Plain"))

# --- 1. #772 spans ---------------------------------------------------------------------------------
print("--- 1. group spans roll up their members ---")
face = cards.card_face(fleet)
check("collection span = earliest member start .. latest member end",
      (face["effective_start"], face["effective_end"]) == (Y2000, Y2020 + 5 * DAY),
      (face["effective_start"], face["effective_end"]))
check("collection face shows a date line", bool(face["dates"]), face["dates"])
check("card_span agrees with the face", cards.card_span(fleet) == (face["effective_start"], face["effective_end"]))
jf = cards.card_face(joe)
check("family span = its two members", (jf["effective_start"], jf["effective_end"]) == (Y2000, Y2020 + 5 * DAY),
      (jf["effective_start"], jf["effective_end"]))
of = cards.card_face(ovr)
check("the group's own start/end overrides still win", (of["effective_start"], of["effective_end"]) == (Y2010, Y2010 + DAY),
      (of["effective_start"], of["effective_end"]))
pf = cards.card_face(plain)
check("a plain card is unchanged", (pf["effective_start"], pf["effective_end"]) == (Y2010, Y2010))
lf = cards.card_face(lego)
check("a collection with nothing dated anywhere still has no span", lf["effective_start"] is None)
info = item_dates.item_date(db.get_by_slug(loose[0]))
check("a loose undated file on the collection borrows the rolled-up range (#757 display)",
      info["approx"] and (info["start"], info["end"]) == (Y2000, Y2020 + 5 * DAY) and info["card_title"] == "Fleet",
      info)
check("... and is not a needs_date item", loose[0] not in {r["row"]["slug"] for r in item_dates.needs_date_rows()})
with actor.acting_as(actor.ACTOR_UI):
    add_files(fleet["id"], [Y2020 + 400 * DAY])
check("the group's own dated file widens the rolled-up span",
      cards.card_span(card("Fleet"))[1] == Y2020 + 400 * DAY, cards.card_span(card("Fleet")))
fleet = card("Fleet")

# --- 2. #765: the pips and the Curator agree, groups graded by members -----------------------------
print("--- 2. one kind-aware rule ---")
for p in db.list_projects():
    lvl = card_level.card_level(p)
    sc = curator.score_project(p)
    for pip, item in (("cover", "cover_image"), ("dates", "content_dates"), ("writeup", "writeup_body")):
        row = checklist(sc, item)
        check(f"{p['title']}: pip {pip} == Curator {item}",
              lvl["checks"][pip] == row["passed"] and lvl["reasons"][pip] == row["reason"],
              (lvl["checks"][pip], lvl["reasons"][pip], row["passed"], row["reason"]))
lvl = card_level.card_level(fleet)
check("collection with written-up members passes the write-up check", lvl["checks"]["writeup"], lvl["reasons"])
check("collection: 3 dated members + undated loose files = 3 of 4 units (75%) fails dates",
      not lvl["checks"]["dates"] and "3 of 3 members" in lvl["reasons"]["dates"], lvl["reasons"]["dates"])
jl = card_level.card_level(joe)
check("family graded by members only: dates and write-up pass", jl["checks"]["dates"] and jl["checks"]["writeup"],
      jl["reasons"])
sc = curator.score_project(joe)
check("Curator does not nudge a family for write-up / dates its members cover",
      not any(u["item"] in ("writeup_body", "content_dates") for u in sc["unmet"]), sc["unmet"])
check("a group with no cover of its own still fails cover (its face has no art)", not jl["checks"]["cover"],
      jl["reasons"]["cover"])

# --- 3. #554 rules -----------------------------------------------------------------------------------
print("--- 3. family vs collection rules ---")
check("family: no missing_provenance need", "missing_provenance" not in need_kinds(joe), need_kinds(joe))
with actor.acting_as(actor.ACTOR_UI):
    cards.set_provenance(joe["id"], "collected")
check("family: no missing_provenance_credit need either", need_kinds(card("Joe Builds")) == [], need_kinds(card("Joe Builds")))
with actor.acting_as(actor.ACTOR_UI):
    cards.set_provenance(lego["id"], None)
check("collection: still nagged for provenance", "missing_provenance" in need_kinds(card("Lego Bucket")))
check("project: still nagged for provenance", "missing_provenance" in need_kinds(plain))
check("provenance_nag_applies: family no, collection yes",
      not card_rules.provenance_nag_applies("family") and card_rules.provenance_nag_applies("collection"))
try:
    with actor.acting_as(actor.ACTOR_UI):
        cards.set_whereabouts(joe["id"], "have_it")
    check("family: whereabouts refused", False, "accepted")
except CardError as e:
    check("family: whereabouts refused", e.code == "bad_whereabouts", e.code)
ll = card_level.card_level(card("Lego Bucket"))
check("file-bucket collection (4 files, 0 members) earns the stack pip", ll["checks"]["stack"], ll["reasons"]["stack"])
check("family with 2 members and no files: no stack pip", not jl["checks"]["stack"], jl["reasons"]["stack"])

print("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS))
