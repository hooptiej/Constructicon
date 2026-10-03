"""Standalone test for the slimmed item-grid payload (#517); no pytest in this repo.

Home's Files panel, /unfiled, the user gallery and the hobby page's loose objects embed their
items as inline JSON for client-side rendering. web/app.py's _card_item_public() projects each
record down to CARD_ITEM_FIELDS. This checks that:

  1. every `item.<field>` / `it.<field>` / sort `a.<field>` the card renderer
     (web/static/js/cards.js) and the page scripts read is in the whitelist (so the cards
     can't silently lose a lamp, badge, tag or sort key);
  2. every `type_metadata` key the card reads is kept;
  3. the heavy fields no card needs are gone, long tooltips are clipped, short ones intact;
  4. the projection never mutates its input;
  5. (optional) with a JSON file of real _to_public() records, reports before/after size:
        python scripts/test_home_payload.py --sample items.json

Run from the repo root: python scripts/test_home_payload.py
"""
import copy
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import card_payload as webapp  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# --- 1. fields the JS reads ---------------------------------------------------------------
# Item variable names per file: cards.js uses `item`; pages use item / it / a / b in sorters.
JS_SOURCES = {
    "web/static/js/cards.js": ("item",),
    "web/templates/home.html": ("item", "a", "b"),
    "web/templates/unfiled.html": ("item", "a", "b"),
    "web/templates/user_gallery.html": ("item", "a", "b"),
    "web/templates/hobby.html": ("it", "a", "b"),
}
# Properties of non-item objects that share a variable name in those files (a / b / item).
NOT_ITEM_FIELDS = {
    "web/templates/home.html": {"dataset", "title", "id", "name", "slug_"},
    "web/templates/hobby.html": {"dataset", "title", "id", "name"},
}

whitelist = set(webapp.CARD_ITEM_FIELDS)
for rel, names in JS_SOURCES.items():
    src = read(rel)
    # Only look at the item-grid region of the pages (the card consumers), not the whole page:
    # cards.js is read whole; templates are scanned from their first ItemCards/sorter use.
    if rel.endswith(".html"):
        start = min([i for i in (src.find("ItemCards"), src.find("SORTERS")) if i >= 0] or [0])
        src = src[start:]
    used = set()
    for name in names:
        used |= set(re.findall(r"\b%s\.([A-Za-z_]\w*)" % name, src))
    used -= {"push", "includes", "slice", "length", "map", "filter", "forEach", "localeCompare",
             "has", "get", "set", "add", "delete", "clear", "size", "tags_", "dataset"}
    used -= NOT_ITEM_FIELDS.get(rel, set())
    missing = sorted(u for u in used if u not in whitelist)
    check(not missing, "%s reads only whitelisted fields (checked %d; missing: %s)" % (rel, len(used), missing))

# Spot checks the regex can't see: fields the card visibly depends on.
for must in ("slug", "display_name", "type_label", "card_date", "thumb_url", "has_thumbnail", "redacted",
             "codes", "highlight", "provenance", "ocr_status", "extracted_text", "caption_capable",
             "type_metadata", "type_icon", "client", "uploaded_by_display", "tags", "uploaded_at",
             "media_type"):
    check(must in whitelist, "whitelist has %s" % must)

# --- 2/3/4. projection behaviour ----------------------------------------------------------
HEAVY = {"description": "x" * 500, "source": "screenshot", "artifact_link": "/f/abc", "url": "/f/abc",
         "content_date": 1, "filename": "a.png", "uploaded_by": "someone", "type_badge": "IMG"}
LONG = "word " * 400
record = {
    "slug": "abc", "display_name": "A", "media_type": "image", "type_label": "Image", "type_icon": "I",
    "card_date": "Mar 4, 2021", "thumb_url": "/f/abc/thumb", "has_thumbnail": True, "redacted": False,
    "highlight": True, "provenance": "found", "client": "P", "uploaded_by_display": "Me",
    "uploaded_at": 5, "tags": ["t"], "codes": ["3DP"], "ocr_status": "done", "extracted_text": LONG,
    "caption_capable": True,
    "type_metadata": {"rotation": 90, "auto_caption_status": "done", "auto_caption": LONG,
                      "auto_caption_model": "moondream", "stl_stats": {"tris": 1}, "body": LONG},
    **{k: v for k, v in HEAVY.items() if k != "type_badge"},
}
before = copy.deepcopy(record)
slim = webapp.card_item_public(record)
check(record == before, "projection does not mutate its input")
check(set(slim) == whitelist, "projected record has exactly the whitelisted keys")
for gone in ("description", "source", "artifact_link", "url", "content_date", "filename", "uploaded_by"):
    check(gone not in slim, "heavy/unused field dropped: %s" % gone)
tm = slim["type_metadata"]
check(set(tm) == {"rotation", "auto_caption_status", "auto_caption"}, "type_metadata trimmed to the card's keys: %s" % sorted(tm))
check(tm["rotation"] == 90 and tm["auto_caption_status"] == "done", "rotation + caption status survive")
limit = webapp.CARD_TOOLTIP_CHARS
check(len(slim["extracted_text"]) <= limit + 1 and slim["extracted_text"].endswith("…"), "long OCR text clipped for the tooltip")
check(len(tm["auto_caption"]) <= limit + 1, "long caption clipped for the tooltip")
short = webapp.card_item_public({**record, "extracted_text": "", "type_metadata": {"auto_caption": "a dog"}})
check(short["extracted_text"] == "", "empty OCR text stays empty ('OCR found no text' lamp)")
check(short["type_metadata"]["auto_caption"] == "a dog", "short caption untouched")
none = webapp.card_item_public({**record, "extracted_text": None, "type_metadata": None})
check(none["extracted_text"] is None and none["type_metadata"] == {}, "None extracted_text / type_metadata handled")
check(len(json.dumps(slim)) < len(json.dumps(record)) / 3, "projected record is much smaller than the original")

# --- 5. optional real-data size report ----------------------------------------------------
if "--sample" in sys.argv:
    path = sys.argv[sys.argv.index("--sample") + 1]
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    full = len(json.dumps(items, separators=(",", ":")))
    slimmed = len(json.dumps([webapp.card_item_public(i) for i in items], separators=(",", ":")))
    print("sample: %d items, %.0f KB -> %.0f KB (%.0f%% smaller)" % (len(items), full / 1024, slimmed / 1024, 100 * (1 - slimmed / full)))

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
