#!/usr/bin/env python3
"""Checks for the cheap Curator badge and lazy queue (#524). Run INSIDE the app container, against
the app on localhost:80, on a COPY of real data (constructicon-test), never production:

    docker exec constructicon-test python3 scripts/check_curator_queue.py [--before /path/queue-before.json]

It asserts (exit 1 on any failure):
  * the badge's count (?summary=1) equals the open, non-deferred items of the full queue, and
    equals what an uncached build_queue() says;
  * every group's lazily fetched slice (?card= and the fragment's ?group=) holds exactly the
    items the full queue's group holds, in the same order;
  * after a Defer / Bring back / Dismiss / Accept (and undo) the badge changes by exactly the
    right amount on the very next call (the cache never serves a stale count);
  * with --before (a build_queue() JSON dumped from `main` on the same DB), the grouping and
    ordering are unchanged and the only difference is the documented dedupe (nudge
    `missing_writeup` + need `blank_writeup_with_files` on one card -> one item).
Writes it makes are undone; run it on a restorable copy anyway."""

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/app")
from core import curation_queue as cq, db  # noqa: E402

BASE = "http://localhost:80"
FAILS = []


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path).read())


def get_text(path):
    r = urllib.request.urlopen(BASE + path)
    return r.status, r.read().decode("utf-8")


def post(path, **form):
    data = urllib.parse.urlencode(form).encode()
    return json.loads(urllib.request.urlopen(urllib.request.Request(BASE + path, data=data)).read())


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def timed_ms(path):
    t = time.time()
    urllib.request.urlopen(BASE + path).read()
    return (time.time() - t) * 1000


def badge():
    return get("/api/curator/queue?summary=1")["counts"]


def open_items(q):
    return [i for g in q["groups"] for i in g["items"]]


def norm(x):
    return json.loads(json.dumps(x, sort_keys=True, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", help="build_queue() JSON from main on this same DB")
    ap.add_argument("--after", help="build_queue() JSON from this code on the same DB snapshot as --before")
    args = ap.parse_args()
    import _http  # #467 step 2: send the install token to the app (scripts/_http.py)
    _http.install(BASE)

    # 1. badge == full queue ------------------------------------------------------------
    truth = norm(cq.build_queue())
    full = get("/api/curator/queue")
    b = badge()
    flat = open_items(full)
    check("badge counts == uncached build_queue counts", b == truth["counts"], f"{b} vs {truth['counts']}")
    check("badge open == len(open, non-deferred items in the full queue)", b["open"] == len(flat) and not any(i["deferred"] for i in flat), f"{b['open']} vs {len(flat)}")
    check("badge deferred == deferred items", b["deferred"] == sum(len(g["items"]) for g in full["deferred"]) and all(i["deferred"] for g in full["deferred"] for i in g["items"]))
    check("cached full queue == uncached build", norm(full) == truth)
    check("open == questions + nudges + needs", b["open"] == b["questions"] + b["nudges"] + b["needs"])

    # 2. lazy slices == the full queue's groups ---------------------------------------
    bad = []
    n_checked = 0
    for section, key in (("open", "groups"), ("deferred", "deferred")):
        for g in full[key]:
            if g["type"] == "card":
                sl = get("/api/curator/queue?card=" + urllib.parse.quote(g["slug"]))
                match = [x for x in sl[key] if x["id"] == g["id"]]
                if not match or norm(match[0]["items"]) != norm(g["items"]):
                    bad.append(f"{section}:{g['id']}")
            status, html = get_text("/api/curator/queue/html?group=" + urllib.parse.quote(g["id"]) + "&section=" + section)
            rows = len(re.findall(r'<li class="cq-item', html))
            if status != 200 or rows != len(g["items"]):
                bad.append(f"fragment {section}:{g['id']} ({rows} rows vs {len(g['items'])})")
            n_checked += 1
    check(f"every group's lazy slice + fragment matches the full queue ({n_checked} groups)", not bad, ", ".join(bad[:5]))
    status, shell = get_text("/api/curator/queue/html")
    check("shell fragment renders collapsed groups with counts", status == 200 and shell.count("cq-group-toggle") >= len(full["groups"]) and "cq-item" not in shell.replace("cq-items", ""),
          f"{len(shell)} bytes")

    # 3. grouping / ordering unchanged versus main ------------------------------------
    if args.before:
        before = json.load(open(args.before))
        # --after: a build_queue() dump of THIS code on the same DB snapshot as --before. Use it
        # when the DB is moving under you (someone browsing the test box); otherwise the live
        # queue is compared.
        after = json.load(open(args.after)) if args.after else norm(full)
        after_counts = after["counts"]
        for section in ("groups", "deferred"):
            check(f"{section}: same groups in the same order", [g["id"] for g in before[section]] == [g["id"] for g in after[section]])
        removed, changed = [], []
        for section in ("groups", "deferred"):
            for gb, ga in zip(before[section], after[section]):
                keys_after = [i["key"] for i in ga["items"]]
                kept = [i for i in gb["items"] if i["key"] in keys_after]
                gone = [i for i in gb["items"] if i["key"] not in keys_after]
                removed += [(gb["id"], i["key"], i["kind"], section) for i in gone]
                check(f"{section}/{gb['id']}: remaining items in the same order", [i["key"] for i in kept] == keys_after)
                for ib, ia in zip(kept, ga["items"]):
                    if ib != ia:
                        changed.append((gb["id"], ia["key"], [k for k in ia if ia.get(k) != ib.get(k)]))
        print(f"INFO  dedupe removed {len(removed)} item(s):")
        for r in removed:
            print("        ", r)
        print(f"INFO  {len(changed)} kept item(s) changed (expected: only `detail` on the surviving nudge):")
        for c in changed[:5]:
            print("        ", c)
        check("only removed items are need:blank_writeup_with_files", all(r[2] == "blank_writeup_with_files" for r in removed))
        check("only changed field is detail, on missing_writeup nudges", all(c[2] == ["detail"] and c[1].startswith("missing_writeup:") for c in changed))
        check("open count differs by exactly the removed open items",
              before["counts"]["open"] - after_counts["open"] == sum(1 for r in removed if r[3] == "groups"),
              f"{before['counts']['open']} -> {after_counts['open']}")

    # 4. defer / bring back / dismiss never leave a stale badge --------------------------
    target = next((i for i in flat if i["type"] == "nudge"), None) or next((i for i in flat if i["type"] == "need"), None)
    cold = []
    if target:
        k = target["key"]
        post("/api/curator/queue/defer", key=k)
        t = time.time(); b2 = badge(); cold.append((time.time() - t) * 1000)
        check("defer: badge open -1, deferred +1 on the very next call", b2["open"] == b["open"] - 1 and b2["deferred"] == b["deferred"] + 1, f"{b['open']}/{b['deferred']} -> {b2['open']}/{b2['deferred']}")
        grp = get_text("/api/curator/queue/html?group=" + urllib.parse.quote(next(g["id"] for g in full["groups"] if target in g["items"])) + "&section=deferred")
        check("deferred item shows under the Deferred section with Bring back", k in grp[1] and "Bring back" in grp[1])
        post("/api/curator/queue/bring-back", key=k)
        t = time.time(); b3 = badge(); cold.append((time.time() - t) * 1000)
        check("bring back: badge restored", b3 == b, f"{b3}")
        # dismiss + undo
        before_log = db.list_change_log(limit=1)
        post("/api/curator/needs/dismiss", nudge_key=k)
        b4 = badge()
        check("dismiss: badge open -1, deferred unchanged", b4["open"] == b["open"] - 1 and b4["deferred"] == b["deferred"], f"{b4}")
        row = db.list_change_log(limit=1)[0]
        if row["id"] != (before_log[0]["id"] if before_log else None):
            post(f"/api/changes/{row['batch_id'] or row['id']}/undo")
        b5 = badge()
        check("undo of the dismiss: badge restored", b5 == b, f"{b5}")
    else:
        check("a nudge/need to exercise Defer", False)

    # 5. accept a suggested question, then undo it ----------------------------------------
    qn = next((i for i in flat if i["type"] == "question" and i["suggested"]["picks"] and i["kind"].startswith("card_")), None)
    if qn:
        pre = db.list_change_log(limit=1)
        pre_id = pre[0]["id"] if pre else 0
        post(f"/api/pending-decisions/{qn['id']}/resolve", choice=qn["suggested"]["picks"][0])
        b6 = badge()
        keys_now = {i["key"] for i in open_items(get("/api/curator/queue"))}
        check("accept: the question left the queue", qn["key"] not in keys_now)
        # Accepting can legitimately raise new items (a new kind brings its own needs), so the
        # badge is checked against a fresh uncached build, not against open - 1.
        check("accept: badge == a fresh uncached build (cache not stale)", b6 == norm(cq.build_queue())["counts"], f"{b['open']} -> {b6['open']} ({qn['kind']})")
        rows = [r for r in db.list_change_log(limit=20) if r["id"] > pre_id]
        batches = []
        for r in rows:
            bid = r["batch_id"] or r["id"]
            if bid not in batches:
                batches.append(bid)
        undone = 0
        for bid in batches:
            try:
                post(f"/api/changes/{bid}/undo")
                undone += 1
            except urllib.error.HTTPError as e:
                print("INFO  undo", bid, "->", e.code, e.read()[:120])
        b7 = badge()
        check("undo of the accept: question is back and the badge is restored", b7["open"] == b["open"], f"{b7['open']} vs {b['open']} (undid {undone} batch(es))")
    else:
        print("INFO  no open card question with a suggestion to Accept; skipped")

    # 6. pages ---------------------------------------------------------------------------
    for p in ("/", "/admin", "/curator", "/project/clod-a-pede", "/api/pending-decisions", "/api/curator/dashboard", "/api/curator/needs"):
        try:
            status = urllib.request.urlopen(BASE + p).status
        except urllib.error.HTTPError as e:
            status = e.code
        check(f"GET {p} -> 200", status == 200, str(status))

    # timings ------------------------------------------------------------------------------
    print("INFO  cold badge (first call after a write), ms:", [round(c) for c in cold])
    hits = [timed_ms("/api/curator/queue?summary=1") for _ in range(5)]
    print(f"INFO  warm badge median {statistics.median(hits):.0f} ms")

    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
