#!/usr/bin/env python3
"""The home Files panel, a page at a time (#624). Throwaway DB, no server.

    python scripts/test_home_paging_624.py                run the checks
    python scripts/test_home_paging_624.py --bench [N]    seed the same archive, print GET / size and
                                                          median time over N runs (default 7); needs
                                                          nothing from this PR, so it also runs on main
    python scripts/test_home_paging_624.py --dump DIR     also write the orders for the Node check:
        node scripts/test_home_paging_624.js --compare DIR

A seeded archive (~1,800 files across eight types, 60 cards, half the files filed, ties in the upload
time, mixed-case and accented names, plus a restricted certificate, flagged / redacted / brand / superseded
items) is served three ways and compared:

  1. the page embeds only the first batch of the default view, with correct counts and a cursor, and
     is a fraction of the old size;
  2. paging GET /api/home/files through every view (type tab x filed mode x sort) returns every visible
     item exactly once, in the order the old client-side sort produced (an independent oracle here for
     the upload-time sorts; the real String.localeCompare in the Node check for A-Z);
  3. the embedded batch plus the endpoint continues each tab with no gap and no repeat;
  4. a restricted, flagged (someone else's), redacted or brand item never appears for a viewer, in the
     page or any page of any view; an admin still sees the flagged item; its uploader sees their own;
  5. anonymous = 401 in the shared error shape; bad parameters and cursors = 400 with a specific code;
  6. an upload landing between two pages, or the last item served going away, doesn't repeat or skip.
Exits 1 if any check fails.
"""

import json
import os
import random
import re
import secrets
import sqlite3
import statistics
import sys
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("paging624-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, items, policy, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
BASE = "http://testhost.local"
SAME = {"Origin": BASE}
TYPES = {"image": 1100, "video": 200, "pdf": 150, "audio": 100, "stl": 80, "document": 70, "svg": 50, "psd": 30}
NAMES = ["Alpha", "alpha", "Beta", "beta-2", "Gamma 10", "Gamma 9", "Émile", "emile", "Zed", "zebra", "_under",
         "(paren)", "1st", "10th", "Äpfel", "apple", "Étude", "etude", "Straße", "strasse"]


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def make_item(slug, media_type, name=None, uploaded_by="tester", **kw):
    db.insert_upload(slug, f"{slug}.bin", f"{slug}.bin", uploaded_by, media_type=media_type,
                     content_description=name, **kw)


def seed(n_cards=60):
    """The archive. Returns {"slugs": [...], "cert": ..., "flagged": ..., "redacted": ..., "brand": ...,
    "old": [superseded slugs], "mine": the viewer's own flagged item (made later)}."""
    rnd = random.Random(624)
    db.init_db()
    out = {"slugs": []}
    base = time.time() - 400 * 86400
    i = 0
    with actor.acting_as(actor.ACTOR_SCRIPT):
        for mt, n in TYPES.items():
            for k in range(n):
                slug = f"{mt[:3]}-{k:04d}-{secrets.token_hex(2)}"
                name = f"{rnd.choice(NAMES)} {k}" if k % 7 else rnd.choice(NAMES)
                make_item(slug, mt, name)
                out["slugs"].append(slug)
                i += 1
        # restricted type, flagged (sensitive), redacted and brand items
        out["cert"] = "cert-" + secrets.token_hex(3)
        make_item(out["cert"], "certkey", "A certificate")
        out["flagged"] = "flag-" + secrets.token_hex(3)
        make_item(out["flagged"], "image", "Flagged by the admin side", sensitive=True)
        out["redacted"] = "red-" + secrets.token_hex(3)
        make_item(out["redacted"], "image", "Redacted")
        items.redact(out["redacted"])
        out["brand"] = "brand-" + secrets.token_hex(3)
        make_item(out["brand"], "image", "A brand asset")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        slugs = out["slugs"]
        # distinct upload times, newest = last seeded, except ten items that share one timestamp across types
        for n, slug in enumerate(slugs):
            conn.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (base + n * 17.0 + rnd.random(), slug))
        tie_t = base + 100 * 86400
        for slug in [slugs[5], slugs[1200], slugs[1350], slugs[1500], slugs[1700], slugs[1750], slugs[1300], slugs[20],
                     slugs[1460], slugs[1600]]:
            conn.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (tie_t, slug))
        conn.execute("UPDATE capture_events SET is_brand_asset = 1 WHERE slug = ?", (out["brand"],))
        # a few revision chains: slugs[k] is superseded by slugs[k+1] (same type)
        out["old"] = []
        for k in (10, 30, 50, 70):
            conn.execute("INSERT INTO item_revisions (old_slug, new_slug, created_at) VALUES (?, ?, ?)",
                         (slugs[k], slugs[k + 1], time.time()))
            out["old"].append(slugs[k])
        conn.commit()
    finally:
        conn.close()
    with actor.acting_as(actor.ACTOR_SCRIPT):
        card_slugs = [cards.create(f"Card {c}").data["card"]["id"] for c in range(n_cards)]
    conn = sqlite3.connect(db.DB_PATH)
    try:  # half the files are filed into a card (fixture rows; the app only reads them here)
        for n, slug in enumerate(out["slugs"]):
            if n % 2 == 0:
                conn.execute("INSERT INTO project_items (project_id, post_slug, sort_order) VALUES (?, ?, 0)",
                             (card_slugs[n % n_cards], slug))
        conn.commit()
    finally:
        conn.close()
    return out


def embedded(html):
    """(seed items, FILES meta) as the page embeds them."""
    by_type = re.search(r"const filesSeed = (.*);\n", html)
    meta = re.search(r"const FILES = (.*);\n", html)
    return (json.loads(by_type.group(1)) if by_type else None), (json.loads(meta.group(1)) if meta else None)


def bench(client, n):
    sizes, times = [], []
    for _ in range(n):
        t0 = time.perf_counter()
        r = client.get("/")
        times.append(time.perf_counter() - t0)
        sizes.append(len(r.content))
        if r.status_code != 200:
            print(f"bench: GET / answered {r.status_code}: {r.text[:200]}")
            return
    print(f"BENCH GET /: {sizes[0]} bytes, median {statistics.median(times) * 1000:.0f} ms over {n} runs "
          f"(min {min(times) * 1000:.0f}, max {max(times) * 1000:.0f})")


def count_connects():
    """Context manager-ish: patch sqlite3.connect in core.db to count opens."""
    real = db.sqlite3.connect
    box = {"n": 0}

    def counting(*a, **kw):
        box["n"] += 1
        return real(*a, **kw)
    return real, counting, box


def main():
    args = sys.argv[1:]
    data = seed()
    admin = _testenv.client(webapp.app)
    if "--bench" in args:
        i = args.index("--bench")
        n = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else 7
        real, counting, box = count_connects()
        db.sqlite3.connect = counting
        try:
            r = admin.get("/")
        finally:
            db.sqlite3.connect = real
        print(f"BENCH connections opened by one GET /: {box['n']}")
        bench(admin, n)
        if admin.get("/api/home/files").status_code == 200:  # this PR's endpoint (absent on main)
            times, size = [], 0
            for _ in range(n):
                t0 = time.perf_counter()
                r = admin.get("/api/home/files", params={"type": "image", "sort": "az"})
                times.append(time.perf_counter() - t0)
                size = len(r.content)
            print(f"BENCH GET /api/home/files (a 120-item A-Z page): {size} bytes, median {statistics.median(times) * 1000:.0f} ms over {n} runs")
        return
    dump = args[args.index("--dump") + 1] if "--dump" in args else None

    # --- users: a viewer and the uploader of one flagged item ---------------------------------
    pw = "p624-" + secrets.token_urlsafe(12)
    with actor.acting_as(actor.ACTOR_SCRIPT):
        users.create_user("boss624", pw, "admin")
        users.create_user("viewer624", pw, "viewer")
    viewer = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = viewer.post("/api/auth/login", json={"username": "viewer624", "password": pw}, headers=SAME)
    check("viewer signs in", r.status_code == 200, r.text[:200])
    with actor.acting_as("user:viewer624"):
        data["mine"] = "mine-" + secrets.token_hex(3)
        make_item(data["mine"], "image", "The viewer's own flagged item", sensitive=True)
    anon = TestClient(webapp.app, base_url=BASE, follow_redirects=False)

    def api(client, **params):
        return client.get("/api/home/files", params={k: v for k, v in params.items() if v is not None})

    def err(r):
        try:
            return r.json().get("error", {})
        except ValueError:
            return {}

    # --- the visible list, the way the old page built it, and the old client's order -------------
    from web.shapes import _split_revisions  # noqa: E402

    def oracle(client_role, tab, filed, sort, show_all=False):
        """Rows in the order the old embed-everything page + client sort produced, as `client_role`."""
        who = {"admin": actor.ACTOR_SCRIPT, "viewer": "user:viewer624"}[client_role]
        with actor.acting_as(who):
            by_type = {}
            for mt, rows in db.list_recent_items_by_type(limit_per_type=10000, include_superseded=True).items():
                rows, _ = _split_revisions(policy.filter_visible(rows), show_all)
                if rows:
                    by_type[mt] = rows
            unfiled = {r["slug"] for r in db.list_unfiled_items(include_superseded=True)}
        rows = []
        for mt in sorted(by_type):  # the client concatenated types in alphabetical order
            if tab in ("all", mt):
                rows += by_type[mt]
        if filed != "all":
            rows = [r for r in rows if (filed == "unfiled") == (r["slug"] in unfiled)]
        if sort == "newest":
            rows = sorted(rows, key=lambda r: -r["timestamp"])
        elif sort == "oldest":
            rows = sorted(rows, key=lambda r: r["timestamp"])
        return rows, by_type, unfiled

    def walk(client, limit=None, **params):
        """Every page of a view -> (slugs, pages, last response)."""
        slugs, cursor, pages = [], None, 0
        while True:
            r = api(client, cursor=cursor, limit=limit, **params)
            if r.status_code != 200:
                return slugs, pages, r
            body = r.json()
            slugs += [it["slug"] for it in body["items"]]
            pages += 1
            cursor = body["next_cursor"]
            if not cursor or pages > 400:
                return slugs, pages, r

    # --- 1. the page ------------------------------------------------------------------------------
    print("--- 1. the page embeds one batch of the default view ---")
    real, counting, box = count_connects()
    db.sqlite3.connect = counting
    try:
        r = admin.get("/")
    finally:
        db.sqlite3.connect = real
    html = r.text
    check("GET / 200", r.status_code == 200, r.status_code)
    by_type_emb, meta = embedded(html)
    rows_all, by_type, unfiled = oracle("admin", "all", "all", "newest")
    check("the page embeds filesSeed and FILES", by_type_emb is not None and meta is not None)
    check("the embed is one batch (120) of the default view", len(by_type_emb) == 120, len(by_type_emb))
    check("every type with files has a tab (counts keys == visible types)", set(meta["counts"]) == set(by_type),
          (sorted(meta["counts"]), sorted(by_type)))
    check("the seed is the first 120 of All / Newest, in order",
          [i["slug"] for i in by_type_emb] == [x["slug"] for x in rows_all[:120]])
    check("per-type counts match (all / unfiled / filed)", all(
        meta["counts"][mt] == {"all": len(rows), "unfiled": sum(1 for x in rows if x["slug"] in unfiled),
                               "filed": sum(1 for x in rows if x["slug"] not in unfiled)} for mt, rows in by_type.items()))
    check("unfiled_total is every unfiled slug", meta["unfiled_total"] == len(unfiled), (meta["unfiled_total"], len(unfiled)))
    emb_slugs = {i["slug"] for i in by_type_emb}
    check("unfiled_slugs lists only embedded items that are unfiled", set(meta["unfiled_slugs"]) == emb_slugs & unfiled)
    check("the batch size and seed view are sent", meta["batch"] == 120 and meta["seed"] == {"filed": "all", "sort": "newest"})
    check("one cursor continues the seed view", isinstance(meta["cursor"], str) and "cursors" not in meta)
    print(f"      page size {len(r.content)} bytes; embedded {len(by_type_emb)} of {len(rows_all)} items; "
          f"{box['n']} connections opened")
    check("GET / opens few connections (read_session, #624)", box["n"] < 60, box["n"])
    check("the restricted, redacted and brand items are not in the page", all(
        data[k] not in html for k in ("cert", "redacted", "brand")))
    r2 = admin.get("/?rev=all")
    _, meta_all = embedded(r2.text)
    check("?rev=all: the page passes rev on to the endpoint and counts the old revisions",
          meta_all["rev"] == "all" and sum(c["all"] for c in meta_all["counts"].values()) == len(rows_all) + len(data["old"]))
    check("the Bulk file & tag link shows while unfiled files exist", "Bulk file &amp; tag" in html)

    # --- 2. every view, every page ---------------------------------------------------------------------
    print("--- 2. paging every view ---")
    orders = {}
    n_views = 0
    for sort in ("newest", "oldest", "az"):
        for tab in ["all"] + sorted(by_type):
            for filed in ("all", "unfiled", "filed") if tab in ("all", "image", "pdf") else ("all",):
                got, pages, last = walk(admin, type=tab, filed=filed, sort=sort)
                want, _, _ = oracle("admin", tab, filed, sort)
                n_views += 1
                orders[f"{tab}|{filed}|{sort}"] = got
                dup = len(got) - len(set(got))
                if sort == "az":
                    ok = sorted(got) == sorted(r["slug"] for r in want) and not dup
                    check(f"az {tab}/{filed}: every item exactly once ({len(got)} in {pages} pages)", ok, (dup, len(got), len(want)))
                else:
                    ok = got == [r["slug"] for r in want]
                    check(f"{sort} {tab}/{filed}: same items, same order as the old client ({len(got)} in {pages} pages)",
                          ok, (dup, len(got), len(want)))
    print(f"      {n_views} views walked")
    got, pages, _ = walk(admin, limit=7, type="stl", sort="newest")
    want, _, _ = oracle("admin", "stl", "all", "newest")
    check("limit=7 pages the same list (stl)", got == [r["slug"] for r in want] and pages == -(-len(want) // 7), (len(got), pages))
    body = api(admin, type="image", limit="500").json()
    check("limit=500 returns up to 500 and a cursor", len(body["items"]) == 500 and body["next_cursor"])
    body = api(admin, type="all").json()
    check("a page is the card payload (slug, display_name, uploaded_at, type_label, ...) and carries total",
          body["total"] == len(rows_all) and len(body["items"]) == 120
          and set(body["items"][0]) == set(__import__("core.card_payload", fromlist=["x"]).CARD_ITEM_FIELDS), sorted(body["items"][0]))
    check("unfiled_slugs of a page are exactly its unfiled items",
          set(body["unfiled_slugs"]) == {i["slug"] for i in body["items"]} & unfiled)
    # the superseded items: hidden by default, listed with rev=all
    flat = [s for v in orders.values() for s in v]
    check("older revisions are hidden by default", not (set(data["old"]) & set(orders["all|all|newest"])))
    got, _, _ = walk(admin, type="all", sort="newest", rev="all")
    want, _, _ = oracle("admin", "all", "all", "newest", show_all=True)
    check("rev=all lists them, same order as the old page", got == [r["slug"] for r in want] and set(data["old"]) <= set(got))

    # --- 3. the embedded batch + the endpoint = the whole list ---------------------------------------------
    print("--- 3. embedded batch continues into the endpoint ---")
    slugs = [i["slug"] for i in by_type_emb]
    cursor = meta["cursor"]
    while cursor:
        body = api(admin, type="all", filed="all", sort="newest", cursor=cursor).json()
        slugs += [i["slug"] for i in body["items"]]
        cursor = body["next_cursor"]
    check("All: embedded batch + endpoint == the old full list", slugs == [r["slug"] for r in rows_all], (len(slugs), len(rows_all)))
    for tab in sorted(by_type):  # a type tab's first click: its first page, no cursor
        body = api(admin, type=tab).json()
        want, _, _ = oracle("admin", tab, "all", "newest")
        check(f"{tab}: first page from the endpoint == the first 120 of the old list",
              [i["slug"] for i in body["items"]] == [r["slug"] for r in want[:120]] and body["total"] == len(want))

    # --- 4. who may see what ------------------------------------------------------------------------------------
    print("--- 4. nothing restricted leaks ---")
    hidden = {data["cert"], data["flagged"], data["redacted"], data["brand"], data["mine"]}
    vpage = viewer.get("/")
    check("viewer GET / 200", vpage.status_code == 200, vpage.status_code)
    check("viewer's page names none of the restricted / flagged / redacted / brand items",
          not any(s in vpage.text for s in (data["cert"], data["flagged"], data["redacted"], data["brand"])))
    leaked = []
    for sort in ("newest", "oldest", "az"):
        for tab in ["all", "image"]:
            for filed in ("all", "unfiled", "filed"):
                got, _, _ = walk(viewer, type=tab, filed=filed, sort=sort, rev="all")
                leaked += [s for s in got if s in {data["cert"], data["flagged"], data["redacted"], data["brand"]}]
    check("viewer: no page of any view contains them", not leaked, leaked)
    got, _, _ = walk(viewer, type="all", sort="newest")
    want, _, _ = oracle("viewer", "all", "all", "newest")
    check("viewer: the list is the old page's list for a viewer (incl. their own flagged item)",
          got == [r["slug"] for r in want] and data["mine"] in got and data["flagged"] not in got)
    r = api(viewer, type="certkey")
    check("viewer asking for the restricted type by name: 400 bad_type, which doesn't confirm it exists",
          r.status_code == 400 and err(r).get("code") == "bad_type" and "certkey" in err(r).get("message", "")
          and "restricted" not in err(r).get("message", ""), r.text[:200])
    got, _, _ = walk(admin, type="all", sort="newest")
    check("admin: sees the flagged item (the admin rule), never the restricted type, redacted or brand",
          data["flagged"] in got and data["mine"] in got and not {data["cert"], data["redacted"], data["brand"]} & set(got))
    body = api(viewer, type="all").json()
    check("viewer's total counts only what they can see", body["total"] == len(want), (body["total"], len(want)))

    # --- 5. refusals ----------------------------------------------------------------------------------------------------
    print("--- 5. anonymous and bad parameters ---")
    r = api(anon, type="all")
    check("anonymous: 401 unauthorized in the shared shape", r.status_code == 401 and err(r).get("code") == "unauthorized"
          and r.json().get("ok") is False and r.json().get("detail"), r.text[:200])
    r = TestClient(webapp.app, base_url=BASE, follow_redirects=False,
                   headers={"Authorization": "Bearer wrong-token"}).get("/api/home/files")
    check("a wrong install token: 401", r.status_code == 401, r.status_code)
    first = api(admin, type="image", sort="newest").json()
    good = first["next_cursor"]
    cases = [
        ("garbage cursor", dict(type="image", cursor="not-a-cursor!!"), "bad_cursor"),
        ("valid base64 but not ours", dict(type="image", cursor="eyJhIjoxfQ"), "bad_cursor"),
        ("negative offset", dict(type="image", cursor=__import__("base64").urlsafe_b64encode(
            json.dumps({"v": "image|all|newest|current", "o": -3, "s": "x"}).encode()).decode().rstrip("=")), "bad_cursor"),
        ("cursor from another sort", dict(type="image", sort="oldest", cursor=good), "cursor_view_mismatch"),
        ("cursor from another tab", dict(type="video", cursor=good), "cursor_view_mismatch"),
        ("cursor from another filter", dict(type="image", filed="unfiled", cursor=good), "cursor_view_mismatch"),
        ("cursor from the other revision mode", dict(type="image", rev="all", cursor=good), "cursor_view_mismatch"),
        ("unknown sort", dict(sort="sideways"), "bad_sort"),
        ("unknown filed mode", dict(filed="maybe"), "bad_filed"),
        ("unknown type", dict(type="hologram"), "bad_type"),
        ("limit 0", dict(limit="0"), "bad_limit"),
        ("limit 501", dict(limit="501"), "bad_limit"),
        ("limit not a number", dict(limit="lots"), "bad_limit"),
    ]
    for label, params, code in cases:
        r = api(admin, **params)
        e = err(r)
        check(f"{label}: 400 {code} with a message and detail", r.status_code == 400 and e.get("code") == code
              and len(e.get("message", "")) > 25 and r.json().get("detail") == e.get("message"), r.text[:240])
    r = api(admin, type="image", cursor="zzz")
    print("      sample bad_cursor message:", err(r).get("message"))
    r = api(admin, type="hologram")
    print("      sample bad_type message:  ", err(r).get("message"))
    body = api(admin, type="image", cursor=None).json()
    check("an empty cursor is the first page", body["items"][0]["slug"] == first["items"][0]["slug"])

    # --- 6. the list changing between two pages --------------------------------------------------------------------------------
    print("--- 6. changes between pages ---")
    p1 = api(admin, type="all", sort="newest", limit="50").json()
    with actor.acting_as(actor.ACTOR_SCRIPT):
        fresh = "fresh-" + secrets.token_hex(3)
        make_item(fresh, "image", "Uploaded between two pages")  # the newest file, lands before page 1's items
    p2 = api(admin, type="all", sort="newest", limit="50", cursor=p1["next_cursor"]).json()
    want, _, _ = oracle("admin", "all", "all", "newest")
    want = [r["slug"] for r in want]
    s1 = [i["slug"] for i in p1["items"]]
    check("a new upload at the top doesn't repeat page 1's last item on page 2",
          not (set(s1) & {i["slug"] for i in p2["items"]}))
    check("page 2 continues exactly after page 1's last item",
          [i["slug"] for i in p2["items"]] == want[want.index(s1[-1]) + 1: want.index(s1[-1]) + 51])
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("DELETE FROM capture_events WHERE slug = ?", (fresh,))  # fixture clean-up
        conn.commit()
    finally:
        conn.close()
    p1 = api(admin, type="all", sort="newest", limit="50").json()
    last = p1["items"][-1]["slug"]
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("DELETE FROM capture_events WHERE slug = ?", (last,))
        conn.commit()
    finally:
        conn.close()
    r = api(admin, type="all", sort="newest", limit="50", cursor=p1["next_cursor"])
    check("the last item served going away: the cursor falls back to its offset (200, no error)",
          r.status_code == 200 and len(r.json()["items"]) == 50, r.text[:200])

    if dump:
        os.makedirs(dump, exist_ok=True)
        # the old client's inputs (the whole embed, per type) for the Node localeCompare check
        with actor.acting_as(actor.ACTOR_SCRIPT):
            from web.shapes import _card_items  # noqa: E402
            old_full = {}
            for mt, rows in db.list_recent_items_by_type(limit_per_type=10000, include_superseded=True).items():
                rows, _ = _split_revisions(policy.filter_visible(rows), False)
                if rows:
                    old_full[mt] = _card_items(rows)
            unfiled_now = sorted(r["slug"] for r in db.list_unfiled_items(include_superseded=True))
        with open(os.path.join(dump, "old_full.json"), "w") as f:
            json.dump({"files_by_type": old_full, "unfiled_slugs": unfiled_now}, f)
        # re-walk against the same state as old_full (the fixture edits above changed it)
        views = {}
        for sort in ("newest", "oldest", "az"):
            for tab in ["all"] + sorted(old_full):
                for filed in ("all", "unfiled", "filed"):
                    got, _, _ = walk(admin, type=tab, filed=filed, sort=sort)
                    views[f"{tab}|{filed}|{sort}"] = got
        with open(os.path.join(dump, "new_orders.json"), "w") as f:
            json.dump(views, f)
        print(f"      wrote {len(views)} view orders and the old full payload to {dump}")

    print(f"\n{len(FAILS)} failure(s)")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        sys.exit(1)


main()
