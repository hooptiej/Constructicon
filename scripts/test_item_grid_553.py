#!/usr/bin/env python3
"""#553: one file grid (templates/_item_grid.html + static/js/item-grid.js + web/item_grid.py) on the
four pages that list files. Throwaway DB, the real app through TestClient with real sessions, no server:

    python scripts/test_item_grid_553.py

  1. home, /unfiled, the user gallery and a hobby page draw the SAME grid: its config block, the shared
     Sort menu, the type filter, the All/Unfiled/Filed toggle (not on /unfiled), Select (always on for
     /unfiled) and the same six bulk actions; ?rev=all reaches every grid;
  2. a viewer gets the grids with no Select button, no bulk bar and an empty `bulk` list, and the bulk
     endpoints refuse a viewer (403);
  3. every bulk action works from a grid that is not /unfiled (tag, add to card, new card from the hobby
     page's loose objects; provenance and delete from the user gallery; mark sensitive from home), each as
     ONE change-log batch that the generic undo reverses, and GET /api/items/grid shows the change;
  4. the user gallery holds every row: 1,050 uploads (over the old 1,000 cap) -> the page's count, the
     config's counts and a walk of GET /api/items/grid all say 1,050, each slug once; the page embeds one
     batch, not the list; the back link goes home;
  5. GET /api/items/grid: bad source 400 bad_source, unknown hobby 404, a cursor never continues another
     grid's list (cursor_view_mismatch), the type filter and the filed toggle per source;
  6. the project page's per-file x (POST /api/projects/{id}/remove-item) works and is undoable (#553
     re-checks the 500 the review saw; fixed in df498fa, #563).
Prints PASS / FAIL per check and ends with ALL PASS or FAILED: <names>.
"""

import json
import os
import re
import secrets
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("itemgrid553-")
os.environ.setdefault("CAPTION_DISABLED", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, hobbies, membership, tags, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from web import item_grid  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}
GALLERY = "bulk553"
N_GALLERY = 1050


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:300]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def item(slug, uploader="grid553", media_type="youtube", **kw):
    db.insert_content(slug, uploader, media_type, external_url="https://example.com/" + slug,
                      content_description=kw.pop("title", slug), **kw)
    return slug


def config(html, gid):
    m = re.search(r'<script type="application/json" id="%s-config"[^>]*>(.*?)</script>' % re.escape(gid), html, re.S)
    return json.loads(m.group(1)) if m else None


def controls(html, gid):
    """Which grid controls the page drew (by element id)."""
    found = set()
    for part in ("sort", "type", "filed", "layout", "select", "bar", "clear", "card", "new-card", "tag",
                 "provenance", "provenance-apply", "sensitive", "delete", "grid", "empty"):
        if f'id="{gid}-{part}"' in html:
            found.add(part)
    if f'id="{gid}" data-item-grid="{gid}"' in html:
        found.add("box")
    if re.search(r'class="item-grid is-selecting" id="%s"' % re.escape(gid), html):
        found.add("selecting")
    return found


def code(r):
    try:
        return r.json().get("error", {}).get("code")
    except ValueError:
        return None


def walk(c, **params):
    slugs, cursor, total, pages = [], "", None, 0
    while True:
        r = c.get("/api/items/grid", params={**params, "cursor": cursor, "limit": "500"})
        if r.status_code != 200:
            return slugs, total, r
        body = r.json()
        slugs += [i["slug"] for i in body["items"]]
        total, cursor, pages = body["total"], body["next_cursor"], pages + 1
        if not cursor or pages > 50:
            return slugs, total, r


def main():
    db.init_db()
    users.limiter.reset()

    # --- fixture -------------------------------------------------------------------------------------
    with actor.acting_as(actor.ACTOR_SCRIPT):
        for i in range(N_GALLERY):
            item(f"g{i:04d}", uploader=GALLERY)
        for i in range(6):
            item(f"loose{i}", media_type="audio" if i % 2 else "youtube")
            tags.attach(f"loose{i}", ["Grid553"])
        hobby = hobbies.create("Grid553").data["hobby"]
        card = cards.create("Grid553 card").data["card"]
        filed = item("filed0")
    hobby_slug = hobby["slug"] if isinstance(hobby, dict) else hobby
    with actor.acting_as(actor.ACTOR_SCRIPT):
        membership.add_files(card["id"], [filed], **membership.UI_EFFECTS)

    pw = {}
    with actor.acting_as(actor.ACTOR_SCRIPT):
        for name, role in (("g_ed", "editor"), ("g_view", "viewer")):
            pw[name] = "g-" + secrets.token_urlsafe(16)
            users.create_user(name, pw[name], role)
    sess = {}
    for key, name in (("E", "g_ed"), ("V", "g_view")):
        c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
        r = c.post("/api/auth/login", json={"username": name, "password": pw[name]}, headers=SAME)
        check(f"{key} signs in", r.status_code == 200, r.text[:200])
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', c.get("/").text)
        c.headers.update({**SAME, "X-CSRF-Token": m.group(1) if m else ""})
        sess[key] = c
    E, V = sess["E"], sess["V"]
    admin = _testenv.client(webapp.app)

    pages = {"home": ("/", "files"), "unfiled": ("/unfiled", "unfiled"),
             "user gallery": (f"/gallery/user/{GALLERY}", "uploads"), "hobby": (f"/hobby/{hobby_slug}", "loose")}

    # --- 1. the same grid on all four pages ------------------------------------------------------------------
    print("--- 1. four pages, one grid ---")
    core = {"box", "sort", "type", "grid", "empty", "bar", "clear", "card", "new-card", "tag", "provenance",
            "provenance-apply", "sensitive", "delete"}
    seen = {}
    for name, (url, gid) in pages.items():
        r = E.get(url)
        check(f"{name}: GET {url} 200", r.status_code == 200, r.status_code)
        html = r.text
        cfg = config(html, gid)
        ctl = controls(html, gid)
        seen[name] = ctl
        check(f"{name}: embeds the grid config", cfg is not None)
        cfg = cfg or {}
        check(f"{name}: draws every shared control (sort, type filter, bulk bar with all six actions)", core <= ctl, sorted(core - ctl))
        check(f"{name}: the bulk list is the shared one, in order", cfg.get("bulk") == list(item_grid.BULK_ACTIONS), cfg.get("bulk"))
        check(f"{name}: the Sort menu is the shared include, Date: newest selected",
              '<option value="date_newest" selected>Date: newest</option>' in html)
        check(f"{name}: loads item-grid.js, item-sort.js and undo-bar.js (cache-busted)",
              all(re.search(r'<script src="/static/js/%s\?v=\d+"></script>' % re.escape(f), html)
                  for f in ("item-grid.js", "item-sort.js", "undo-bar.js")))
        check(f"{name}: one grid, one config block", html.count("data-item-grid=") == 1 and html.count('type="application/json"') == 1)
        check(f"{name}: the config block carries this response's CSP nonce",
              re.search(r'<script type="application/json" id="%s-config" nonce="([^"]+)"' % gid, html) is not None
              and re.search(r'<script type="application/json" id="%s-config" nonce="([^"]+)"' % gid, html).group(1)
              in r.headers.get("content-security-policy", ""))
        check(f"{name}: no page-level selection bar, Delete mode or type tabs left",
              'id="selection-bar"' not in html and "delete-mode" not in html and 'id="files-tabs"' not in html)
        if name == "unfiled":
            check("unfiled: no All/Unfiled/Filed toggle (n/a), selecting always on (no Select button), grid/list layout",
                  "filed" not in ctl and "selecting" in ctl and "select" not in ctl and "layout" in ctl, sorted(ctl))
        else:
            check(f"{name}: the All/Unfiled/Filed toggle and a Select button", {"filed", "select"} <= ctl and "selecting" not in ctl, sorted(ctl))
        r2 = E.get(url + "?rev=all")
        cfg2 = config(r2.text, gid) or {}
        check(f"{name}: ?rev=all reaches the grid (its pages ask for rev=all)", (cfg2.get("meta") or {}).get("rev") == "all")
    check("the three non-/unfiled grids have exactly the same controls",
          seen["home"] == seen["user gallery"] == seen["hobby"], {k: sorted(v) for k, v in seen.items()})
    for tpl in ("home.html", "unfiled.html", "user_gallery.html", "hobby.html"):
        src = open(os.path.join(ROOT, "web", "templates", tpl), encoding="utf-8").read()
        check(f"{tpl}: no bulk script of its own (no /api/bulk, /api/delete, toggleCardSelect)",
              "/api/bulk" not in src and "/api/delete" not in src and "toggleCardSelect" not in src)

    # --- 2. viewers -------------------------------------------------------------------------------------------
    print("--- 2. a viewer: grids, no bulk controls ---")
    for name, (url, gid) in pages.items():
        r = V.get(url)
        cfg = config(r.text, gid) or {}
        ctl = controls(r.text, gid)
        check(f"viewer {name}: 200, the grid with sort/type, no Select, no bar, empty bulk list",
              r.status_code == 200 and {"sort", "type", "grid"} <= ctl and not ({"select", "bar", "delete", "tag"} & ctl)
              and cfg.get("bulk") == [] and cfg.get("can_edit") is False and "selecting" not in ctl, (r.status_code, sorted(ctl)))
    for path, data in (("/api/bulk/attach-tags", {"slugs": ["loose0"], "tag_names": "x"}),
                       ("/api/bulk/provenance", {"slugs": ["loose0"], "provenance": "found"}),
                       ("/api/bulk/add-to-project", {"slugs": ["loose0"], "project_id": str(card["id"])}),
                       ("/api/bulk/sensitive", {"slugs": ["loose0"], "sensitive": "true"}),
                       ("/api/delete", {"slugs": ["loose0"]})):
        r = V.post(path, data=data)
        check(f"viewer POST {path}: 403", r.status_code == 403, (r.status_code, r.text[:120]))

    # --- 3. bulk actions from grids that aren't /unfiled -------------------------------------------------------------
    print("--- 3. bulk actions from the other grids, with change log + undo ---")

    def changes_of(batch):
        return [dict(r) for r in db.list_change_log(batch_id=batch, limit=50)] if batch else []

    def loose_slugs():
        s, _, _ = walk(E, source=f"hobby:{hobby_slug}")
        return set(s)

    # tag, from the hobby's loose objects
    r = E.post("/api/bulk/attach-tags", data={"slugs": ["loose0", "loose1"], "tag_names": "Gridtag553"})
    b = r.json() if r.status_code == 200 else {}
    rows = changes_of(b.get("batch_id"))
    check("hobby grid: bulk tag 200, one batch, logged", r.status_code == 200 and b.get("count") == 2 and rows, (r.status_code, r.text[:200]))
    check("hobby grid: both files carry the tag", all("Gridtag553" in (db.get_by_slug(s).get("tags") or []) for s in ("loose0", "loose1")))
    body = E.get("/api/items/grid", params={"source": f"hobby:{hobby_slug}"}).json()
    check("hobby grid refresh: the API shows the new tag on the cards",
          all("Gridtag553" in i["tags"] for i in body["items"] if i["slug"] in ("loose0", "loose1")))
    r = E.post(f"/api/changes/{b.get('batch_id')}/undo")
    check("hobby grid: undo the tag", r.status_code == 200 and all("Gridtag553" not in (db.get_by_slug(s).get("tags") or []) for s in ("loose0", "loose1")),
          (r.status_code, r.text[:200]))

    # add to card, from the hobby's loose objects
    before = loose_slugs()
    r = E.post("/api/bulk/add-to-project", data={"slugs": ["loose2", "loose3"], "project_id": str(card["id"])})
    b = r.json() if r.status_code == 200 else {}
    on_card = {i["slug"] for i in db.list_project_items(card["id"])}
    check("hobby grid: bulk add to card 200, logged, both on the card",
          r.status_code == 200 and b.get("batch_id") and changes_of(b["batch_id"]) and {"loose2", "loose3"} <= on_card, (r.status_code, r.text[:200]))
    after_add = walk(E, source=f"hobby:{hobby_slug}", filed="filed")[0]
    check("hobby grid refresh: the Filed toggle now lists them", {"loose2", "loose3"} <= set(after_add), after_add)
    r = E.post(f"/api/changes/{b.get('batch_id')}/undo")
    check("hobby grid: undo the add", r.status_code == 200 and not ({"loose2", "loose3"} & {i["slug"] for i in db.list_project_items(card["id"])})
          and loose_slugs() == before, r.text[:200])

    # turn into a new card, from the hobby's loose objects
    r = E.post("/api/projects/from-selection", data={"slugs": ["loose4"], "title": "Grid553 new"})
    b = r.json() if r.status_code == 200 else {}
    check("hobby grid: turn into card 200 and answers a batch_id (#553)", r.status_code == 200 and b.get("batch_id") and b.get("slug"), r.text[:200])
    r = E.post(f"/api/changes/{b.get('batch_id')}/undo")
    check("hobby grid: undo removes the new card", r.status_code == 200 and db.get_project(b.get("slug") or "x") is None, r.text[:200])

    # provenance, from the user gallery
    r = E.post("/api/bulk/provenance", data={"slugs": ["g0001", "g0002", "g0003"], "provenance": "found"})
    b = r.json() if r.status_code == 200 else {}
    rows = changes_of(b.get("batch_id"))
    check("user gallery: bulk provenance 200, ONE change-log row for three files",
          r.status_code == 200 and b.get("count") == 3 and len(rows) == 1 and rows[0]["op"] == "item_bulk_provenance", (r.status_code, r.text[:200], len(rows)))
    check("user gallery: all three set", all(db.get_by_slug(s)["provenance"] == "found" for s in ("g0001", "g0002", "g0003")))
    r = E.post(f"/api/changes/{b.get('batch_id')}/undo")
    check("user gallery: undo the provenance", r.status_code == 200 and all(db.get_by_slug(s)["provenance"] is None for s in ("g0001", "g0002", "g0003")),
          r.text[:200])
    r = E.post("/api/bulk/provenance", data={"slugs": ["g0001", "g0002"], "provenance": "not-a-key"})
    check("bulk provenance: an unknown key is refused, nothing written",
          r.status_code in (400, 422) and code(r) == "bad_provenance" and db.get_by_slug("g0001")["provenance"] is None, (r.status_code, r.text[:200]))
    r = E.post("/api/bulk/provenance", data={"slugs": ["g0001", "nope553"], "provenance": "found"})
    check("bulk provenance: an unknown slug is 404, nothing written",
          r.status_code == 404 and db.get_by_slug("g0001")["provenance"] is None, (r.status_code, r.text[:200]))

    # delete, from the user gallery
    r = E.post("/api/delete", data={"slugs": ["g0010", "g0011"]})
    b = r.json() if r.status_code == 200 else {}
    s_after, total_after, _ = walk(E, source=f"user:{GALLERY}")
    check("user gallery: bulk delete 200, logged, gone from the grid",
          r.status_code == 200 and b.get("deleted") == 2 and changes_of(b.get("batch_id")) and total_after == N_GALLERY - 2
          and not ({"g0010", "g0011"} & set(s_after)), (r.status_code, total_after))
    r = E.post(f"/api/changes/{b.get('batch_id')}/undo")
    check("user gallery: undo the delete", r.status_code == 200 and walk(E, source=f"user:{GALLERY}")[1] == N_GALLERY, r.text[:200])

    # mark sensitive, from home (undoing a mark is an unmark: admin only, #603)
    r = E.post("/api/bulk/sensitive", data={"slugs": ["loose5"], "sensitive": "true"})
    b = r.json() if r.status_code == 200 else {}
    check("home: bulk mark sensitive 200, logged", r.status_code == 200 and b.get("count") == 1 and changes_of(b.get("batch_id")), r.text[:200])
    r = admin.post(f"/api/changes/{b.get('batch_id')}/undo")
    check("home: undo the mark (admin)", r.status_code == 200 and not db.get_by_slug("loose5").get("sensitive"), r.text[:200])

    # --- 4. the user gallery: every row --------------------------------------------------------------------------------
    print("--- 4. the user gallery is complete (over 1,000) ---")
    r = E.get(f"/gallery/user/{GALLERY}")
    html = r.text
    conn = db.get_conn()
    try:
        real = conn.execute("SELECT COUNT(*) FROM capture_events WHERE tech = ?", (GALLERY,)).fetchone()[0]
    finally:
        conn.close()
    m = re.search(r'id="uploads-count">(\d+) uploads?<', html)
    cfg = config(html, "uploads") or {}
    total = sum(c["all"] for c in (cfg.get("meta") or {}).get("counts", {}).values())
    check(f"user gallery: the real row count is {N_GALLERY}", real == N_GALLERY, real)
    check("user gallery: the page's count is every row", bool(m) and int(m.group(1)) == real, m.group(0) if m else "no count")
    check("user gallery: the config's counts add up to every row", total == real, total)
    check("user gallery: the page embeds one batch (120), not the whole list", len(cfg.get("seed") or []) == 120, len(cfg.get("seed") or []))
    s, t, last = walk(E, source=f"user:{GALLERY}")
    check("user gallery: the grid endpoint pages through every row, each once", len(s) == len(set(s)) == real and t == real, (len(s), t))
    check("user gallery: the back link goes home (it pointed at /gallery, which only redirects)",
          'href="/gallery"' not in html and re.search(r'<a class="back-link" href="/">\s*<svg[^>]*>.*?</svg>\s*Back home', html, re.S) is not None)

    # --- 5. the endpoint ---------------------------------------------------------------------------------------------------
    print("--- 5. GET /api/items/grid ---")
    r = E.get("/api/items/grid", params={"source": "nope"})
    check("bad source: 400 bad_source", r.status_code == 400 and code(r) == "bad_source", (r.status_code, r.text[:200]))
    r = E.get("/api/items/grid", params={"source": "hobby:no-such-hobby-553"})
    check("unknown hobby: 404 not_found", r.status_code == 404 and code(r) == "not_found", (r.status_code, r.text[:200]))
    r = E.get("/api/items/grid", params={"source": f"user:{GALLERY}", "limit": "5"})
    cur = r.json()["next_cursor"]
    r = E.get("/api/items/grid", params={"source": "home", "limit": "5", "cursor": cur})
    check("a user-gallery cursor doesn't continue home's list: cursor_view_mismatch", r.status_code == 400 and code(r) == "cursor_view_mismatch", r.text[:200])
    r = E.get("/api/home/files", params={"limit": "5"})
    r2 = E.get("/api/items/grid", params={"source": "home", "limit": "5", "cursor": r.json()["next_cursor"]})
    check("/api/home/files is /api/items/grid?source=home (their cursors are one)", r2.status_code == 200, r2.text[:200])
    s, t, _ = walk(E, source=f"hobby:{hobby_slug}", type="audio")
    check("hobby grid: the type filter lists that type only", set(s) == {"loose1", "loose3", "loose5"} and t == 3, (s, t))
    body = E.get("/api/items/grid", params={"source": "unfiled", "limit": "500"}).json()
    check("unfiled grid: counts per type, labels from the registry, the card on a file left out",
          filed not in {i["slug"] for i in body["items"]} and "youtube" in body["counts"] and body["labels"].get("youtube"), body.get("labels"))
    s_home, t_home, _ = walk(E, source="home", filed="filed")
    check("home grid: Filed lists the file on a card", filed in s_home)

    # --- 6. the project page's per-file x -------------------------------------------------------------------------------------
    print("--- 6. remove a file from a card (project page x) ---")
    r = E.post(f"/api/projects/{card['slug']}/remove-item", data={"slug": filed})
    b = r.json() if r.status_code == 200 else {}
    check("remove-item: 200 (the review's 500 is gone), detached, logged",
          r.status_code == 200 and b.get("batch_id") and filed not in {i["slug"] for i in db.list_project_items(card["id"])}
          and changes_of(b["batch_id"]), (r.status_code, r.text[:200]))
    r = E.post(f"/api/changes/{b.get('batch_id')}/undo")
    check("remove-item: undo puts it back", r.status_code == 200 and filed in {i["slug"] for i in db.list_project_items(card["id"])}, r.text[:200])
    r = V.post(f"/api/projects/{card['slug']}/remove-item", data={"slug": filed})
    check("remove-item: a viewer is refused (403)", r.status_code == 403, r.status_code)

    print()
    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
