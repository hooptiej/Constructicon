#!/usr/bin/env python3
"""Caption clean-up for a running install (#743 mid-word captions, #542 short-title backfill).

Talks HTTP to the web app with the install token (scripts/_http.py), like the other maintenance
tools; run it beside the web container from the install's checkout (CLAUDE.md, "Scripts: what runs
where"):

    ./scripts/run_tool.sh <web container> scripts/caption_cleanup.py --base-url http://localhost:80 scan
    ./scripts/run_tool.sh <web container> scripts/caption_cleanup.py --base-url http://localhost:80 requeue-midword
    ./scripts/run_tool.sh <web container> scripts/caption_cleanup.py --base-url http://localhost:80 requeue-midword --execute
    ./scripts/run_tool.sh <web container> scripts/caption_cleanup.py --base-url http://localhost:80 propose-titles [--limit N]
    ./scripts/run_tool.sh <web container> scripts/caption_cleanup.py --base-url http://localhost:80 propose-titles --execute

  scan              READ-ONLY. Every `done` caption suggestion that starts mid-word ("urn with...",
                    "ids and..."): the count, by model, how many were also accepted as the
                    description, and examples (GET /api/captions/mid-word).
  requeue-midword   Puts them back in the needs-caption queue as ONE undoable batch
                    (POST /api/captions/mid-word/requeue). Dry run unless --execute.
  propose-titles    Has the local captioner propose a short title for every item whose title used
                    to be its long caption; proposals wait in /captions/review, nothing is applied
                    (POST /api/captions/propose-titles). Dry run unless --execute.

A real run prints the batch id; undo it with POST /api/changes/<batch>/undo (or constructicon_undo).
Write runs go to constructicon-test before production, as always. Every failure prints what was
tried, what happened and what to do, and exits 1.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def call(base, method, path, form=None):
    url = base.rstrip("/") + path
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body).get("error") or {}
            msg = f"{err.get('code')}: {err.get('message')}"
        except ValueError:
            msg = body[:300]
        raise SystemExit(f"FAILED: {method} {url} answered HTTP {e.code} ({msg}). Nothing else was tried.")
    except urllib.error.URLError as e:
        raise SystemExit(f"FAILED: couldn't reach {url} ({e.reason}). Is the instance running and --base-url right?")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True, help="the web app, e.g. http://localhost:80 beside the container")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="read-only: list done captions that start mid-word")
    rq = sub.add_parser("requeue-midword", help="re-queue them as one undoable batch (dry run unless --execute)")
    rq.add_argument("--execute", action="store_true")
    pt = sub.add_parser("propose-titles", help="queue short-title proposals (dry run unless --execute)")
    pt.add_argument("--execute", action="store_true")
    pt.add_argument("--limit", type=int, default=2000)
    pt.add_argument("--examples", type=int, default=10)
    ap.add_argument("--show", type=int, default=10, help="how many examples to print (scan)")
    args = ap.parse_args(argv)

    import _http  # #467 step 2: send the install token to the app (scripts/_http.py)
    _http.install(args.base_url)

    if args.cmd == "scan":
        r = call(args.base_url, "GET", "/api/captions/mid-word")
        print(f"mid-word done captions: {r['count']} (by model: {r['by_model']}; also accepted as the description: "
              f"{r['accepted']})")
        for it in r["items"][:args.show]:
            print(f"  {it['slug']}  model={it['model']} step={it['step']}  {it['caption'][:70]!r}")
        print("OK: read-only scan, nothing written")
    elif args.cmd == "requeue-midword":
        r = call(args.base_url, "POST", "/api/captions/mid-word/requeue", {"dry_run": "false" if args.execute else "true"})
        verb = "re-queued" if args.execute else "would re-queue"
        print(f"{verb} {r['count']} mid-word caption(s)" + (f"; batch {r['batch_id']}" if r.get("batch_id") else ""))
        print("OK" + ("" if args.execute else ": dry run, nothing written (add --execute)"))
    else:
        r = call(args.base_url, "POST", "/api/captions/propose-titles",
                 {"dry_run": "false" if args.execute else "true", "limit": str(args.limit)})
        verb = "queued" if args.execute else "would queue"
        print(f"{verb} {r['count']} short-title proposal(s)" + (f"; batch {r['batch_id']}" if r.get("batch_id") else ""))
        for ex in r.get("examples", [])[:args.examples]:
            print(f"  {ex['slug']}  now titled {ex['title']!r}; description {ex['description'][:60]!r}")
        print("OK" + ("" if args.execute else ": dry run, nothing written (add --execute)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
