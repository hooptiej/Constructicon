"""Issue #302: reset constructicon-test to a "sampler platter" of production —
every real project (with its cover and write-up), a taste of every media
type each one actually contains, none of the bulk.

constructicon-test accumulates fictional projects and ad hoc rows across
rounds of dev work until it stops resembling the real site. This script
wipes the TARGET instance and re-seeds it from a read-only view of the
SOURCE (production) database + storage directory, so the test box ends up
looking like a small, realistic slice of the real thing. It is meant to be
re-run whenever the test data goes stale, not a one-off.

DISCIPLINE — how this touches each side
=======================================

  SOURCE (production) is read-only, enforced two ways:
    - The database is never opened in place. --source-db is copied (along
      with any -wal/-shm siblings, since production runs in WAL mode) into
      a temp dir and THAT snapshot is opened with sqlite's ?mode=ro. Nothing
      ever holds a writable handle on production's DB file.
    - Storage files are only ever read (Path.read_bytes) to be re-uploaded.
    The recommended way to run it makes the read-only-ness a filesystem
    fact rather than a promise: a throwaway container with production's
    data dir bind-mounted :ro (see "Running it" below).

  TARGET (constructicon-test) is written to ONLY over HTTP, through the
    same routes the app's own UI and the other scripts/ use — no raw SQL
    against the target, ever (same rule as scripts/full_youtube_channel_sync.py
    and scripts/backfill_content_dates.py):
      POST /api/delete-all                 the wipe (items + files + tags + projects)
      GET  /api/pending-decisions          listing it auto-resolves decisions whose
                                           object was just deleted (#240 "stale")
      POST /api/projects                   create each project (+ its linked tag and
                                           auto write-up doc, exactly like the UI)
      POST /api/projects/{id}              description / status / cover / dates
      POST /api/upload                     file-backed items (multipart, real bytes)
      POST /api/content                    youtube / url / document items
      POST /api/image/{slug}               display_name / icon / content_date /
                                           content_description / type_metadata
      POST /api/image/{slug}/project       an item's 2nd+ project membership
      POST /api/delete                     the auto write-up on a project that has
                                           none in production
    Slugs are minted by the target, so nothing here assumes the source's
    slugs survive — every reference (cover_slug, extra memberships) goes
    through a source-slug -> target-slug map built as rows are created.
    Things that only exist in the row (uploaded_by/Source label, timestamp,
    OCR text, embeddings, agent_notes) are deliberately NOT copied — the
    target regenerates OCR/thumbnails/captions itself, which is the point
    of having real files there.

SAMPLING — what "a taste, not the whole meal" means here
=======================================================

  Projects: EVERY project row in the source, including nested children
    (projects.parent_id), recreated parent-first so the hierarchy survives.
    Production's top-level projects are mostly containers whose actual
    content lives in their sub-projects (e.g. "Ancient projects." holds
    nothing directly), so copying only parent_id IS NULL rows would leave
    them hollow — the issue's "every real top-level project represented"
    is only meaningful with their children along. Title, description,
    status, cover, write-up body, and start/end date overrides all come
    across.

  Items per project: the project's members in their curated sort_order,
    grouped by media_type, and the FIRST --per-type of each type (default
    2) are taken. Deterministic on purpose (no random.sample) so two runs
    against the same source produce the same test data. Two exceptions:
      - the project's cover item is always included (a project card with a
        broken cover is exactly the kind of thing the test box exists to
        catch), even if it wasn't in the first N of its type;
      - the project's write-up document is not sampled as an item at all —
        it's copied through the project's own write-up slot instead, so the
        target ends up with one real write-up per project, not two.
    An item that belongs to several projects is created once and attached
    to the rest via /api/image/{slug}/project.

  Unfiled items: --unfiled-per-type (default 1) of each media_type among
    rows in no project at all, oldest first, so /unfiled has something
    real on it too.

  Skipped, with a line in the output: redacted rows (no file to copy),
    rows whose stored file is missing on disk, files over the target's
    25 MB upload cap (core/storage.py MAX_BYTES).

Running it
==========

  Dry run (the default — prints the plan and the target's current state,
  writes nothing):

    python scripts/seed_test_from_production.py \
        --source-db /prod/imagerepo.db --source-storage /prod/storage \
        --base-url http://172.16.6.2:80

  For real, add --execute. There is no other confirmation: --execute WIPES
  THE TARGET. Look at the dry run first.

  The recommended way to run it on the TrueNAS box, with production's data
  directory mounted read-only into a throwaway container on the bridge
  network both app containers share (the app containers' ipvlan addresses
  aren't reachable from the host itself):

    scp scripts/seed_test_from_production.py hoop@10.0.1.78:seed.py
    sudo docker run --rm --network ollama_default \
        -v "/mnt/Storage Pool/Media/constructicon:/prod:ro" \
        -v "$HOME/seed.py:/seed.py:ro" \
        constructicon-test:latest \
        python3 /seed.py --source-db /prod/imagerepo.db \
            --source-storage /prod/storage --base-url http://172.16.6.2:80 --execute

  (172.16.6.2 is constructicon-test's address on ollama_default — check
  `docker inspect constructicon-test` rather than trusting this comment.)
  It only needs the standard library, so it also runs from a dev machine
  that can see the target over the LAN, given a copy of the source DB and
  storage dir.

DO NOT point --base-url at the production instance. There is no guard
that could tell the two apart from the outside — the wipe is the first
thing --execute does.
"""
import argparse
import glob
import json
import mimetypes
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# core/storage.py's MAX_BYTES — /api/upload rejects anything bigger with a
# 400, so skip those up front rather than burning the transfer.
UPLOAD_CAP_BYTES = 25 * 1024 * 1024

# Types created via /api/content (no file on disk). Everything else goes
# through /api/upload with the real bytes. Mirrors ThumbnailSource in
# core/object_types: a spec with UPLOADED_FILE/CAPTURE-from-file needs the
# upload route, FETCH_URL/NONE-without-file rows are content rows. Decided
# here from the source row's stored_filename rather than by importing the
# registry, so this script has no dependency on the app's own code.


# --------------------------------------------------------------------------
# HTTP helpers — stdlib only, same as the other scripts in this directory.
# --------------------------------------------------------------------------

class Target:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _request(self, method, path, data=None, headers=None, timeout=60):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, body
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get_json(self, path):
        status, body = self._request("GET", path)
        if status != 200:
            raise RuntimeError(f"GET {path} -> {status}: {body[:300]!r}")
        return json.loads(body)

    def get_status(self, path, timeout=30):
        status, _ = self._request("GET", path, timeout=timeout)
        return status

    def post_form(self, path, fields, ok=(200,), timeout=60):
        """application/x-www-form-urlencoded POST. Repeated keys (FastAPI's
        list[str] = Form(...)) are passed as a list value."""
        pairs = []
        for k, v in fields.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                pairs.extend((k, item) for item in v)
            else:
                pairs.append((k, v))
        data = urllib.parse.urlencode(pairs).encode("utf-8")
        status, body = self._request(
            "POST", path, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=timeout,
        )
        if status not in ok:
            raise RuntimeError(f"POST {path} -> {status}: {body[:300]!r}")
        return status, (json.loads(body) if body else None)

    def post_multipart(self, path, fields, filename, content, ok=(200,), timeout=300):
        boundary = f"----seed{uuid.uuid4().hex}"
        parts = []
        for k, v in fields.items():
            if v is None:
                continue
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
            )
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe_name = filename.replace('"', "'")
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n".encode("utf-8")
        )
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        data = b"".join(parts)
        status, body = self._request(
            "POST", path, data=data,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=timeout,
        )
        if status not in ok:
            raise RuntimeError(f"POST {path} ({filename}) -> {status}: {body[:300]!r}")
        return status, (json.loads(body) if body else None)


# --------------------------------------------------------------------------
# Source (read-only snapshot of production's DB)
# --------------------------------------------------------------------------

def open_source_snapshot(source_db):
    """Copy the DB (+ WAL/SHM siblings) into a temp dir and open the copy
    read-only. Returns (conn, tempdir) — caller removes tempdir."""
    src = Path(source_db)
    if not src.exists():
        sys.exit(f"--source-db not found: {src}")
    tmp = Path(tempfile.mkdtemp(prefix="seed-src-"))
    for f in glob.glob(str(src) + "*"):
        shutil.copy2(f, tmp / Path(f).name)
    conn = sqlite3.connect(f"file:{tmp / src.name}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, tmp


def row_dict(r):
    d = dict(r)
    if "tags" in d:
        try:
            d["tags"] = json.loads(d["tags"] or "[]")
        except json.JSONDecodeError:
            d["tags"] = []
    if "type_metadata" in d:
        try:
            d["type_metadata"] = json.loads(d["type_metadata"] or "{}")
        except json.JSONDecodeError:
            d["type_metadata"] = {}
    return d


def load_source(conn):
    projects = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")]
    by_id = {p["id"]: p for p in projects}
    # Parent-first ordering: depth, then id. Parents must exist on the
    # target before a child can reference them.
    def depth(p):
        d, cur = 0, p
        while cur.get("parent_id") and cur["parent_id"] in by_id:
            d += 1
            cur = by_id[cur["parent_id"]]
        return d
    projects.sort(key=lambda p: (depth(p), p["id"]))

    members = {}
    for p in projects:
        rows = conn.execute(
            "SELECT ce.* FROM project_items pi JOIN capture_events ce ON ce.slug = pi.post_slug "
            "WHERE pi.project_id = ? AND ce.redacted = 0 ORDER BY pi.sort_order ASC, ce.id ASC",
            (p["id"],),
        ).fetchall()
        members[p["id"]] = [row_dict(r) for r in rows]

    unfiled = [row_dict(r) for r in conn.execute(
        "SELECT * FROM capture_events WHERE redacted = 0 "
        "AND slug NOT IN (SELECT post_slug FROM project_items) ORDER BY timestamp ASC, id ASC"
    )]
    writeups = {}
    for p in projects:
        if p.get("writeup_slug"):
            r = conn.execute("SELECT * FROM capture_events WHERE slug = ?", (p["writeup_slug"],)).fetchone()
            if r is not None:
                writeups[p["id"]] = row_dict(r)
    return projects, members, unfiled, writeups


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

def sample_by_type(rows, per_type, exclude_slugs=(), force_slugs=()):
    """First `per_type` rows of each media_type, in the given order, plus
    any force_slugs regardless of the cap. Preserves original order."""
    taken = OrderedDict()
    counts = Counter()
    for r in rows:
        if r["slug"] in exclude_slugs:
            continue
        if r["slug"] in force_slugs or counts[r["media_type"]] < per_type:
            if r["slug"] not in taken:
                taken[r["slug"]] = r
                counts[r["media_type"]] += 1
    return list(taken.values())


def build_plan(projects, members, unfiled, writeups, source_storage, per_type, unfiled_per_type):
    storage = Path(source_storage)
    plan_items = OrderedDict()  # source slug -> {"row", "primary_project", "extra_projects"}
    skipped = []                # (slug, media_type, reason)
    project_plans = []

    def consider(row, project_id):
        slug = row["slug"]
        if slug in plan_items:
            if project_id is not None and project_id != plan_items[slug]["primary_project"]:
                plan_items[slug]["extra_projects"].append(project_id)
            return True
        if row.get("stored_filename"):
            path = storage / row["stored_filename"]
            if not path.exists():
                skipped.append((slug, row["media_type"], f"file missing: {row['stored_filename']}"))
                return False
            size = path.stat().st_size
            if size > UPLOAD_CAP_BYTES:
                skipped.append((slug, row["media_type"], f"{size / 1e6:.1f} MB exceeds 25 MB upload cap"))
                return False
            row["_path"], row["_bytes"] = path, size
        else:
            row["_path"], row["_bytes"] = None, 0
        plan_items[slug] = {"row": row, "primary_project": project_id, "extra_projects": []}
        return True

    for p in projects:
        exclude = {p["writeup_slug"]} if p.get("writeup_slug") else set()
        force = {p["cover_slug"]} if p.get("cover_slug") else set()
        chosen = sample_by_type(members[p["id"]], per_type, exclude_slugs=exclude, force_slugs=force)
        kept = [r for r in chosen if consider(r, p["id"])]
        project_plans.append({"project": p, "items": kept, "writeup": writeups.get(p["id"])})

    unfiled_chosen = sample_by_type(unfiled, unfiled_per_type)
    unfiled_kept = [r for r in unfiled_chosen if consider(r, None)]
    return project_plans, unfiled_kept, plan_items, skipped


def describe_plan(project_plans, unfiled_kept, plan_items, skipped, projects, members, unfiled):
    print("=== PLAN ===")
    total_source_items = sum(len(m) for m in members.values())
    print(f"Source: {len(projects)} projects ({sum(1 for p in projects if not p.get('parent_id'))} top-level), "
          f"{total_source_items} project memberships, {len(unfiled)} unfiled rows")
    for pp in project_plans:
        p = pp["project"]
        src_counts = Counter(r["media_type"] for r in members[p["id"]])
        got = Counter(r["media_type"] for r in pp["items"])
        indent = "  " if p.get("parent_id") else ""
        summary = ", ".join(f"{t}:{got[t]}/{src_counts[t]}" for t in sorted(src_counts))
        cover = "cover" if p.get("cover_slug") else "NO COVER"
        writeup = "writeup" if pp["writeup"] else "no writeup"
        print(f"  {indent}[{p['status']:8}] {p['title']!r}: {summary}  ({cover}, {writeup})")
    got_unfiled = Counter(r["media_type"] for r in unfiled_kept)
    print(f"  Unfiled sample: {dict(got_unfiled)}")
    by_type = Counter(v["row"]["media_type"] for v in plan_items.values())
    total_bytes = sum(v["row"]["_bytes"] for v in plan_items.values())
    multi = sum(1 for v in plan_items.values() if v["extra_projects"])
    print(f"Items to create: {len(plan_items)} ({total_bytes / 1e6:.1f} MB of files), by type: {dict(by_type)}")
    print(f"  + {len(project_plans)} projects, {sum(1 for pp in project_plans if pp['writeup'])} write-ups; "
          f"{multi} items in more than one project")
    if skipped:
        print(f"Skipped {len(skipped)}:")
        for slug, mt, why in skipped:
            print(f"  - {slug} ({mt}): {why}")


def describe_target(target):
    try:
        projects = target.get_json("/api/projects")
        groups = target.get_json("/api/gallery")
        items = sum(g.get("total", 0) for g in groups)
        pending = target.get_json("/api/pending-decisions").get("count", "?")
        print(f"=== TARGET {target.base_url} currently: {len(projects)} projects, {items} items, "
              f"{pending} open pending decisions ===")
        for p in projects:
            print(f"  - {p['title']!r} (id {p['id']}, parent {p.get('parent_id')})")
    except Exception as e:  # noqa: BLE001 — a dry run should still print the plan
        print(f"=== TARGET {target.base_url}: could not read current state ({e}) ===")


# --------------------------------------------------------------------------
# Execute
# --------------------------------------------------------------------------

def mountain_local_string(epoch):
    """POST /api/projects/{id} takes start_date/end_date as the
    <input type=datetime-local> string the project page sends, which
    web/app.py parses as naive Mountain Time (core/timeline.py). Convert
    the stored UTC epoch back into that convention."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Denver")
    except Exception:  # noqa: BLE001 — no tz database on this machine
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S")


def wipe_target(target):
    _, result = target.post_form("/api/delete-all", {}, timeout=600)
    print(f"Wiped target: {result}")
    # Listing pending decisions resolves the ones whose object no longer
    # exists (#240) — the wipe just made that all of them.
    pending = target.get_json("/api/pending-decisions")
    print(f"Pending decisions after wipe: {pending.get('count')}")


def create_projects(target, project_plans):
    """Returns {source project id: target project dict}."""
    id_map = {}
    for pp in project_plans:
        p = pp["project"]
        parent_new = id_map.get(p["parent_id"])["id"] if p.get("parent_id") in id_map else None
        _, created = target.post_form("/api/projects", {"title": p["title"], "parent_id": parent_new})
        id_map[p["id"]] = created
        fields = {"description": p.get("description") or "", "status": p.get("status") or "active"}
        if p.get("start_date_override"):
            fields["start_date"] = mountain_local_string(p["start_date_override"])
        if p.get("end_date_override"):
            fields["end_date"] = mountain_local_string(p["end_date_override"])
        target.post_form(f"/api/projects/{created['id']}", fields)
        # Write-up: the create route already made a blank document and
        # linked it; fill it from the source's, or remove it when the
        # source project has none.
        w = pp["writeup"]
        if w is not None and created.get("writeup_slug"):
            target.post_form(f"/api/image/{created['writeup_slug']}", {
                "content_description": w.get("content_description") or "",
                "type_metadata": json.dumps(w.get("type_metadata") or {}),
                "description": w.get("description") or "",
                "tags": json.dumps(w.get("tags") or []),
            })
        elif w is None and created.get("writeup_slug"):
            target.post_form(f"/api/projects/{created['id']}", {"writeup_slug": ""})
            target.post_form("/api/delete", {"slugs": [created["writeup_slug"]]})
        print(f"  project {p['id']} -> {created['id']} {p['title']!r}"
              f"{' (child of ' + str(parent_new) + ')' if parent_new else ''}")
    return id_map


DUPE_SLUG_RE = re.compile(r"/object/([A-Za-z0-9_-]+)")


def create_item(target, row, primary_project_new):
    """Create one row on the target through the same route the UI would
    use for it; returns the target's slug."""
    common = {
        "description": row.get("description") or "",
        "tags": json.dumps(row.get("tags") or []),
        "client": row.get("client") or "",
        "project_id": str(primary_project_new) if primary_project_new else "",
    }
    if row["_path"] is not None:
        fields = dict(common)
        if row.get("source_modified_at"):
            fields["modified_at"] = str(int(row["source_modified_at"] * 1000))
        status, created = target.post_multipart(
            "/api/upload", fields, row["filename"] or row["_path"].name, row["_path"].read_bytes(), ok=(200, 409),
        )
        if status == 409:
            # find_duplicate matched an earlier upload (same name/size/mtime
            # exists twice in the source) — reuse it rather than fail.
            m = DUPE_SLUG_RE.search(created.get("detail", "") if created else "")
            if not m:
                raise RuntimeError(f"409 on {row['slug']} without a slug in the detail: {created}")
            new_slug = m.group(1)
            if primary_project_new:
                target.post_form(f"/api/image/{new_slug}/project", {"project_id": str(primary_project_new)})
            return new_slug, "dupe"
        new_slug = created["slug"]
        follow = {}
        if row.get("content_description"):
            follow["content_description"] = row["content_description"]
    else:
        fields = dict(common)
        fields.update({
            "media_type": row["media_type"],
            "external_url": row.get("external_url") or "",
            "content_description": row.get("content_description") or "",
            "content_date": str(row["content_date"]) if row.get("content_date") else "",
            "type_metadata": json.dumps(row.get("type_metadata") or {}),
        })
        _, created = target.post_form("/api/content", fields)
        new_slug = created["slug"]
        follow = {}
    # Per-object overrides and the source's own metadata, merged on top of
    # whatever the target's upload path just derived itself.
    if row.get("display_name"):
        follow["display_name"] = row["display_name"]
    if row.get("icon"):
        follow["icon"] = row["icon"]
    if row.get("content_date") and row["_path"] is not None:
        follow["content_date"] = str(row["content_date"])
    if row["_path"] is not None and row.get("type_metadata"):
        follow["type_metadata"] = json.dumps(row["type_metadata"])
    if follow:
        target.post_form(f"/api/image/{new_slug}", follow)
    return new_slug, "created"


def create_items(target, plan_items, id_map):
    slug_map = {}
    outcomes = Counter()
    n = len(plan_items)
    for i, (src_slug, entry) in enumerate(plan_items.items(), 1):
        row = entry["row"]
        primary_new = id_map[entry["primary_project"]]["id"] if entry["primary_project"] else None
        label = row.get("filename") or row.get("content_description") or row.get("external_url") or src_slug
        try:
            new_slug, outcome = create_item(target, row, primary_new)
        except Exception as e:  # noqa: BLE001 — keep going, report at the end
            outcomes["failed"] += 1
            print(f"  [{i}/{n}] FAILED {row['media_type']} {label!r}: {e}")
            continue
        slug_map[src_slug] = new_slug
        outcomes[outcome] += 1
        for extra in entry["extra_projects"]:
            target.post_form(f"/api/image/{new_slug}/project", {"project_id": str(id_map[extra]["id"])})
        extra_note = f" (+{len(entry['extra_projects'])} more projects)" if entry["extra_projects"] else ""
        print(f"  [{i}/{n}] {outcome} {row['media_type']:8} {src_slug} -> {new_slug} {label!r}{extra_note}")
    return slug_map, outcomes


def set_covers(target, project_plans, id_map, slug_map):
    set_count = 0
    for pp in project_plans:
        p = pp["project"]
        if not p.get("cover_slug"):
            continue
        new_cover = slug_map.get(p["cover_slug"])
        if new_cover is None:
            print(f"  cover for {p['title']!r} not seeded (source item skipped) — leaving the auto-cover")
            continue
        target.post_form(f"/api/projects/{id_map[p['id']]['id']}", {"cover_slug": new_cover})
        set_count += 1
    print(f"Covers set: {set_count}")


def verify(target, project_plans, id_map, slug_map, expected_items):
    print("=== VERIFY (over HTTP) ===")
    projects = target.get_json("/api/projects")
    print(f"Target projects: {len(projects)} (expected {len(project_plans)})")
    groups = target.get_json("/api/gallery")
    print(f"Target items: {sum(g.get('total', 0) for g in groups)} "
          f"(expected {expected_items} seeded + write-ups)")
    bad = []
    for pp in project_plans:
        p = pp["project"]
        cover_new = slug_map.get(p.get("cover_slug"))
        if not cover_new:
            continue
        # Thumbnails for video/pdf/stl/youtube are rendered in a background
        # task after the upload returns — give them a moment.
        status = None
        for _ in range(6):
            status = target.get_status(f"/f/{cover_new}/thumb", timeout=60)
            if status == 200:
                break
            time.sleep(5)
        if status != 200:
            bad.append((p["title"], cover_new, status))
    print(f"Cover thumbnails: {len(project_plans) - len(bad) - sum(1 for pp in project_plans if not slug_map.get(pp['project'].get('cover_slug')))} OK"
          + (f", {len(bad)} NOT 200: {bad}" if bad else ""))
    for path in ("/", "/unfiled", "/admin", f"/project/{projects[0]['slug']}" if projects else "/"):
        print(f"  GET {path} -> {target.get_status(path)}")
    pending = target.get_json("/api/pending-decisions").get("count")
    print(f"Open pending decisions now (name-based auto-match, #240, is real upload behavior): {pending}")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source-db", required=True, help="production imagerepo.db (copied, opened read-only)")
    ap.add_argument("--source-storage", required=True, help="production storage/ directory (read only)")
    ap.add_argument("--base-url", required=True, help="the TARGET instance to wipe and seed, e.g. http://172.16.6.2:80")
    ap.add_argument("--per-type", type=int, default=2, help="items of each media_type per project (default 2)")
    ap.add_argument("--unfiled-per-type", type=int, default=1, help="unfiled items of each media_type (default 1)")
    ap.add_argument("--execute", action="store_true", help="actually wipe and seed the target (default: dry run)")
    args = ap.parse_args()

    conn, tmp = open_source_snapshot(args.source_db)
    try:
        projects, members, unfiled, writeups = load_source(conn)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    project_plans, unfiled_kept, plan_items, skipped = build_plan(
        projects, members, unfiled, writeups, args.source_storage, args.per_type, args.unfiled_per_type,
    )
    target = Target(args.base_url)
    describe_target(target)
    describe_plan(project_plans, unfiled_kept, plan_items, skipped, projects, members, unfiled)

    if not args.execute:
        print("\nDry run — nothing written. Re-run with --execute to wipe and seed the target above.")
        return

    print(f"\n=== EXECUTE against {target.base_url} ===")
    t0 = time.time()
    wipe_target(target)
    print("Creating projects...")
    id_map = create_projects(target, project_plans)
    print("Creating items...")
    slug_map, outcomes = create_items(target, plan_items, id_map)
    print(f"Items: {dict(outcomes)}")
    set_covers(target, project_plans, id_map, slug_map)
    verify(target, project_plans, id_map, slug_map, len(slug_map))
    print(f"Done in {time.time() - t0:.0f}s.")


if __name__ == "__main__":
    main()
