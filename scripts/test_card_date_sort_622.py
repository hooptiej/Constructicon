#!/usr/bin/env python3
"""#622: the item grids sort by the date the card shows. Throwaway DB, no server.

    python scripts/test_card_date_sort_622.py

The item-card payload carries `card_date_epoch`, computed by web/shapes.py from the same
core/timeline.py resolve_item_date call as the card's date label (`card_date`). This checks, for
every way an item gets its date (a content date, a display-date override set by hand, a date made,
the file's own modified time from before the install's first upload; #725: nothing real = UNDATED,
never the upload time):

  1. `card_date_epoch` is the resolver's date, and the label on the card is that same date ("Undated"
     and null for an item with nothing real, #725);
  2. the four grids (home Files, /unfiled, the user gallery, the hobby page's loose objects) embed it,
     load the shared Sort (static/js/item-sort.js, cache-busted like the other static JS) and offer the
     shared options (templates/_item_sort_options.html), "Date: newest" selected;
  3. the server-side face of the same files (a card's pile on the project / hobby pages, built by
     core/cards.py) shows the same date as the payload's `card_date`;
  4. GET /api/home/files sorts date_newest / date_oldest by (card date, then upload time), undated
     items LAST in both directions (#725); once they sit on a card they inherit its range ("~ ... .
     from <card>") and sort at the range's start; the default sort is date_newest, and newest / oldest
     still mean upload time;
  5. files_feed.SORTS, item-sort.js and the options template list the same sorts in the same order.
Prints PASS / FAIL per check and ends with ALL PASS or FAILED: <names>.
"""

import json
import os
import re
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("carddate622-")
os.environ.setdefault("CAPTION_DISABLED", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core import actor, cards, datefmt, db, hobbies, membership, tags, timeline  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from web import files_feed, shapes  # noqa: E402

FAILS = []
UPLOADER = "tester622"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def day_epoch(y, m, d):
    """Local noon of a calendar day, as UTC seconds (the app's Mountain Time convention)."""
    from datetime import datetime
    return timeline.source_datetime_to_epoch(datetime(y, m, d, 12, 0))


# --- the fixture: one file per dating rule, uploads in a different order than their card dates ----
UP = day_epoch(2024, 3, 1)  # upload times: UP + n hours
FIXTURE = [
    # slug,        upload hour, columns written,                                     the date the card shows
    ("content",    5, {"content_date": day_epoch(2015, 6, 10)},                       day_epoch(2015, 6, 10)),
    ("override",   1, {"content_date": day_epoch(2016, 1, 2),
                       "display_date_override": day_epoch(2012, 8, 20)},              day_epoch(2012, 8, 20)),
    ("none",       3, {},                                                             None),  # #725: undated
    ("srcmod",     2, {"source_modified_at": day_epoch(2019, 11, 5)},                 day_epoch(2019, 11, 5)),
    ("made",       6, {"content_date": day_epoch(2018, 1, 1),
                       "type_metadata": json.dumps({"date_made": "2010-04-03"})},     None),  # filled below
    ("tie_early",  4, {"content_date": day_epoch(2014, 2, 2)},                        day_epoch(2014, 2, 2)),
    ("tie_late",   7, {"content_date": day_epoch(2014, 2, 2)},                        day_epoch(2014, 2, 2)),
    ("newupload",  9, {},                                                             None),  # #725: undated
]


def seed():
    db.init_db()
    with actor.acting_as(actor.ACTOR_SCRIPT):
        for slug, _, _, _ in FIXTURE:
            db.insert_upload(slug, f"{slug}.jpg", f"{slug}.jpg", UPLOADER, media_type="image",
                             content_description=f"File {slug}")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for slug, hour, cols, _ in FIXTURE:
            conn.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (UP + hour * 3600.0, slug))
            for col, val in cols.items():
                conn.execute(f"UPDATE capture_events SET {col} = ? WHERE slug = ?", (val, slug))
        conn.commit()
    finally:
        conn.close()


def expected():
    """slug -> the date the card should show. "made" comes from the physical-piece parser itself."""
    from core import physical_piece
    out = {slug: want for slug, _, _, want in FIXTURE}
    out["made"] = physical_piece.date_made_epoch({"date_made": "2010-04-03"})
    return out


def label_of(epoch):
    """The card's date line for a real date, or "Undated" (#725)."""
    return datefmt.day(epoch) if epoch is not None else "Undated"


def embedded(html, grid_id, key="seed"):
    """#553: what a page's file grid embeds: its config JSON block (<script type="application/json"
    id="<grid>-config">, templates/_item_grid.html), `seed` (the first batch of the default view) or
    `meta`. The pages used to assign their whole lists to page-script variables."""
    m = re.search(r'<script type="application/json" id="%s-config"[^>]*>(.*?)</script>' % re.escape(grid_id), html, re.S)
    return json.loads(m.group(1)).get(key) if m else None


def main():
    seed()
    want = expected()
    check("fixture: the date-made file resolves to its date made (2010), not its content date",
          want["made"] is not None and datefmt.day(want["made"]) == "Apr 3, 2010", datefmt.day(want["made"] or 0))
    uploaded = {slug: UP + hour * 3600.0 for slug, hour, _, _ in FIXTURE}

    # --- 1. the shape -------------------------------------------------------------------------------
    print("--- 1. card_date_epoch is the card's own date ---")
    for slug in want:
        pub = shapes._to_public(db.get_by_slug(slug))
        check(f"{slug}: card_date_epoch == the resolved date ({label_of(want[slug])})",
              pub["card_date_epoch"] == want[slug], (pub["card_date_epoch"], want[slug]))
        check(f"{slug}: the card's label is that same date",
              pub["card_date"] == label_of(want[slug]), (pub["card_date"], pub["card_date_epoch"]))
    pub_none = shapes._to_public(db.get_by_slug("none"))
    check("#725: nothing real = the card says Undated, card_date_epoch is null (never the upload time)",
          pub_none["card_date_epoch"] is None and pub_none["card_date"] == "Undated"
          and pub_none["uploaded_at"] == uploaded["none"])

    # --- 2. the four grids ----------------------------------------------------------------------------
    print("--- 2. the four grids embed it and use the shared Sort ---")
    with actor.acting_as(actor.ACTOR_SCRIPT):
        hobby = hobbies.create("Sorting622").data["hobby"]
        for slug in want:
            tags.attach(slug, ["Sorting622"])
    hobby_slug = hobby["slug"] if isinstance(hobby, dict) else hobby
    client = _testenv.client(webapp.app)
    pages = {  # #553: (url, the file grid's id); each grid embeds its first batch (all 6 fixture files here)
        "home": ("/", "files"),
        "unfiled": ("/unfiled", "unfiled"),
        "user gallery": (f"/gallery/user/{UPLOADER}", "uploads"),
        "hobby": (f"/hobby/{hobby_slug}", "loose"),
    }
    for name, (url, var) in pages.items():
        r = client.get(url)
        check(f"{name}: GET {url} 200", r.status_code == 200, r.status_code)
        html = r.text
        got = embedded(html, var)
        check(f"{name}: the page embeds its items", isinstance(got, list) and len(got) == len(want),
              None if got is None else len(got))
        got = got or []
        check(f"{name}: every embedded item carries card_date_epoch == its card's date",
              all(it.get("card_date_epoch") == want[it["slug"]] for it in got),
              [(it["slug"], it.get("card_date_epoch")) for it in got if it.get("card_date_epoch") != want[it["slug"]]])
        check(f"{name}: and card_date is the label of that same date",
              all(it["card_date"] == label_of(it["card_date_epoch"]) for it in got))
        check(f"{name}: loads the shared static/js/item-sort.js with a cache-busting ?v=",
              re.search(r'<script src="/static/js/item-sort\.js\?v=[0-9a-f]+"></script>', html) is not None)
        check(f"{name}: the Sort menu is the shared one, Date: newest selected",
              '<option value="date_newest" selected>Date: newest</option>' in html
              and '<option value="newest">Uploaded: newest</option>' in html)
        nonce_hdr = re.search(r"'nonce-([^']+)'", r.headers.get("content-security-policy", ""))
        inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", html)
        check(f"{name}: every inline script carries this response's CSP nonce ({len(inline)} inline)",
              nonce_hdr is not None and all(f'nonce="{nonce_hdr.group(1)}"' in attrs for attrs in inline))
    meta = embedded(client.get("/").text, "files", "meta")
    check("home: the embedded seed view is Date: newest", meta and meta["seed"]["sort"] == "date_newest", meta and meta["seed"])

    # --- 2b. #725: undated items go last, in both directions, while they are on no card -----------------
    print("--- 2b. undated items sort last ---")
    dated = [s for s in want if want[s] is not None]
    undated = [s for s in want if want[s] is None]

    def order(**params):
        r = client.get("/api/home/files", params=params)
        # only the fixture files (section 3's card added its own write-up document to the archive)
        return [it["slug"] for it in r.json()["items"] if it["slug"] in want] if r.status_code == 200 else r.status_code

    want_newest = (sorted(dated, key=lambda s: (-want[s], -uploaded[s]))
                   + sorted(undated, key=lambda s: -uploaded[s]))
    want_oldest = (sorted(dated, key=lambda s: (want[s], uploaded[s]))
                   + sorted(undated, key=lambda s: uploaded[s]))
    check("date_newest: dated newest first, then the undated (newest upload first)",
          order(sort="date_newest") == want_newest, (order(sort="date_newest"), want_newest))
    check("date_oldest: dated oldest first, and the undated STILL last",
          order(sort="date_oldest") == want_oldest, (order(sort="date_oldest"), want_oldest))

    # --- 3. the server-side face of the same files ------------------------------------------------------
    print("--- 3. the server-built file face shows the same date ---")
    with actor.acting_as(actor.ACTOR_SCRIPT):
        card = cards.create("Pile622").data["card"]
        membership.add_files(card["id"], list(want), link_tag=False, merge_free_tags=False, auto_cover=False)
    pile = cards.project_pile(card["id"])
    # only the fixture files (#725: the card's own blank write-up is undated too, so it can sit in the fan)
    faces = {c["slug"]: c["dates"] for c in pile["cards"] if c["slug"] in want} if pile else {}
    payload = {it["slug"]: it for it in shapes._card_items([db.get_by_slug(s) for s in want])}
    check("the pile drew file faces", len(faces) >= 5, len(faces))
    check("each file face's date (core/cards.py) == the grid payload's card_date for that file",
          faces and all(faces[s] == payload[s]["card_date"] for s in faces),
          {s: (faces[s], payload[s]["card_date"]) for s in faces if faces[s] != payload[s]["card_date"]})
    piles = cards.file_stacks(card["id"]) if hasattr(cards, "file_stacks") else []
    # (the card's own blank write-up is in its piles too; only the fixture files are compared)
    stacked = {c["slug"]: c["dates"] for p in (piles or []) for c in p.get("cards", []) if c["slug"] in payload}
    check("the project page's per-type piles agree too",
          len(stacked) >= 5 and all(stacked[s] == payload[s]["card_date"] for s in stacked), stacked)

    range_start, range_end = min(want[s] for s in dated), max(want[s] for s in dated)
    inherited = f"≈ {datefmt.month(range_start)} - {datefmt.month(range_end)} · from Pile622"
    check("#725: on the card, an undated file inherits the card's REAL-dated range (approximate)",
          all(payload[s]["card_date"] == inherited and payload[s]["card_date_epoch"] == range_start for s in undated),
          {s: (payload[s]["card_date"], payload[s]["card_date_epoch"]) for s in undated})
    check("#725: the inherited date is never stored as content_date",
          all(db.get_by_slug(s)["content_date"] is None for s in undated))

    # --- 4. the server's order ------------------------------------------------------------------------------
    print("--- 4. GET /api/home/files: the date sorts ---")
    # Every file is now on a card: the undated ones sort at their card's range start (#725).
    sort_at = {s: (want[s] if want[s] is not None else range_start) for s in want}
    by_date_newest = sorted(want, key=lambda s: (-sort_at[s], -uploaded[s]))
    by_date_oldest = sorted(want, key=lambda s: (sort_at[s], uploaded[s]))
    by_up_newest = sorted(want, key=lambda s: -uploaded[s])

    check("date_newest: by card date, newest first", order(sort="date_newest") == by_date_newest,
          (order(sort="date_newest"), by_date_newest))
    check("date_oldest: by card date, oldest first", order(sort="date_oldest") == by_date_oldest,
          (order(sort="date_oldest"), by_date_oldest))
    check("same card date: the upload time decides (tie_late after tie_early when oldest first)",
          by_date_oldest.index("tie_early") < by_date_oldest.index("tie_late")
          and order(sort="date_oldest").index("tie_early") < order(sort="date_oldest").index("tie_late"))
    check("no sort given = date_newest (the default, #622)", order() == by_date_newest)
    check("newest still means upload time", order(sort="newest") == by_up_newest, order(sort="newest"))
    check("oldest still means upload time", order(sort="oldest") == by_up_newest[::-1])
    check("the date order differs from the upload order here (the fixture tests something)",
          by_date_newest != by_up_newest)
    r = client.get("/api/home/files", params={"sort": "card_date"})
    check("an unknown sort: 400 bad_sort naming the real ones",
          r.status_code == 400 and r.json()["error"]["code"] == "bad_sort" and "date_newest" in r.json()["error"]["message"])

    # --- 5. one list of sorts --------------------------------------------------------------------------------
    print("--- 5. the server, the JS and the menu list the same sorts ---")
    js = read("web/static/js/item-sort.js")
    js_names = re.findall(r"^\s{4}(\w+): (?:\(a, b\) =>|byDate\()", js, re.M)
    opt_names = re.findall(r'<option value="(\w+)"', read("web/templates/_item_sort_options.html"))
    check("files_feed.SORTS == item-sort.js SORTS == the menu's options, same order",
          list(files_feed.SORTS) == js_names == opt_names, (list(files_feed.SORTS), js_names, opt_names))
    check("the default is the same everywhere (date_newest)",
          files_feed.DEFAULT_SORT == "date_newest" and "const DEFAULT = 'date_newest';" in js)
    # #553: the four pages draw one shared grid; its include holds the Sort menu, item-grid.js wires it.
    for tpl in ("home.html", "unfiled.html", "user_gallery.html", "hobby.html"):
        src = read("web/templates/" + tpl)
        check(f"{tpl}: no hand-rolled sorter or Sort menu of its own; includes the shared grid",
              "uploaded_at - " not in src and "ItemSort" not in src and '{% include "_item_grid.html" %}' in src)
    grid_tpl, grid_js = read("web/templates/_item_grid.html"), read("web/static/js/item-grid.js")
    check("_item_grid.html carries the shared Sort menu; item-grid.js wires it with ItemSort and sorts nothing itself",
          '{% include "_item_sort_options.html" %}' in grid_tpl and "ItemSort.wire(" in grid_js
          and "uploaded_at - " not in grid_js and "localeCompare" not in grid_js)

    print()
    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
