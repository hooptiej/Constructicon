#!/usr/bin/env python3
"""Measure the look-alike pass (#759) on a READ-ONLY copy of a Constructicon database.

Opens the SQLite file with a `mode=ro` URI (nothing is ever written; no migrations run) and uses only
the standard library plus core/lookalike_match.py (stdlib only), so it runs inside the app image with
no network and no app import side effects. It prints:

  * images considered, how many have a usable perceptual hash;
  * the distance histogram of every pair within --histogram bits (to choose the threshold);
  * candidate groups at --max-distance (default lookalike_match.MAX_DISTANCE) and their size histogram;
  * how many groups were excluded entirely as byte-identical (#615's job), as a revision chain (#477)
    or as already answered "Not the same", and how many pairs each reason removed;
  * one line per candidate group (first --show, default 60): slugs, file names and pairwise distances.

Byte-identical needs the stored files: pass --storage <dir> (default: the CONSTRUCTICON_STORAGE_DIR
environment variable, else <db dir>/../storage). If no storage folder is found it says so and
counts no pair as byte-identical (never silently).

    python3 scripts/measure_lookalikes_759.py /path/to/copy-of-constructicon.db [--storage DIR]
        [--max-distance 6] [--histogram 12] [--show 60]

Exit status: 0 on success, 2 when the database can't be opened or has no capture_events table.
"""

import argparse
import hashlib
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import lookalike_match as lm  # noqa: E402  (stdlib only; core/__init__.py imports nothing)

IMAGE_CLAUSE = "(media_type = 'image' OR media_type IS NULL OR media_type = '')"  # = db.LOOKALIKE_IMAGE_CLAUSE


def _open_ro(path):
    """A read-only connection (mode=ro). A WAL database in a read-only folder can't be opened that way
    (SQLite wants to create its -shm file), so on that error it retries with immutable=1, which is right
    for a COPY that nothing else is writing, and says so."""
    base = pathlib.Path(os.path.abspath(path)).as_uri()  # file:///..., percent-encoded
    try:
        conn = sqlite3.connect(base + "?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM capture_events LIMIT 1")
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise
        print(f"note: mode=ro failed ({e}); opening with immutable=1 (only correct for a copy nothing writes to)")
        conn = sqlite3.connect(base + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db_path")
    ap.add_argument("--storage", default=None)
    ap.add_argument("--max-distance", type=int, default=lm.MAX_DISTANCE)
    ap.add_argument("--histogram", type=int, default=12, help="histogram pairs up to this many bits")
    ap.add_argument("--show", type=int, default=60, help="print at most this many groups")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.db_path):
        print(f"ERROR: no database file at {a.db_path}", file=sys.stderr)
        return 2
    try:
        conn = _open_ro(a.db_path)
        rows = [dict(r) for r in conn.execute(
            "SELECT slug, filename, stored_filename, perceptual_hash FROM capture_events "
            "WHERE COALESCE(redacted, 0) = 0 AND stored_filename IS NOT NULL AND stored_filename != '' "
            f"AND {IMAGE_CLAUSE} ORDER BY id")]
        revs = {r[0]: r[1] for r in conn.execute("SELECT old_slug, new_slug FROM item_revisions")}
        dismissed = set()
        try:
            for (key,) in conn.execute("SELECT nudge_key FROM curator_dismissals WHERE action = 'dismiss' "
                                       "AND nudge_key LIKE 'need:lookalike_pair:%'"):
                x, _, y = key[len("need:lookalike_pair:"):].partition(":")
                if x and y:
                    dismissed.add(lm.pair(x, y))
        except sqlite3.OperationalError as e:
            print(f"note: no curator_dismissals table ({e}); no earlier answers counted")
    except sqlite3.Error as e:
        print(f"ERROR: can't read {a.db_path}: {e}", file=sys.stderr)
        return 2

    storage = a.storage or os.environ.get("CONSTRUCTICON_STORAGE_DIR") or \
        os.path.join(os.path.dirname(os.path.abspath(a.db_path)), "..", "storage")
    have_storage = os.path.isdir(storage)
    stored = {r["slug"]: r["stored_filename"] for r in rows}
    unreadable = {}

    def sig(slug):
        p = os.path.join(storage, stored[slug])
        try:
            return os.path.getsize(p), _sha(p)
        except OSError as e:
            unreadable[slug] = str(e)
            return None

    def identical(x, y):
        sx, sy = sig(x), sig(y)
        return sx is not None and sx == sy

    hashes = {r["slug"]: r["perceptual_hash"] for r in rows if r["perceptual_hash"]}
    built = lm.build(hashes, chains=lm.chain_index(revs), dismissed=dismissed,
                     identical=identical if have_storage else None, max_distance=a.max_distance)

    print(f"images considered: {len(rows)}; with a perceptual hash: {len(hashes)}; usable (not degenerate): "
          f"{built['hashed']}; degenerate: {built['degenerate']}")
    if not have_storage:
        print(f"WARNING: storage folder not found ({storage}); byte-identical pairs can't be told apart and are "
              "counted as look-alikes. Pass --storage.")

    ints = {s: lm.hash_int(h) for s, h in hashes.items()}
    ints = {s: v for s, v in ints.items() if v is not None}
    hist_pairs = lm.candidate_pairs(ints, min(a.histogram, 63))
    hist = {}
    for d in hist_pairs.values():
        hist[d] = hist.get(d, 0) + 1
    print(f"pair distance histogram (bits of 64, all pairs <= {a.histogram}, before exclusions): "
          + ", ".join(f"{d}:{hist.get(d, 0)}" for d in range(a.histogram + 1)))

    groups = built["groups"]
    sizes = {}
    for g in groups:
        sizes[len(g["members"])] = sizes.get(len(g["members"]), 0) + 1
    print(f"candidate groups at max distance {a.max_distance}: {len(groups)} "
          f"({sum(len(g['members']) for g in groups)} images); sizes: "
          + (", ".join(f"{k} photos x{v}" for k, v in sorted(sizes.items())) or "none"))
    ep, eg = built["excluded_pairs"], built["excluded_groups"]
    print(f"excluded groups: byte-identical {eg['byte_identical']}, revision chain {eg['revision_chain']}, "
          f"answered 'not the same' {eg['dismissed']}, several reasons {eg['several']}; excluded pairs: "
          f"byte-identical {ep['byte_identical']}, revision chain {ep['revision_chain']}, answered {ep['dismissed']}")
    if unreadable:
        print(f"unreadable stored files (not counted as byte-identical): {len(unreadable)}; e.g. "
              + "; ".join(f"{s}: {r}" for s, r in list(unreadable.items())[:3]))

    names = {r["slug"]: r["filename"] for r in rows}
    for i, g in enumerate(groups[:a.show], 1):
        dists = " ".join(f"{x}~{y}={d}" for (x, y), d in sorted(g["pairs"].items(), key=lambda kv: kv[1]))
        files = " | ".join(f"{s} {names.get(s) or '?'}" for s in g["members"])
        extra = f" (answered before: {len(g['answered'])})" if g["answered"] else ""
        print(f"{i:>3}. {files} :: {dists}{extra}")
    if len(groups) > a.show:
        print(f"... {len(groups) - a.show} more groups not shown (--show N)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
