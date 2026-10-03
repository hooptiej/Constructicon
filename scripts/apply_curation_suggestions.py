"""Issue #503: rewrite the SUGGESTED answer of open V2 card decisions from a reviewed file.

The V2 card migration (#486) queues owner decisions (card_status, card_kind,
card_built_for, card_family_members) with weak, deterministic suggestions. The
curation pass in data/v2c_curation_suggestions.json replaces those with reviewed
suggestions, each with a confidence and a one-line evidence note. This script
copies them onto the queue.

What it changes, and what it never touches
------------------------------------------
For every OPEN `card:<slug>` decision whose card slug + decision kind appear in the
file, it rewrites only these payload fields:
    suggested          the suggested option key (a list of keys for card_family_members)
    suggested_reason   the evidence note
    confidence         high / medium / low
plus, for multi-pick questions whose options carry a per-option `suggested` flag
(card_family_members), that flag, so the pre-ticked boxes match `suggested`.

It never resolves a decision, never touches an answered (resolved) one, never
changes options, patches, the provisional value or the question. Decisions are
matched by card slug + kind, not by id (ids differ between installs). Slugs or
kinds in the file with no open decision are skipped and listed; so are open card
decisions the file has no entry for. A suggestion whose key is not one of the
decision's options is skipped and reported.

DRY-RUN BY DEFAULT: prints before -> after for every decision that would change.
--execute applies the changes in one transaction and records one change-log row
(audit_log, op `curation_503_suggestions`, actor `migration`) with before/after row
images of each payload, so the batch can be undone like any other card write.
Idempotent: a second run finds nothing to change.

Usage
-----
    python scripts/apply_curation_suggestions.py              # dry run
    python scripts/apply_curation_suggestions.py --execute    # apply (take a backup first)
    python scripts/apply_curation_suggestions.py --file other.json --db /path/to/imagerepo.db

The DB path is --db, else $CONSTRUCTICON_DB_PATH, else core.db's default.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_FILE = os.path.join(ROOT, "data", "v2c_curation_suggestions.json")
OP = "curation_503_suggestions"
CONFIDENCES = ("high", "medium", "low")


def load_suggestions(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("header", {}), doc.get("suggestions", {})


def new_payload(payload, entry):
    """The payload with only the suggestion fields replaced (a new dict)."""
    out = dict(payload)
    out["suggested"] = entry["suggested"]
    out["suggested_reason"] = entry["evidence"]
    out["confidence"] = entry["confidence"]
    if isinstance(entry["suggested"], list):
        picks = set(entry["suggested"])
        opts = []
        for o in payload.get("options", []):
            if "suggested" in o:
                o = dict(o, suggested=o["key"] in picks)
            opts.append(o)
        out["options"] = opts
    return out


def check_entry(entry, payload):
    """Returns an error string, or None when the entry fits this decision."""
    if entry.get("confidence") not in CONFIDENCES:
        return f"confidence must be one of {CONFIDENCES}"
    if not entry.get("evidence"):
        return "no evidence note"
    keys = {o["key"] for o in payload.get("options", [])}
    sug = entry.get("suggested")
    picks = sug if isinstance(sug, list) else [sug]
    if not picks or any(p not in keys for p in picks):
        return f"suggested {sug!r} is not one of the options {sorted(keys)}"
    return None


def short(payload):
    return f"{payload.get('suggested')!r} ({payload.get('confidence')}): {payload.get('suggested_reason')}"


def plan(db, cards, suggestions):
    """Read-only. Returns (changes, report) where changes is a list of
    (decision, new_payload) and report holds the skipped/unchanged lists."""
    report = {"unchanged": [], "bad_entry": [], "no_entry": [], "not_open": []}
    changes = []
    matched = set()
    for d in db.list_pending_decisions():
        if d["kind"] not in cards.CARD_DECISION_KINDS or not d["post_slug"].startswith(cards.CARD_DECISION_PREFIX):
            continue
        slug = d["post_slug"][len(cards.CARD_DECISION_PREFIX):]
        entry = suggestions.get(slug, {}).get(d["kind"])
        if entry is None:
            report["no_entry"].append(f"{slug} / {d['kind']} (decision {d['id']})")
            continue
        matched.add((slug, d["kind"]))
        err = check_entry(entry, d["payload"])
        if err:
            report["bad_entry"].append(f"{slug} / {d['kind']}: {err}")
            continue
        after = new_payload(d["payload"], entry)
        if after == d["payload"]:
            report["unchanged"].append(f"{slug} / {d['kind']}")
            continue
        changes.append((d, after))
    for slug, kinds in suggestions.items():
        for kind in kinds:
            if (slug, kind) not in matched:
                report["not_open"].append(f"{slug} / {kind}")
    return changes, report


def apply(db, changes_mod, changes):
    """Writes every change in ONE transaction with one change-log row. Re-checks that
    each decision is still open and unchanged inside the transaction."""
    batch = changes_mod.new_batch_id()
    written, skipped = 0, []
    with db.transaction() as tx:
        il = db.ImageLog(OP, changes_mod.ACTOR_MIGRATION, batch,
                         affected_slugs=[d["post_slug"][5:] for d, _ in changes], conn=tx.conn)
        for d, after in changes:
            row = tx.conn.execute("SELECT resolved_at, payload FROM pending_decisions WHERE id = ?", (d["id"],)).fetchone()
            if row is None or row["resolved_at"] is not None:
                skipped.append(f"decision {d['id']}: resolved or gone since the plan was made")
                continue
            if json.loads(row["payload"] or "{}") != d["payload"]:
                skipped.append(f"decision {d['id']}: payload changed since the plan was made")
                continue
            if il.update("pending_decisions", {"id": d["id"]}, {"payload": json.dumps(after)}):
                written += 1
        il.flush()
    return written, skipped, batch


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--execute", action="store_true", help="apply the changes (default: dry run)")
    ap.add_argument("--file", default=DEFAULT_FILE, help="suggestions JSON (default: data/v2c_curation_suggestions.json)")
    ap.add_argument("--db", help="database path (default: $CONSTRUCTICON_DB_PATH or core.db's default)")
    args = ap.parse_args(argv)
    if args.db:
        os.environ["CONSTRUCTICON_DB_PATH"] = args.db
    from core import cards, changes as changes_mod, db  # after the env var, so DB_PATH picks it up
    if args.db:
        db.DB_PATH = args.db

    header, suggestions = load_suggestions(args.file)
    print(f"Suggestions file: {args.file} (dated {header.get('date')}, "
          f"{sum(len(v) for v in suggestions.values())} entries)")
    print(f"Database: {db.DB_PATH}")
    print("Mode: EXECUTE" if args.execute else "Mode: dry run (pass --execute to apply)")
    print()

    planned, report = plan(db, cards, suggestions)
    for d, after in planned:
        slug = d["post_slug"][len(cards.CARD_DECISION_PREFIX):]
        print(f"[{d['id']}] {slug} / {d['kind']}")
        print(f"    before: {short(d['payload'])}")
        print(f"    after:  {short(after)}")

    print()
    print(f"Would change: {len(planned)}" if not args.execute else f"Planned changes: {len(planned)}")
    print(f"Already matching (no-op): {len(report['unchanged'])}")
    for label, key in (("Entries with a bad suggestion (skipped)", "bad_entry"),
                       ("Open card decisions with no entry in the file (left alone)", "no_entry"),
                       ("File entries with no OPEN decision here (skipped: unknown slug, or already answered)", "not_open")):
        print(f"{label}: {len(report[key])}")
        for line in report[key]:
            print(f"    {line}")

    if args.execute and planned:
        written, skipped, batch = apply(db, changes_mod, planned)
        print()
        print(f"Written: {written} decision payload(s), change-log batch {batch} (op {OP}).")
        for line in skipped:
            print(f"    skipped {line}")
    elif args.execute:
        print()
        print("Nothing to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
