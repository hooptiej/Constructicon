"""Link the original 'lil dragon' ink drawing to the brand assets it became (#425).

The drawing (capture_events.slug, found by filename 'lil-dragon-original-drawing') is the
source of the hooptieJ logo's dragon. This proposes item-to-item "related" links
(capture_event_relations, the same store the item page's "+ Add related" uses) between it and:

  strong   brand assets whose filename says they ARE the dragon artwork (lildrag*, *dragonly*)
           plus the Constructicon logo.
  logos    (--include-logos) the other hooptieJ logo files (HooptieJlogo*, brand_role logo).

DRY-RUN BY DEFAULT: prints the plan and touches nothing (the database is opened read-only).
Nothing is written without --execute. Run it where the database is, e.g. in the app container:

    python3 scripts/link_lil_dragon_brand.py                  # plan only
    python3 scripts/link_lil_dragon_brand.py --include-logos  # plan with the wider logo family
    python3 scripts/link_lil_dragon_brand.py --execute        # actually link

Side effect to know about: db.add_relation (#16) also shares tags and project membership both
ways, so linking pulls the assets' tags (e.g. "New Hoop Icon") and projects onto the drawing and
vice versa. The plan prints exactly what would move. Pass --plain with --execute to write only the
relation rows and skip that sharing. Already-linked pairs are skipped, so re-running is safe.
"""
import argparse
import json
import os
import re
import sqlite3
import sys

try:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # piped to `python3 -`
    ROOT = os.getcwd()
sys.path.insert(0, ROOT)

DRAWING_FILENAME = "lil-dragon-original-drawing"
STRONG = re.compile(r"lildrag|dragonly", re.I)
LOGO_FAMILY = re.compile(r"^hooptiejlogo|hooptiej-wordmark", re.I)
EXTRA_SLUGS_BY_FILENAME = ("constructicon-logo.png",)


def _tags(raw):
    try:
        return [t for t in json.loads(raw or "[]")]
    except ValueError:
        return []


def plan(conn, drawing_slug=None, include_logos=False):
    conn.row_factory = sqlite3.Row
    if drawing_slug:
        d = conn.execute("SELECT * FROM capture_events WHERE slug = ?", (drawing_slug,)).fetchone()
    else:
        rows = conn.execute("SELECT * FROM capture_events WHERE redacted = 0 AND filename LIKE ?",
                            (DRAWING_FILENAME + "%",)).fetchall()
        if len(rows) > 1:
            raise SystemExit("More than one lil dragon drawing; pass --slug: " + ", ".join(r["slug"] for r in rows))
        d = rows[0] if rows else None
    if d is None:
        raise SystemExit("Could not find the lil dragon drawing.")
    already = {r["slug_b"] for r in conn.execute(
        "SELECT slug_b FROM capture_event_relations WHERE slug_a = ?", (d["slug"],))}
    d_projects = {r["id"]: r["title"] for r in conn.execute(
        "SELECT p.id, p.title FROM projects p JOIN project_items pi ON pi.project_id = p.id "
        "WHERE pi.post_slug = ?", (d["slug"],))}
    cands = []
    for r in conn.execute("SELECT * FROM capture_events WHERE redacted = 0 AND is_brand_asset = 1 "
                          "AND slug != ? ORDER BY filename", (d["slug"],)):
        fn = r["filename"] or ""
        if STRONG.search(fn) or fn in EXTRA_SLUGS_BY_FILENAME:
            tier = "strong"
        elif include_logos and (LOGO_FAMILY.search(fn) or r["brand_role"] == "logo" and "hooptiej" in fn.lower()):
            tier = "logos"
        else:
            continue
        projs = {p["id"]: p["title"] for p in conn.execute(
            "SELECT p.id, p.title FROM projects p JOIN project_items pi ON pi.project_id = p.id "
            "WHERE pi.post_slug = ?", (r["slug"],))}
        cands.append({
            "slug": r["slug"], "filename": fn, "brand_role": r["brand_role"], "tier": tier,
            "already_linked": r["slug"] in already,
            "tags": _tags(r["tags"]), "projects": projs,
        })
    return d, d_projects, cands


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", help="slug of the drawing (default: found by filename)")
    ap.add_argument("--include-logos", action="store_true", help="also propose the wider hooptieJ logo family")
    ap.add_argument("--execute", action="store_true", help="actually write the links (default is a dry run)")
    ap.add_argument("--plain", action="store_true",
                    help="with --execute: write only the relation rows, no tag/project sharing")
    ap.add_argument("--db", help="database path (default: core.db.DB_PATH)")
    args = ap.parse_args(argv)

    from core import db
    path = str(args.db or db.DB_PATH)
    ro = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        drawing, d_projects, cands = plan(ro, args.slug, args.include_logos)
    finally:
        ro.close()

    print(f"Drawing: {drawing['slug']}  {drawing['filename']}")
    print(f"  in projects: {', '.join(d_projects.values()) or '(none)'}")
    todo = [c for c in cands if not c["already_linked"]]
    for c in cands:
        mark = "already linked" if c["already_linked"] else "WOULD LINK"
        extra = [t for t in c["tags"]] + [f"project:{t}" for pid, t in c["projects"].items() if pid not in d_projects]
        print(f"  [{c['tier']:6}] {mark:14} {c['slug']}  {c['filename']}  role={c['brand_role']}"
              + (f"  shares: {', '.join(extra)}" if extra and not c["already_linked"] and not args.plain else ""))
    print(f"{len(todo)} new link(s), {len(cands) - len(todo)} already linked.")

    if not args.execute:
        print("DRY RUN: nothing written. Re-run with --execute to link.")
        return 0
    for c in todo:
        if args.plain:
            conn = db.get_conn()
            try:
                import time
                now = time.time()
                for a, b in ((drawing["slug"], c["slug"]), (c["slug"], drawing["slug"])):
                    conn.execute("INSERT OR IGNORE INTO capture_event_relations (slug_a, slug_b, created_at) "
                                 "VALUES (?, ?, ?)", (a, b, now))
                conn.commit()
            finally:
                conn.close()
        else:
            db.add_relation(drawing["slug"], c["slug"])
        print("linked", c["slug"], c["filename"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
