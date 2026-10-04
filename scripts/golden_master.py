"""Golden-master snapshot of the web app, for refactors that must not change behaviour (#547).

Run it INSIDE the app container (it imports web.app and reads the DB directly) against the
same DB state before and after a refactor, then compare the two files:

    python3 scripts/golden_master.py --out /tmp/gm-before.json [--mutations]
    ... deploy the refactor, restore the same DB ...
    python3 scripts/golden_master.py --out /tmp/gm-after.json [--mutations]
    python3 scripts/golden_master.py --compare /tmp/gm-before.json /tmp/gm-after.json

What it records:
  1. routes      every entry of app.routes: kind, methods, path, endpoint __name__, the
                 endpoint's parameters (name, kind, annotation, default) and FastAPI's own
                 classification (path/query/header/cookie/body params, response class).
                 Compared as a set: the order of registration is checked by (2).
  2. resolution  for a generated list of concrete URLs (each route's params filled from
                 real DB values, plus every literal path segment of every route substituted
                 into every param slot, to catch /x/{id} vs /x/literal collisions) and each
                 HTTP method, which route Starlette's router picks (FULL / PARTIAL / NONE),
                 emulating Router.__call__'s first-match rule. No HTTP involved.
  3. openapi     app.openapi(), key-sorted (so ordering-only differences vanish).
  4. responses   HTTP GET of ~100 pages and read-only APIs on the running server
                 (--base-url): status, content type, Location, and a sha256 of the body
                 after normalising volatile bits (cache-busters, zip timestamps).
                 Normalised bodies are written next to the output (<out>.bodies/) for diffing.
  5. mutations   (--mutations) safe write round-trips through HTTP with a same-origin
                 Origin header, each reverted: item edit, card field edit, provenance option
                 add + retire, a .txt upload + delete, a no-op settings write (its audit row
                 must be redacted), the cross-origin guard (403) and the JSON gate (415). The
                 audit row each one wrote is recorded (volatile ids normalised), including
                 its `actor` column (#560).
  6. errors      (--errors) deliberate error probes (404 object/card/route, 405, bad card
                 edit, unknown decision, bad decision choice, revision cycle, bad
                 physical-piece date, bad queue key, missing form field): status + JSON body.
                 compare() reports, per probe, whether the bodies differ ONLY by the shared
                 error-shape fields `ok` / `error` (#548). The revision-cycle probe makes one
                 link and removes it again.
                 Writes are reverted where the API allows; the provenance option it adds
                 stays (retired). Restore the DB backup afterwards for a fully clean state.

Expected difference when routes move between routers: the raw GET /openapi.json body
lists paths in registration order, so its hash in (4) changes while (3), the key-sorted
document, stays identical. Check the bodies with json sort_keys before accepting it.

GET /api/pending-decisions and the curator queue can tidy stale data as a side effect,
so capture both sides from the same restored DB, not one after the other on a live DB.
"""

import argparse
import hashlib
import inspect
import io
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

METHODS = ("GET", "POST", "PUT", "DELETE")
PARAM_RE = re.compile(r"{([^}:]+)(?::[^}]+)?}")


# --- 1. route table ---------------------------------------------------------------------

def _ann(a):
    if a is inspect.Parameter.empty:
        return None
    return getattr(a, "__name__", None) if isinstance(a, type) else repr(a)


def _default(d):
    if d is inspect.Parameter.empty:
        return None
    return repr(d)


def _cls_name(c):
    """A response class name; FastAPI's DefaultPlaceholder (whose repr has an address) as Default(X)."""
    inner = getattr(c, "value", None)
    if inner is not None and type(c).__name__ == "DefaultPlaceholder":
        return f"Default({getattr(inner, '__name__', inner)})"
    return getattr(c, "__name__", repr(c))


def flat_routes(app):
    """app.routes with included routers expanded. FastAPI >= 0.14x keeps an included
    APIRouter as one lazy _IncludedRouter entry instead of copying its routes in."""
    for r in app.routes:
        contexts = getattr(r, "effective_route_contexts", None)
        if contexts is None:
            yield r
        else:
            for ctx in contexts():
                yield ctx.original_route


def route_table(app):
    from fastapi.routing import APIRoute
    from starlette.routing import Mount

    rows = []
    for r in flat_routes(app):
        if isinstance(r, Mount):
            rows.append({"kind": "mount", "path": r.path, "name": r.name})
            continue
        ep = getattr(r, "endpoint", None)
        row = {
            "kind": "api" if isinstance(r, APIRoute) else type(r).__name__,
            "methods": sorted(getattr(r, "methods", None) or []),
            "path": r.path,
            "name": getattr(r, "name", None),
            "endpoint": getattr(ep, "__name__", None),
            "is_async": inspect.iscoroutinefunction(ep) if ep else None,
        }
        if ep is not None:
            row["params"] = [
                [p.name, p.kind.name, _ann(p.annotation), _default(p.default)]
                for p in inspect.signature(ep).parameters.values()
            ]
        if isinstance(r, APIRoute):
            d = r.dependant
            row["fastapi"] = {
                "path": [f.name for f in d.path_params],
                "query": [f.name for f in d.query_params],
                "header": [f.name for f in d.header_params],
                "cookie": [f.name for f in d.cookie_params],
                "body": [f.name for f in d.body_params],
                "request_param": d.request_param_name,
                "background_tasks_param": d.background_tasks_param_name,
                "response_class": _cls_name(r.response_class),
                "status_code": r.status_code,
                "include_in_schema": r.include_in_schema,
            }
        rows.append(row)
    return rows


# --- real values from the DB ------------------------------------------------------------

def db_values():
    from core import db
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row

    def col(sql, *args):
        return [r[0] for r in conn.execute(sql, args).fetchall()]

    v = {
        # one current, non-redacted item per media type (oldest first, deterministic)
        "items_by_type": {r["media_type"]: r["slug"] for r in conn.execute(
            "SELECT media_type, MIN(id) AS id, slug FROM capture_events WHERE redacted = 0 "
            "GROUP BY media_type ORDER BY media_type").fetchall()},
        "items_with_file": col("SELECT slug FROM capture_events WHERE redacted = 0 AND stored_filename IS NOT NULL "
                               "AND media_type = 'image' ORDER BY id LIMIT 3"),
        "redacted": col("SELECT slug FROM capture_events WHERE redacted = 1 ORDER BY id LIMIT 1"),
        "revised": col("SELECT old_slug FROM item_revisions ORDER BY old_slug LIMIT 1"),
        "projects_by_kind": {r["kind"]: (r["id"], r["slug"]) for r in conn.execute(
            "SELECT kind, MIN(id) AS id, slug FROM projects GROUP BY kind ORDER BY kind").fetchall()},
        "projects_most_items": [(r[0], r[1]) for r in conn.execute(
            "SELECT p.id, p.slug FROM projects p JOIN project_items pi ON pi.project_id = p.id "
            "GROUP BY p.id ORDER BY COUNT(*) DESC, p.id LIMIT 3").fetchall()],
        "hobbies": col("SELECT slug FROM blog_tags WHERE is_hobby = 1 ORDER BY id LIMIT 4"),
        "hobby_ids": col("SELECT id FROM blog_tags WHERE is_hobby = 1 ORDER BY id LIMIT 1"),
        "uploaders": col("SELECT tech FROM capture_events WHERE redacted = 0 GROUP BY tech "
                         "ORDER BY COUNT(*) DESC, tech LIMIT 2"),
        "tags": col("SELECT name FROM blog_tags ORDER BY id LIMIT 2"),
        "blog_entries": col("SELECT slug FROM blog_entries ORDER BY id LIMIT 2"),
        "decisions": col("SELECT id FROM pending_decisions WHERE resolved_at IS NULL ORDER BY id LIMIT 1"),
        "families": col("SELECT family_id FROM family_members ORDER BY family_id LIMIT 1"),
        "batches": col("SELECT batch_id FROM audit_log WHERE batch_id IS NOT NULL ORDER BY id DESC LIMIT 1"),
        "provenance_keys": col("SELECT key FROM provenance_options WHERE scope = 'card' ORDER BY sort_order, key LIMIT 1"),
    }
    conn.close()
    return v


# --- 2. path resolution -----------------------------------------------------------------

def _label(r):
    from starlette.routing import Mount
    if r is None:
        return None
    if isinstance(r, Mount):
        return f"mount:{r.name} {r.path}"
    ep = getattr(r, "endpoint", None)
    return f"{getattr(ep, '__name__', r.name)} {r.path}"


def _inner(r, scope):
    """The route an included router (FastAPI _IncludedRouter) would pick for this scope."""
    pick = getattr(r, "_match", None)
    if pick is None:
        return r
    _, _, route, ctx = pick(dict(scope))
    return ctx.original_route if ctx is not None else route


def resolve(app, method, path):
    """Starlette/FastAPI Router.app: the first FULL match handles it, else the first PARTIAL.
    An included router answers FULL/PARTIAL for its whole group, then picks its own first
    FULL (else first PARTIAL) -- equivalent to the flat first-match rule."""
    from starlette.routing import Match
    scope = {"type": "http", "method": method, "path": path, "root_path": "",
             "headers": [], "query_string": b""}
    partial = None
    for r in app.router.routes:
        match, _ = r.matches(dict(scope))
        if match == Match.FULL:
            return "FULL " + _label(_inner(r, scope))
        if match == Match.PARTIAL and partial is None:
            partial = _inner(r, scope)
    return ("PARTIAL " + _label(partial)) if partial else "NONE"


def param_pool(v):
    items = list(v["items_by_type"].values())
    projects = list(v["projects_by_kind"].values())
    return {
        "slug": items[:2] + [s for _, s in projects[:1]] + v["hobbies"][:1] + v["blog_entries"][:1],
        "uploader": v["uploaders"][:1],
        "decision_id": [str(d) for d in v["decisions"]] or ["1"],
        "project_id": [str(i) for i, _ in projects[:1]] + [s for _, s in projects[:1]],
        "id_or_slug": v["hobbies"][:1] + [str(i) for i in v["hobby_ids"]],
        "scope": ["card", "file"],
        "key": v["provenance_keys"][:1] or ["k"],
        "batch_id": v["batches"][:1] or ["b"],
        "family_id": [str(f) for f in v["families"]] or ["1"],
    }


def resolution_urls(routes, v):
    pool = param_pool(v)
    literals = set()
    for r in routes:
        for seg in r["path"].strip("/").split("/"):
            if seg and not PARAM_RE.fullmatch(seg):
                literals.add(seg)
    literals |= {"", "nope", "123"}
    urls = set()
    for r in routes:
        if r["kind"] == "mount":
            urls.add(r["path"] + "/x.css")
            urls.add(r["path"])
            continue
        path = r["path"]
        names = PARAM_RE.findall(path)

        def fill(choice):
            out = path
            for name in names:
                out = PARAM_RE.sub(lambda m: urllib.parse.quote(choice.get(name, ""), safe=""), out, count=1)
            return out

        if not names:
            urls.add(path)
            urls.add(path.rstrip("/") + "/")
            continue
        base = {n: (pool.get(n) or ["x"])[0] for n in names}
        urls.add(fill(base))
        for n in names:
            for val in pool.get(n, []):
                urls.add(fill({**base, n: val}))
            for lit in literals:
                urls.add(fill({**base, n: lit}))
    return sorted(urls)


# --- 4. response snapshots --------------------------------------------------------------

def snapshot_urls(v):
    u = ["/", "/?rev=all", "/unfiled", "/unfiled?rev=all", "/gallery", "/upload", "/hobbies",
         "/brand", "/wallpaper", "/account", "/admin", "/admin?embed=1", "/curator", "/captions/review",
         "/object/does-not-exist", "/project/does-not-exist", "/hobby/does-not-exist",
         "/healthz", "/api/version", "/api/settings", "/api/pending-decisions", "/api/audit-log?limit=25",
         "/api/redacted", "/api/restricted", "/api/admin/storage-stats", "/api/processing",
         "/api/captions/unreviewed", "/api/captions/defaults", "/api/gallery", "/api/gallery?query=kerbal",
         "/api/clients", "/api/projects", "/api/provenance-options", "/api/provenance-options?scope=file&include_retired=1",
         "/api/curator/dashboard", "/api/curator/needs", "/api/curator/needs?limit=5", "/api/curator/queue",
         "/api/curator/queue?summary=1", "/api/curator/queue/html", "/api/hobbies", "/api/hobby/does-not-exist",
         "/api/brand-assets", "/api/wallpapers", "/api/blog-entries", "/api/blog-entries?status=draft",
         "/api/tags", "/api/search?query=kerbal", "/api/search?query=build", "/api/export/config",
         "/api/export/targets", "/api/account/desktop-app-build", "/api/image/does-not-exist",
         "/f/does-not-exist", "/downloads/constructicon-uploader-source.zip",
         "/downloads/constructicon-uploader.zip", "/openapi.json", "/docs"]
    for h in v["hobbies"]:
        u += [f"/hobby/{h}", f"/api/hobby/{h}", f"/?hobby={h}"]
    for kind, (pid, slug) in v["projects_by_kind"].items():
        u += [f"/project/{slug}", f"/api/cards/{slug}", f"/api/projects/{pid}/explain",
              f"/api/project/{slug}/links", f"/api/curator/queue?card={slug}",
              f"/api/curator/queue/html?card={slug}"]
    for pid, slug in v["projects_most_items"]:
        u += [f"/project/{slug}", f"/project/{slug}?rev=all"]
    if v["projects_most_items"]:
        u.append(f"/api/projects/{v['projects_most_items'][-1][0]}/export.zip")
    for mt, slug in v["items_by_type"].items():
        u += [f"/object/{slug}", f"/api/image/{slug}"]
    for slug in v["items_with_file"][:2]:
        u += [f"/f/{slug}", f"/f/{slug}/thumb", f"/image/{slug}", f"/api/image/{slug}/revisions",
              f"/api/image/{slug}/similar"]
    for slug in v["redacted"] + v["revised"]:
        u += [f"/object/{slug}", f"/api/image/{slug}/revisions"]
    for up in v["uploaders"]:
        u += ["/gallery/user/" + urllib.parse.quote(up, safe=""), "/gallery/user/" + urllib.parse.quote(up, safe="") + "?rev=all"]
    for t in v["tags"]:
        u.append("/api/search?tags=" + urllib.parse.quote(t))
    for b in v["blog_entries"]:
        u.append(f"/api/blog-entries/{b}")
    return list(dict.fromkeys(u))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

VOLATILE = [
    (re.compile(rb"\?v=\d+"), b"?v=N"),
]


def normalise(body, ctype):
    if "zip" in (ctype or "") or body[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(body))
            return json.dumps(sorted([i.filename, i.CRC, i.file_size] for i in zf.infolist())).encode()
        except zipfile.BadZipFile:
            pass
    for rx, rep in VOLATILE:
        body = rx.sub(rep, body)
    return body


def fetch(base, url, method="GET", data=None, headers=None):
    req = urllib.request.Request(base + url, data=data, method=method, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() if e.fp else b""


def snapshots(base, urls, bodies_dir):
    out = {}
    if bodies_dir:
        bodies_dir.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(urls):
        status, headers, body = fetch(base, url)
        ctype = headers.get("content-type") or headers.get("Content-Type")
        norm = normalise(body, ctype)
        out[url] = {
            "status": status,
            "content_type": ctype,
            "location": headers.get("location") or headers.get("Location"),
            "sha256": hashlib.sha256(norm).hexdigest(),
            "bytes": len(norm),
        }
        if bodies_dir:
            (bodies_dir / f"{i:03d}.txt").write_bytes(url.encode() + b"\n" + norm)
    return out


# --- 5. mutations -----------------------------------------------------------------------

def _last_audit(conn, upload_slug=None):
    r = conn.execute("SELECT method, path, status_code, form_body, affected_slugs, actor FROM audit_log "
                     "WHERE path LIKE '/api/%' AND op IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    row = list(r)
    if upload_slug:
        row = [x.replace(upload_slug, "<upload>") if isinstance(x, str) else x for x in row]
    return row


def _form(fields):
    return urllib.parse.urlencode(fields, doseq=True).encode()


def mutations(base, v):
    from core import db
    conn = sqlite3.connect(db.DB_PATH)
    host = urllib.parse.urlsplit(base).netloc
    same = {"Origin": f"http://{host}", "Content-Type": "application/x-www-form-urlencoded"}
    res = {}

    def step(name, url, fields=None, method="POST", headers=None, raw=None):
        hdrs = headers if headers is not None else same
        data = raw if raw is not None else (_form(fields) if fields is not None else None)
        before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        status, _, body = fetch(base, url, method=method, data=data, headers=hdrs)
        added = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] - before
        res[name] = {"status": status, "audit_rows_added": added, "audit": _last_audit(conn) if added else None}
        return status, body

    # records with a non-empty description, so the revert writes a real value back
    item, orig = conn.execute("SELECT slug, description FROM capture_events WHERE redacted = 0 AND description != '' "
                              "ORDER BY id LIMIT 1").fetchone()
    step("item_edit", f"/api/image/{item}", {"description": orig + " [golden-master]"})
    step("item_edit_revert", f"/api/image/{item}", {"description": orig})
    res["item_reverted"] = conn.execute("SELECT description FROM capture_events WHERE slug = ?", (item,)).fetchone()[0] == orig

    pid, pdesc = conn.execute("SELECT id, description FROM projects WHERE description != '' ORDER BY id LIMIT 1").fetchone()
    step("card_edit", f"/api/projects/{pid}", {"description": pdesc + " [golden-master]"})
    step("card_edit_revert", f"/api/projects/{pid}", {"description": pdesc})
    res["card_reverted"] = conn.execute("SELECT description FROM projects WHERE id = ?", (pid,)).fetchone()[0] == pdesc

    step("provenance_add", "/api/provenance-options/card", {"key": "golden_master", "label": "Golden master"})
    step("provenance_retire", "/api/provenance-options/card/golden_master/retire", {})

    boundary = "gmboundary547"
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"golden-master.txt\"\r\n"
               f"Content-Type: text/plain\r\n\r\ngolden master upload #547\r\n--{boundary}--\r\n").encode()
    status, _, body = fetch(base, "/api/upload", method="POST", data=payload, headers={
        "Origin": f"http://{host}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        up = json.loads(body)
        up_slug = up.get("slug") or (up.get("item") or {}).get("slug")
    except Exception:
        up_slug = None
    res["upload"] = {"status": status, "audit": _last_audit(conn, up_slug), "got_slug": bool(up_slug)}
    if up_slug:
        status, _, _ = fetch(base, f"/api/image/{up_slug}/delete", method="POST", data=b"", headers=same)
        res["upload_delete"] = {"status": status, "audit": _last_audit(conn, up_slug)}
        res["upload_gone"] = conn.execute("SELECT COUNT(*) FROM capture_events WHERE slug = ?", (up_slug,)).fetchone()[0] == 0

    key = "youtube_data_api_key"
    current = db.get_setting(key)
    if current:
        step("settings_noop", "/api/settings", {"key": key, "value": current})
        audit = res["settings_noop"]["audit"]
        res["settings_value_redacted"] = bool(audit) and current not in (audit[3] or "")
    step("guard_evil_origin", f"/api/image/{item}", {"description": "evil"},
         headers={"Origin": "http://evil.example", "Content-Type": "application/x-www-form-urlencoded"})
    res["item_untouched_by_evil"] = conn.execute("SELECT description FROM capture_events WHERE slug = ?", (item,)).fetchone()[0] == orig
    if v["blog_entries"]:
        step("json_gate_415", f"/api/blog-entries/{v['blog_entries'][0]}/projects", {"project_ids": "1"}, method="PUT")
    conn.close()
    return res


# --- 6. error probes (#548) -------------------------------------------------------------

def error_probes(base):
    from core import db
    conn = sqlite3.connect(db.DB_PATH)
    host = urllib.parse.urlsplit(base).netloc
    same = {"Origin": f"http://{host}", "Content-Type": "application/x-www-form-urlencoded"}
    out = {}

    def probe(name, url, fields=None, method="POST"):
        data = _form(fields) if fields is not None else (b"" if method == "POST" else None)
        status, headers, body = fetch(base, url, method=method, data=data,
                                      headers=same if method != "GET" else {})
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"_non_json_sha256": hashlib.sha256(body).hexdigest()}
        out[name] = {"status": status, "body": parsed}

    items = [r[0] for r in conn.execute("SELECT slug FROM capture_events WHERE redacted = 0 AND slug NOT IN "
                                        "(SELECT old_slug FROM item_revisions UNION SELECT new_slug FROM item_revisions) "
                                        "ORDER BY id LIMIT 2")]
    pid = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()[0]
    open_dec = conn.execute("SELECT id FROM pending_decisions WHERE resolved_at IS NULL ORDER BY id LIMIT 1").fetchone()

    probe("get_unknown_card_api", "/api/cards/no-such-card-golden", method="GET")
    probe("get_unknown_object_page", "/object/no-such-object-golden", method="GET")
    probe("get_unknown_route", "/api/no-such-route-golden", method="GET")
    probe("method_not_allowed", "/api/projects", method="PUT")
    probe("card_edit_bad_stage", f"/api/projects/{pid}", {"stage": "bogus-stage"})
    probe("card_edit_bad_parent", f"/api/projects/{pid}", {"parent_id": "not-a-number"})
    probe("decision_unknown", "/api/pending-decisions/999999999/resolve", {"choice": "x"})
    if open_dec:
        probe("decision_bad_choice", f"/api/pending-decisions/{open_dec[0]}/resolve", {"choice": "no-such-choice-golden"})
    probe("queue_bad_key", "/api/curator/needs/dismiss", {"nudge_key": "not-a-key"})
    probe("missing_form_field", f"/api/image/{items[0]}/superseded-by", {})
    probe("physical_piece_bad_date", f"/api/image/{items[0]}", {"type_metadata": json.dumps({"date_made": "june 2009"})})
    probe("revision_self", f"/api/image/{items[0]}/superseded-by", {"new_slug": items[0]})
    # a real cycle: A superseded by B, then B superseded by A; then remove the link again
    a, b = items[0], items[1]
    st, _, _ = fetch(base, f"/api/image/{a}/superseded-by", method="POST", data=_form({"new_slug": b}), headers=same)
    probe("revision_cycle", f"/api/image/{b}/superseded-by", {"new_slug": a})
    fetch(base, f"/api/image/{a}/revisions/remove", method="POST", data=b"", headers=same)
    out["_revision_link_made"] = st
    out["_revision_cleaned"] = conn.execute("SELECT COUNT(*) FROM item_revisions WHERE old_slug IN (?, ?) "
                                            "OR new_slug IN (?, ?)", (a, b, a, b)).fetchone()[0] == 0
    probe("thumbnail_refresh_unknown", "/api/image/no-such-object-golden/thumbnail/refresh", {})
    conn.close()
    return out


def _strip_shape(body):
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if k not in ("ok", "error")}
    return body


# --- compare ----------------------------------------------------------------------------

def _canon(x):
    return json.dumps(x, sort_keys=True)


def compare(a_path, b_path):
    a = json.loads(Path(a_path).read_text(encoding="utf-8"))
    b = json.loads(Path(b_path).read_text(encoding="utf-8"))
    ok = True

    ra, rb = {_canon(r) for r in a["routes"]}, {_canon(r) for r in b["routes"]}
    same_order = [r.get("path") for r in a["routes"]] == [r.get("path") for r in b["routes"]]
    print(f"routes:     {len(a['routes'])} vs {len(b['routes'])}; identical as a set: {ra == rb}"
          f" (registration order {'unchanged' if same_order else 'differs, see resolution'})")
    for r in sorted(ra - rb)[:20]:
        print("  only in A:", r)
    for r in sorted(rb - ra)[:20]:
        print("  only in B:", r)
    ok &= ra == rb

    diff = [k for k in sorted(set(a["resolution"]) | set(b["resolution"]))
            if a["resolution"].get(k) != b["resolution"].get(k)]
    print(f"resolution: {len(a['resolution'])} vs {len(b['resolution'])} (method, url) probes; mismatches: {len(diff)}")
    for k in diff[:50]:
        print(f"  {k}: {a['resolution'].get(k)}  ->  {b['resolution'].get(k)}")
    ok &= not diff

    same_api = _canon(a["openapi"]) == _canon(b["openapi"])
    print(f"openapi:    {len(a['openapi'].get('paths', {}))} vs {len(b['openapi'].get('paths', {}))} paths; identical (key-sorted): {same_api}")
    if not same_api:
        for p in sorted(set(a["openapi"]["paths"]) | set(b["openapi"]["paths"])):
            if _canon(a["openapi"]["paths"].get(p)) != _canon(b["openapi"]["paths"].get(p)):
                print("  differs:", p)
        if _canon(a["openapi"].get("components")) != _canon(b["openapi"].get("components")):
            print("  differs: components")
    ok &= same_api

    sa, sb = a.get("responses", {}), b.get("responses", {})
    rdiff = [u for u in sorted(set(sa) | set(sb)) if sa.get(u) != sb.get(u)]
    print(f"responses:  {len(sa)} vs {len(sb)} URLs; mismatches: {len(rdiff)}")
    for u in rdiff:
        print(f"  {u}: {sa.get(u)}  ->  {sb.get(u)}")
    ok &= not rdiff

    if "mutations" in a or "mutations" in b:
        ma, mb = a.get("mutations", {}), b.get("mutations", {})
        mdiff = [k for k in sorted(set(ma) | set(mb)) if _canon(ma.get(k)) != _canon(mb.get(k))]
        print(f"mutations:  {len(ma)} vs {len(mb)} checks; mismatches: {len(mdiff)}")
        for k in mdiff:
            print(f"  {k}: {ma.get(k)}  ->  {mb.get(k)}")
        ok &= not mdiff

    if "errors" in a or "errors" in b:
        ea, eb = a.get("errors", {}), b.get("errors", {})
        print(f"errors:     {len(ea)} vs {len(eb)} probes")
        for k in sorted(set(ea) | set(eb)):
            pa, pb = ea.get(k), eb.get(k)
            if not isinstance(pa, dict) or "body" not in pa or not isinstance(pb, dict):
                print(f"  {k}: {pa} -> {pb}")
                continue
            same_status = pa["status"] == pb["status"]
            only_shape = _canon(_strip_shape(pa["body"])) == _canon(_strip_shape(pb["body"]))
            verdict = "identical" if _canon(pa) == _canon(pb) else (
                "only ok/error added" if same_status and only_shape else "DIFFERS")
            print(f"  {k}: {pa['status']} -> {pb['status']}  {verdict}")
            if verdict != "identical":
                print(f"     before: {json.dumps(pa['body'], sort_keys=True)[:300]}")
                print(f"     after:  {json.dumps(pb['body'], sort_keys=True)[:300]}")

    print("RESULT:", "IDENTICAL" if ok else "DIFFERENT")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="write a snapshot to this JSON file")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="compare two snapshots")
    ap.add_argument("--base-url", default="http://localhost:80", help="running server for responses/mutations")
    ap.add_argument("--no-http", action="store_true", help="skip the HTTP snapshots (routes/resolution/openapi only)")
    ap.add_argument("--mutations", action="store_true", help="also run the write round-trips (writes to the DB)")
    ap.add_argument("--errors", action="store_true", help="also run the error probes (#548; one revision link made + removed)")
    args = ap.parse_args()

    if args.compare:
        sys.exit(compare(*args.compare))
    if not args.out:
        ap.error("--out or --compare is required")

    from web.app import app
    routes = route_table(app)
    v = db_values()
    resolution = {}
    for url in resolution_urls(routes, v):
        for m in METHODS:
            resolution[f"{m} {url}"] = resolve(app, m, url)
    snap = {"routes": routes, "resolution": resolution,
            "openapi": json.loads(json.dumps(app.openapi(), sort_keys=True)), "db_values": v}
    if not args.no_http:
        out = Path(args.out)
        base = args.base_url.rstrip("/")
        urls = snapshot_urls(v)
        # the curator queue's lazy group fragments: a few open groups of each type + a deferred one
        try:
            q = json.loads(fetch(base, "/api/curator/queue")[2])
            seen_types = {}
            for g in q.get("groups", []):
                if seen_types.get(g.get("type"), 0) < 2:
                    seen_types[g.get("type")] = seen_types.get(g.get("type"), 0) + 1
                    urls.append("/api/curator/queue/html?group=" + urllib.parse.quote(g["id"], safe=""))
            for g in q.get("deferred", [])[:2]:
                urls.append("/api/curator/queue/html?section=deferred&group=" + urllib.parse.quote(g["id"], safe=""))
        except Exception as e:
            print("curator queue groups unavailable:", e)
        snap["responses"] = snapshots(base, urls,
                                      out.with_name(out.name + ".bodies"))
    if args.mutations:
        snap["mutations"] = mutations(args.base_url.rstrip("/"), v)
    if args.errors:
        snap["errors"] = error_probes(args.base_url.rstrip("/"))
    Path(args.out).write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
    print(f"routes={len(routes)} resolution={len(resolution)} openapi_paths={len(snap['openapi'].get('paths', {}))}"
          f" responses={len(snap.get('responses', {}))} mutations={len(snap.get('mutations', {}))} -> {args.out}")


if __name__ == "__main__":
    main()
