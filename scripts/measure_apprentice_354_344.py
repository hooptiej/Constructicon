#!/usr/bin/env python3
"""Measure the Curator's Apprentice (#354 provenance rules, #344 project correlations) on a READ-ONLY copy
of a Constructicon database.

Opens the SQLite file with a `mode=ro` URI (nothing is ever written; no migrations run) and uses only the
standard library plus core/apprentice_rules.py and core/timeline.py (both stdlib only; numpy, if present,
only speeds the cosine step up), so it runs inside the app image with no network. It runs the SAME
loaders and rules the app does, and prints:

  * per provenance rule: files considered (its media types, provenance NOT set, not redacted), how many
    the rule matches, how many were answered "not this one", the split by test (text-heavy vs a manual
    name), a histogram of their text lengths, a sample of proposals AND of near misses (files just under
    the text threshold, and hint-named files with too little text) to judge the thresholds;
  * correlations: cards considered and with an embedding, pairs scored / excluded (by reason) /
    answered "not related" / under the threshold, the proposals, a score histogram of every scored pair,
    and a sample of proposals with their parts (text, tags, time, shared) and the plain-words why.

Tune with the flags (each overrides the module constant for this run only):

    python3 scripts/measure_apprentice_354_344.py /path/to/copy-of-constructicon.db
        [--text-min-chars 6000] [--hint-min-chars 300] [--threshold 0.45] [--show 25]

Exit status: 0 on success, 2 when the database can't be opened or has no capture_events table.
"""

import argparse
import os
import pathlib
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import apprentice_rules as ar  # noqa: E402  (stdlib only; core/__init__.py imports nothing)


def _open_ro(path):
    """A read-only connection (mode=ro). A WAL database in a read-only folder can't be opened that way
    (SQLite wants to create its -shm file), so on that error it retries with immutable=1, which is right
    for a COPY that nothing else is writing, and says so."""
    base = pathlib.Path(os.path.abspath(path)).as_uri()
    try:
        conn = sqlite3.connect(base + "?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM capture_events LIMIT 1")
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise
        print(f"note: mode=ro failed ({e}); opening with immutable=1 (only correct for a copy nothing writes to)")
        conn = sqlite3.connect(base + "?mode=ro&immutable=1", uri=True)
        conn.execute("SELECT 1 FROM capture_events LIMIT 1")
    return conn


def _bucket(n, edges):
    for e in edges:
        if n < e:
            return f"< {e:,}"
    return f">= {edges[-1]:,}"


def measure_provenance(conn, show):
    print("=" * 78)
    print("#354 FILE PROVENANCE BY RULE")
    for rule in ar.FILE_RULES:
        props, counts = ar.propose_files(conn, rule)
        print(f"\nrule {rule.key} -> provenance {rule.provenance!r}: {rule.explain}")
        print(f"  thresholds: TEXT_HEAVY_MIN_CHARS={ar.TEXT_HEAVY_MIN_CHARS:,}  MANUAL_HINT_MIN_CHARS={ar.MANUAL_HINT_MIN_CHARS:,}")
        print(f"  files considered (types {', '.join(rule.media_types)}, provenance not set): {counts['considered']}")
        print(f"  matched: {counts['matched']}  answered 'not this one': {counts['answered_not_this']}  "
              f"PROPOSED: {len(props)}")
        by_test = Counter(p["evidence"]["test"] for p in props)
        print(f"  by test: text-heavy {by_test.get('text_heavy', 0)}, manual name {by_test.get('manual_name', 0)}; "
              f"with a name hint {sum(1 for p in props if p['evidence'].get('hint'))}")
        by_type = Counter(p.get("media_type") for p in props)
        print("  by type: " + ", ".join(f"{t} {n}" for t, n in by_type.most_common()))
        rows = ar.load_provenance_candidates(conn, rule)
        edges = [500, 2000, 6000, 20000, 100000]
        hist = Counter(_bucket(int(r.get("text_chars") or 0), edges) for r in rows)
        print("  text length of every file considered:")
        for label in [f"< {e:,}" for e in edges] + [f">= {edges[-1]:,}"]:
            print(f"    {label:>10}  {hist.get(label, 0)}")
        print(f"  sample of proposals (first {show}):")
        for p in props[:show]:
            print(f"    {p['slug']}  {p.get('media_type'):5}  {(p.get('chars') or 0):>8,} chars  "
                  f"{(p.get('display_name') or p.get('filename') or '')[:60]}")
        near = sorted((r for r in rows if r.get("media_type") == "pdf" and rule.test(r) is None
                       and int(r.get("text_chars") or 0) >= ar.TEXT_HEAVY_MIN_CHARS // 3),
                      key=lambda r: -int(r.get("text_chars") or 0))
        print(f"  near misses: PDFs with {ar.TEXT_HEAVY_MIN_CHARS // 3:,}..{ar.TEXT_HEAVY_MIN_CHARS - 1:,} chars "
              f"({len(near)}), first {min(show, len(near))}:")
        for r in near[:show]:
            print(f"    {r['slug']}  {int(r.get('text_chars') or 0):>8,} chars  {(r.get('display_name') or r.get('filename') or '')[:60]}")
        thin = [r for r in rows if ar.manual_hint(r.get("filename"), r.get("display_name"))
                and int(r.get("text_chars") or 0) < ar.MANUAL_HINT_MIN_CHARS]
        print(f"  name says manual/guide/... but under {ar.MANUAL_HINT_MIN_CHARS} chars (not proposed): {len(thin)}")
        for r in thin[:show]:
            print(f"    {r['slug']}  {int(r.get('text_chars') or 0):>6,} chars  {(r.get('display_name') or r.get('filename') or '')[:60]}")


def measure_correlations(conn, show):
    print()
    print("=" * 78)
    print("#344 PROJECT CORRELATIONS")
    data = ar.load_correlation_inputs(conn)
    pairs, counts = ar.propose_pairs(data, keep_below=True)
    print(f"  weights: text {ar.W_TEXT}, tags {ar.W_TAGS}, time {ar.W_TIME}, shared files {ar.W_SHARED}; "
          f"EMB_FLOOR {ar.EMB_FLOOR}, EMB_FULL {ar.EMB_FULL}, TAGS_FULL {ar.TAGS_FULL}, SHARED_FULL {ar.SHARED_FULL}, "
          f"TIME_SLACK_DAYS {ar.TIME_SLACK_DAYS}, TAG_COMMON_SHARE {ar.TAG_COMMON_SHARE}")
    print(f"  SCORE_THRESHOLD {counts['threshold']}")
    print(f"  cards in the database: {len(data['cards'])}; considered (kinds {', '.join(ar.CORRELATION_KINDS)}): "
          f"{counts['cards_considered']}; with an embedding centroid: {counts['with_embedding']}")
    print(f"  pairs scored: {counts['pairs_scored']}; excluded: "
          + (", ".join(f"{k} {v}" for k, v in sorted(counts["excluded"].items())) or "none"))
    print(f"  under the threshold (or time-only): {counts['below_threshold']}; answered 'not related': "
          f"{counts['answered_not_related']}; PROPOSED: {counts['proposed']}")
    scores = counts["below_scores"] + [p["score"] for p in pairs]
    hist = Counter(min(9, int(s * 10)) for s in scores)
    print("  score histogram (every scored pair with evidence or not):")
    for b in range(10):
        lo, hi = b / 10, (b + 1) / 10
        mark = "  <- threshold" if lo <= counts["threshold"] < hi else ""
        print(f"    {lo:.1f}-{hi:.1f}  {hist.get(b, 0)}{mark}")
    print(f"  top {show} proposals:")
    for p in pairs[:show]:
        pt = p["parts"]
        print(f"    {p['score']:.3f}  text {pt['text']:.2f} tags {pt['tags']:.2f} time {pt['time']:.2f} "
              f"shared {pt['shared']:.2f}  {p['a']} <-> {p['b']}")
        print(f"           {p['why']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--text-min-chars", type=int, default=ar.TEXT_HEAVY_MIN_CHARS)
    ap.add_argument("--hint-min-chars", type=int, default=ar.MANUAL_HINT_MIN_CHARS)
    ap.add_argument("--threshold", type=float, default=ar.SCORE_THRESHOLD)
    ap.add_argument("--show", type=int, default=25)
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"error: no database at {args.db}", file=sys.stderr)
        return 2
    try:
        conn = _open_ro(args.db)
    except sqlite3.Error as e:
        print(f"error: can't open {args.db} read-only: {e}", file=sys.stderr)
        return 2
    ar.TEXT_HEAVY_MIN_CHARS = args.text_min_chars
    ar.MANUAL_HINT_MIN_CHARS = args.hint_min_chars
    ar.SCORE_THRESHOLD = args.threshold
    # propose_pairs' default threshold was bound at import; pass the flag explicitly.
    _orig = ar.propose_pairs
    ar.propose_pairs = lambda data, threshold=args.threshold, keep_below=False: _orig(data, threshold, keep_below)
    print(f"database: {args.db} (read-only)")
    measure_provenance(conn, args.show)
    measure_correlations(conn, args.show)
    return 0


if __name__ == "__main__":
    sys.exit(main())
