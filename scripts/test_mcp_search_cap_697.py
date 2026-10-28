#!/usr/bin/env python3
"""MCP tools that return many items send a PREVIEW of each one's extracted_text, not all of it (#697).
Throwaway DB, no server.

    python scripts/test_mcp_search_cap_697.py

In-process (scripts/_testenv.py: temp DB + storage), the MCP tool functions called directly. Covers:
  1. constructicon_search: the default preview is at most 300 characters (plus the "…" marks), centred on
     the first query word found in the text (word start, case and accents folded), else the text's start;
     extracted_text_chars is the full length and extracted_text_truncated is right; a short text comes back
     whole and untruncated; a ~200 KB item keeps the whole result small;
  2. text_chars: a bigger window up to 5,000, never past it (6,000 and 10**9 give 5,000); 0 gives no text;
     a negative or non-integer value is a bad_request; the tool's input schema lists text_chars;
  3. constructicon_get (one item) still returns the whole text;
  4. the other multi-item tools cap it too: get_posts_for_tag (and its text_chars), get_project (items and
     cover, and its text_chars), get_related, add_related, add_items_to_project, list_redacted,
     list_restricted, list_brand_assets, and a blog entry's items.
Exits 1 if any check fails.
"""

import asyncio
import json
import os
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("mcpcap697-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core import actor, db, paths  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)

ELL = "…"
DEFAULT = mcp.TEXT_PREVIEW_CHARS
MAX = mcp.TEXT_PREVIEW_MAX_CHARS
check("the default preview is 300 characters and the hard maximum 5,000", (DEFAULT, MAX) == (300, 5000), (DEFAULT, MAX))


def put(slug, filename, text, media_type="document"):
    (paths.storage_dir() / f"{slug}.txt").write_text(f"{slug}\n")
    db.insert_upload(slug, filename, f"{slug}.txt", "tester", media_type=media_type)
    db.set_extracted_text(slug, text)


def body(t):
    """The preview without its "…" marks."""
    return (t or "").strip(ELL)


# ---- fixtures -----------------------------------------------------------------------------------
# ~200 KB of filler with one marker word about 60% of the way in (the screen-shot-catalog case).
FILLER = "lorem ipsum dolor sit amet consectetur " * 5200          # 202,800 chars
HIT_AT = 120_000
BIG = FILLER[:HIT_AT] + " Zanzibarite marker here " + FILLER[HIT_AT:]
put("big", "screen shot catalog.html", BIG)
put("short", "short.txt", "a short zanzibarite note")
put("head", "head.txt", "zanzibarite at the very start " + "x" * 5000)
put("tail", "tail.txt", "y" * 5000 + " ends with zanzibarite")
put("accent", "accent.txt", "z" * 2000 + " the Café Crème menu " + "w" * 2000)
put("nohit", "nohit.txt", "q" * 3000)  # matched by its filename only
put("empty", "empty.txt", "")


def search(**kw):
    with actor.acting_as(actor.ACTOR_MCP):
        return mcp.constructicon_search(**kw)


def by_slug(rows):
    return {r["slug"]: r for r in rows} if isinstance(rows, list) else {}


# ---- 1. search, default preview ------------------------------------------------------------------
print("--- 1. search: the default preview ---")
res = by_slug(search(query="zanzibarite"))
check("search finds the five zanzibarite items", {"big", "short", "head", "tail"} <= set(res), sorted(res))
b = res.get("big", {})
t = b.get("extracted_text") or ""
check("big: preview is at most 300 chars plus the two marks", len(body(t)) <= DEFAULT and len(t) <= DEFAULT + 2, len(t))
check("big: cut at both ends, so it starts and ends with …", t.startswith(ELL) and t.endswith(ELL), repr(t[:5] + t[-5:]))
check("big: the preview holds the query word", "Zanzibarite" in t, t[:80])
off = body(t).find("Zanzibarite")
check("big: the hit sits inside the window, not at an edge (centred-ish)", 50 <= off <= DEFAULT - 50, off)
check("big: the preview is the text at that place",
      body(t) in BIG and BIG.index(body(t)) <= HIT_AT <= BIG.index(body(t)) + DEFAULT, BIG.find(body(t)))
check("big: extracted_text_chars is the full length", b.get("extracted_text_chars") == len(BIG), b.get("extracted_text_chars"))
check("big: extracted_text_truncated is true", b.get("extracted_text_truncated") is True)
size = len(json.dumps(search(query="zanzibarite")))
check("a search hitting a ~200 KB item stays small (< 20,000 chars of JSON)", size < 20_000, size)
print(f"   search(query='zanzibarite'): {len(res)} items, {size:,} chars of JSON; big item's text {len(BIG):,} chars")

s = res.get("short", {})
check("short: a text under the window comes back whole", s.get("extracted_text") == "a short zanzibarite note", s.get("extracted_text"))
check("short: not truncated, length right", s.get("extracted_text_truncated") is False and s.get("extracted_text_chars") == 24,
      (s.get("extracted_text_truncated"), s.get("extracted_text_chars")))

h = res.get("head", {}).get("extracted_text") or ""
check("head: a hit at the start gives a window from the start (no leading …)",
      h.startswith("zanzibarite") and h.endswith(ELL) and len(h) == DEFAULT + 1, (h[:20], len(h)))
tl = res.get("tail", {}).get("extracted_text") or ""
check("tail: a hit at the end gives a window to the end (no trailing …)",
      tl.startswith(ELL) and tl.endswith("ends with zanzibarite") and len(tl) == DEFAULT + 1, (tl[-25:], len(tl)))

acc = by_slug(search(query="cafe"))
a = acc.get("accent", {}).get("extracted_text") or ""
check("accents fold: 'cafe' centres the preview on 'Café'", "Café Crème" in a, a[:60])
nh = by_slug(search(query="nohit")).get("nohit", {})
check("no hit in the text (matched by name): the preview is the text's start",
      (nh.get("extracted_text") or "").startswith("qqq") and nh.get("extracted_text", "").endswith(ELL)
      and len(nh.get("extracted_text", "")) == DEFAULT + 1, (nh.get("extracted_text") or "")[:10])
check("prefix match like the search: 'zanzi' centres on 'Zanzibarite'",
      "Zanzibarite" in (by_slug(search(query="zanzi")).get("big", {}).get("extracted_text") or ""))
check("no word start: 'anzibarite' is not a hit (search finds nothing either)", "big" not in by_slug(search(query="anzibarite")))
e = by_slug(search(query="empty")).get("empty", {})
check("an empty text stays empty, untruncated", e.get("extracted_text") == "" and e.get("extracted_text_truncated") is False
      and e.get("extracted_text_chars") == 0, (e.get("extracted_text"), e.get("extracted_text_truncated")))
tagonly = by_slug(search(query=None, tags=None))
check("a search with no query also previews (the start)", len(body(tagonly.get("big", {}).get("extracted_text"))) == DEFAULT)

# ---- 2. text_chars -------------------------------------------------------------------------------
print("--- 2. text_chars ---")
for n in (1000, 5000):
    bb = by_slug(search(query="zanzibarite", text_chars=n)).get("big", {})
    tt = bb.get("extracted_text") or ""
    check(f"text_chars={n}: a {n}-char window holding the hit", len(body(tt)) == n and "Zanzibarite" in tt, len(body(tt)))
for n in (6000, 10**9):
    tt = by_slug(search(query="zanzibarite", text_chars=n)).get("big", {}).get("extracted_text") or ""
    check(f"text_chars={n} is capped at the 5,000 maximum", len(body(tt)) == MAX, len(body(tt)))
z = by_slug(search(query="zanzibarite", text_chars=0))
check("text_chars=0: no text for any match", all(r.get("extracted_text") is None for r in z.values()) and z,
      {k: r.get("extracted_text") for k, r in z.items()})
check("text_chars=0: lengths still reported, truncated true when there was text",
      z.get("big", {}).get("extracted_text_chars") == len(BIG) and z.get("big", {}).get("extracted_text_truncated") is True)
check("text_chars=5000 on a 24-char text: whole, untruncated",
      by_slug(search(query="zanzibarite", text_chars=5000)).get("short", {}).get("extracted_text_truncated") is False)
for bad in (-1, "300", 2.5, True):
    r = search(query="zanzibarite", text_chars=bad)
    check(f"text_chars={bad!r} is refused (bad_request)", isinstance(r, dict) and r.get("ok") is False
          and r["error"]["code"] == "bad_request", r if isinstance(r, dict) else type(r))
tools = {t.name: t for t in asyncio.run(mcp.mcp.list_tools())}
for name in ("constructicon_search", "constructicon_list_items", "constructicon_get_project"):
    schema = tools[name].input_schema if hasattr(tools[name], "input_schema") else tools[name].inputSchema
    check(f"{name}'s input schema has text_chars (default 300)",
          schema.get("properties", {}).get("text_chars", {}).get("default") == 300, schema.get("properties", {}).keys())
check("the server instructions point at constructicon_get for the full text (#746: said once, not per tool)",
      "constructicon_get(slug)" in (mcp.mcp.instructions or ""))

# ---- 3. constructicon_get keeps the whole text ---------------------------------------------------
print("--- 3. get ---")
with actor.acting_as(actor.ACTOR_MCP):
    g = mcp.constructicon_get("big")
check("constructicon_get returns the whole text (one item, by design)", g.get("extracted_text") == BIG, len(g.get("extracted_text") or ""))
check("constructicon_get has no preview fields", "extracted_text_truncated" not in g)


# ---- 4. the other multi-item tools ---------------------------------------------------------------
print("--- 4. other multi-item tools ---")


def capped(name, row, n=DEFAULT):
    t = row.get("extracted_text") if isinstance(row, dict) else None
    check(f"{name}: big item's text is a {n}-char preview with the length fields",
          isinstance(t, str) and len(body(t)) == n and row.get("extracted_text_chars") == len(BIG)
          and row.get("extracted_text_truncated") is True, (len(t) if isinstance(t, str) else row))


def find(rows, slug="big"):
    return next((r for r in rows if isinstance(r, dict) and r.get("slug") == slug), {}) if isinstance(rows, list) else {}


with actor.acting_as(actor.ACTOR_MCP):
    mcp.constructicon_item_tags("big", ["catalogs697"])
    capped("list_items(which=tag)", find(mcp.constructicon_list_items(which="tag", tag="catalogs697")["items"]))
    capped("list_items(which=tag, text_chars=2000)",
           find(mcp.constructicon_list_items(which="tag", tag="catalogs697", text_chars=2000)["items"]), 2000)

    card = mcp.constructicon_create_project("Catalog card 697", cover_slug="big")
    added = mcp.constructicon_card_files(card["id"], ["big", "short"])
    capped("card_files(add)", find(added.get("items")))
    proj = mcp.constructicon_get_project(card["id"])
    capped("get_project items", find(proj.get("items")))
    capped("get_project cover", proj.get("cover") or {})
    proj0 = mcp.constructicon_get_project(card["id"], text_chars=0)
    check("get_project(text_chars=0): no item text", find(proj0.get("items")).get("extracted_text") is None)
    check("get_project: the payload stays small", len(json.dumps(proj)) < 30_000, len(json.dumps(proj)))

    capped("related(add)", find(mcp.constructicon_related("short", "big")))
    capped("get(include=related)", find(mcp.constructicon_get("short", include=["related"]).get("related")))

    mcp.constructicon_update("big", fields={"is_brand_asset": True, "brand_role": "logo"})
    capped("list_items(which=brand_assets)", find(mcp.constructicon_list_items(which="brand_assets")))
    mcp.constructicon_update("big", fields={"is_brand_asset": False})

    entry = mcp.constructicon_create_blog_entry("Entry 697")
    capped("update_blog_entry(items)", find(mcp.constructicon_update_blog_entry(entry["slug"], items=[{"slug": "big"}]).get("items")))
    capped("get_blog_entry items", find(mcp.constructicon_get_blog_entry(entry["slug"]).get("items")))

    mcp.constructicon_update("big", fields={"sensitive": True})
    capped("list_items(which=restricted)", find(mcp.constructicon_list_items(which="restricted")))
    mcp.constructicon_update("big", fields={"sensitive": False})

    mcp.constructicon_redact("big")
    capped("list_items(which=redacted)", find(mcp.constructicon_list_items(which="redacted")))
    capped("list_items(which=redacted, text_chars=2000)",
           find(mcp.constructicon_list_items(which="redacted", text_chars=2000)), 2000)

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
