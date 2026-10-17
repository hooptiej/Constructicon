#!/usr/bin/env python3
"""Drop-test an install with the type sampler (#720): upload one file of every type, check each, clean up.

    python3 scripts/drop_test.py --base-url URL [--samples DIR] [--keep] [--timeout 300]
                                 [--no-viewer] [--i-know-this-is-prod]

LIVE-SERVER TOOL: it writes to the target. Point it at a NEW or TEST install (constructicon-test, a
fresh customer install before anyone uses it). It refuses a target whose /api/version says env "prod"
unless --i-know-this-is-prod is passed.

1. Reads DIR/manifest.json (written by scripts/make_type_samples.py; default DIR = samples-out).
2. Uploads every sample file through the real API (`POST /api/upload`) and creates the no-file items
   (YouTube link, web page, written post) through `POST /api/content`, with the install token
   (`scripts/_http.py`: CONSTRUCTICON_TOKEN / CONSTRUCTICON_TOKEN_FILE / the install's own variables).
3. Waits for the background work (OCR, thumbnails, captions) by polling `GET /api/processing`.
4. Checks each item and prints one table row per file: expected type vs the detected type, thumbnail
   (`/f/<slug>/thumb`), extracted text (`/api/image/<slug>/text`), the object page (`/object/<slug>`
   answers 200), how `/f/<slug>` serves it (#701: inline or attachment), the raw view (#703:
   `/f/<slug>/raw` is text for code, a download otherwise), the content date where the file carries
   one, and (unless --no-viewer) what a VIEWER sees: a temporary viewer account is created, signed in,
   and must get 404 for every restricted item (item API and file) and 200 for the rest; it is deleted
   afterwards.
5. Unless --keep, deletes everything it uploaded (`POST /api/delete` with exactly its own slugs;
   never delete-all) and checks they are gone. The trash is emptied ONLY when it held nothing before
   this run and holds nothing but this run's items now (emptying is all-or-nothing); otherwise the
   files stay in the trash and purge on their own after 7 days, and the summary says so.

Ends with "ALL PASS: ..." or "FAILED: <file (what)>, ...", the run_checks verdict style.
Run it beside an install (from the host, #634):
    ./scripts/run_tool.sh <web container> scripts/drop_test.py --base-url http://localhost:<port> --samples <dir>
"""

import argparse
import http.cookiejar
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import _http

RUN = uuid.uuid4().hex[:8]
NOTE = f"type sampler drop test {RUN} (#720)"
EMPTY_TRASH_PHRASE = "EMPTY TRASH"


class Client:
    """HTTP to one install. `opener=None` uses the global opener _http.install() set (install token);
    a viewer client passes its own cookie opener (no token)."""

    def __init__(self, base, opener=None):
        self.base = base.rstrip("/")
        self.open = opener.open if opener else urllib.request.urlopen

    def request(self, method, path, data=None, headers=None, timeout=60):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with self.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def json(self, method, path, body=None, form=None, **kw):
        headers, data = {}, None
        if body is not None:
            data, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
        elif form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, h, raw = self.request(method, path, data=data, headers=headers, **kw)
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, {"raw": raw[:200].decode("utf-8", "replace")}

    def upload(self, name, data, description):
        b = "----droptest" + uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="description"\r\n\r\n{description}\r\n'.encode(),
                 (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n",
                 f"--{b}--\r\n".encode()]
        status, _h, raw = self.request("POST", "/api/upload", data=b"".join(parts),
                                       headers={"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=300)
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, {"raw": raw[:200].decode("utf-8", "replace")}


def _err(body):
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            return f"{e.get('code')}: {e.get('message')}"
        return str(body.get("detail") or body)[:160]
    return str(body)[:160]


def _disposition(headers):
    d = {k.lower(): v for k, v in headers.items()}.get("content-disposition", "")
    return d.split(";")[0].strip().lower() or "none"


def _ctype(headers):
    return {k.lower(): v for k, v in headers.items()}.get("content-type", "").split(";")[0].strip().lower()


# ---------------------------------------------------------------------------------------------- steps

def refuse_prod(api, allow_prod):
    status, body = api.json("GET", "/api/version")
    if status != 200:
        sys.exit(f"refusing: GET /api/version answered {status} ({_err(body)}); is the install token set "
                 "(CONSTRUCTICON_TOKEN_FILE) and the base URL right?")
    env = (body or {}).get("env")
    print(f"target: {api.base}  version {body.get('version')}  env {env}")
    if env == "prod" and not allow_prod:
        sys.exit("refusing: this install reports env 'prod'. A drop test uploads and deletes items; run it on "
                 "a new or test install, or pass --i-know-this-is-prod if this really is a fresh install.")


def wait_for_processing(api, slugs, timeout):
    """Poll /api/processing until none of `slugs` is in flight. Returns the slugs still in flight."""
    deadline = time.time() + timeout
    pending = set(slugs)
    while pending and time.time() < deadline:
        still = set()
        chunk = sorted(pending)
        for i in range(0, len(chunk), 200):
            status, body = api.json("GET", "/api/processing?session=" + ",".join(chunk[i:i + 200]))
            if status != 200:
                print(f"  processing poll answered {status}: {_err(body)}")
                return pending
            for it in body.get("items", []):
                if it.get("slug") in pending and it.get("overall") == "processing":
                    still.add(it["slug"])
        pending = still
        if pending:
            print(f"  waiting for {len(pending)} item(s) to finish processing...", flush=True)
            time.sleep(3)
    return pending


def make_viewer(api):
    """A temporary viewer account and a signed-in cookie client for it, or (None, None, reason)."""
    username = f"droptest-viewer-{RUN}"
    password = secrets.token_urlsafe(24)
    status, body = api.json("POST", "/api/users", body={"username": username, "password": password,
                                                       "role": "viewer", "display_name": "Drop test viewer"})
    if status != 200:
        return None, None, f"could not create a viewer ({status} {_err(body)})"
    status, body = api.json("GET", "/api/users")
    uid = next((u["id"] for u in (body or {}).get("users", []) if u.get("username") == username), None)
    jar = http.cookiejar.CookieJar()
    viewer = Client(api.base, opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)))
    status, body = viewer.json("POST", "/api/auth/login", body={"username": username, "password": password})
    if status != 200:
        return uid, None, f"viewer sign-in failed ({status} {_err(body)})"
    return uid, viewer, ""


def check_item(api, viewer, exp, slug, upload_type):
    """Run every check for one uploaded file. Returns (cells dict, problems list)."""
    cells, problems = {}, []
    cells["detected"] = upload_type or "-"
    if upload_type != exp["media_type"]:
        problems.append(f"landed as {upload_type!r}, expected {exp['media_type']!r}")
    status, item = api.json("GET", f"/api/image/{slug}")
    if status != 200:
        problems.append(f"item API answered {status}")
        item = {}

    status, h, body = api.get(f"/f/{slug}/thumb")
    got = status == 200 and _ctype(h).startswith("image/") and len(body) > 0
    cells["thumb"] = "yes" if got else "no"
    want = exp["thumbnail"]
    if want == "network":
        cells["thumb"] += "" if got else " (net)"
    elif got != bool(want):
        problems.append(f"thumbnail {'missing' if want else 'present but none expected'} ({status})")

    status, text = api.json("GET", f"/api/image/{slug}/text?limit=200")
    chars = (text or {}).get("chars", 0) if status == 200 else 0
    got = chars > 0
    cells["text"] = f"yes ({chars})" if got else f"no ({(text or {}).get('ocr_status') or '-'})"
    want = exp["text"]
    if want == "network":
        pass
    elif got != bool(want):
        problems.append(f"text {'missing' if want else 'present but none expected'} "
                        f"(ocr_status {(text or {}).get('ocr_status')})")

    status, _h, _b = api.get(f"/object/{slug}")
    cells["page"] = str(status)
    if status != 200:
        problems.append(f"object page answered {status}")

    if "disposition" in exp:
        status, h, _b = api.get(f"/f/{slug}")
        disp = _disposition(h) if status == 200 else str(status)
        cells["/f/"] = {"attachment": "attach"}.get(disp, disp)
        if disp != exp["disposition"]:
            problems.append(f"/f/ served {disp}, expected {exp['disposition']}")
        status, h, _b = api.get(f"/f/{slug}/raw")
        raw_disp = _disposition(h) if status == 200 else str(status)
        is_text = status == 200 and _ctype(h) == "text/plain" and raw_disp == "inline"
        cells["raw"] = "text" if is_text else ("dl" if raw_disp == "attachment" else raw_disp)
        if exp["raw"] == "text" and not is_text:
            problems.append(f"raw view not text ({status}, {_ctype(h)}, {raw_disp})")
        if exp["raw"] == "download" and raw_disp != "attachment":
            problems.append(f"raw view should be the download, got {raw_disp}")

    if exp.get("date"):
        cells["date"] = "yes" if item.get("content_date") else "no"
        if not item.get("content_date"):
            problems.append("no content_date from the file's own date")
    else:
        cells["date"] = "-"

    if viewer is None:
        cells["viewer"] = "-"
    else:
        s_api, _ = viewer.json("GET", f"/api/image/{slug}")
        if exp["restricted"]:
            s_file, _h, _b = viewer.get(f"/f/{slug}")
            hidden = s_api == 404 and s_file == 404
            cells["viewer"] = "hidden" if hidden else f"LEAK {s_api}/{s_file}"
            if not hidden:
                problems.append(f"restricted item visible to a viewer (api {s_api}, file {s_file})")
        else:
            cells["viewer"] = "sees" if s_api == 200 else str(s_api)
            if s_api != 200:
                problems.append(f"viewer can't see an ordinary item ({s_api})")
    return cells, problems


COLS = [("file", 34), ("expected", 12), ("detected", 12), ("thumb", 10), ("text", 14), ("page", 5),
        ("/f/", 7), ("raw", 5), ("date", 5), ("viewer", 7), ("result", 6)]


def row(values):
    return " | ".join(str(values.get(name, "")).ljust(width) for name, width in COLS)


def cleanup(api, slugs, trash_before):
    """Delete exactly `slugs`, confirm they're gone, and empty the trash only if it is all ours."""
    if not slugs:
        return True, "nothing to delete"
    status, body = api.json("POST", "/api/delete", form={"slugs": slugs})
    if status != 200:
        return False, f"delete answered {status}: {_err(body)}"
    left = [s for s in slugs if api.json("GET", f"/api/image/{s}")[0] != 404]
    if left:
        return False, f"{len(left)} item(s) still answer after the delete: {', '.join(left[:5])}"
    status, trash = api.json("GET", "/api/trash")
    ours = set(slugs)
    in_trash = {t["slug"] for t in (trash or {}).get("items", [])}
    if trash_before == 0 and in_trash and in_trash <= ours:
        status, body = api.json("POST", "/api/trash/empty", form={"confirm": EMPTY_TRASH_PHRASE})
        if status != 200:
            return False, f"deleted {len(slugs)}; emptying the trash answered {status}: {_err(body)}"
        return True, f"deleted {len(slugs)} item(s); trash emptied (it held only this run's {body.get('purged')} files)"
    if in_trash - ours:
        return True, (f"deleted {len(slugs)} item(s); the trash also holds other deletes, so it was NOT emptied: "
                      "this run's files purge on their own after 7 days")
    return True, f"deleted {len(slugs)} item(s) (nothing of ours left in the trash)"


def main():
    ap = argparse.ArgumentParser(description="Drop-test an install with the type sampler (#720).")
    ap.add_argument("--base-url", required=True, help="the install, e.g. http://localhost:80")
    ap.add_argument("--samples", default="samples-out", help="folder written by make_type_samples.py")
    ap.add_argument("--keep", action="store_true", help="leave the uploaded items in place")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for processing (default 300)")
    ap.add_argument("--no-viewer", action="store_true", help="skip the viewer (restricted-item) checks")
    ap.add_argument("--i-know-this-is-prod", action="store_true", help="allow a target whose env is prod")
    args = ap.parse_args()

    samples = Path(args.samples)
    manifest_path = samples / "manifest.json"
    if not manifest_path.exists():
        sys.exit(f"no {manifest_path}: generate the samples first (scripts/make_type_samples.py {samples})")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if not _http.install(args.base_url):
        sys.exit("refusing: no install token found (set CONSTRUCTICON_TOKEN_FILE)")
    api = Client(args.base_url)
    refuse_prod(api, args.i_know_this_is_prod)
    if manifest.get("skipped_executables"):
        print(f"note: the samples were generated without {len(manifest['skipped_executables'])} executable-class "
              "files (not Linux); those types are not tested this run")

    status, trash = api.json("GET", "/api/trash")
    trash_before = (trash or {}).get("count", 0) if status == 200 else -1

    uploaded = []  # (exp, slug or None, detected type, problem)
    for exp in manifest["files"]:
        data = (samples / exp["file"]).read_bytes()
        status, body = api.upload(exp["file"], data, NOTE)
        if status == 200:
            uploaded.append((exp, body["slug"], body.get("media_type"), body.get("pending_decision_id")))
        else:
            uploaded.append((exp, None, None, f"upload answered {status}: {_err(body)}"))
        print(f"  uploaded {exp['file']:34} -> {status} {body.get('media_type') if status == 200 else _err(body)}",
              flush=True)
    for c in manifest.get("content", []):
        fields = dict(c["fields"], description=NOTE)
        status, body = api.json("POST", "/api/content", form=fields)
        if status == 200:
            uploaded.append((c, body["slug"], body.get("media_type"), None))
        else:
            uploaded.append((c, None, None, f"/api/content answered {status}: {_err(body)}"))
        print(f"  created  {c['name']:34} -> {status} {body.get('media_type') if status == 200 else _err(body)}",
              flush=True)

    slugs = [u[1] for u in uploaded if u[1]]
    stuck = wait_for_processing(api, slugs, args.timeout)

    viewer_uid, viewer, viewer_note = (None, None, "skipped (--no-viewer)")
    if not args.no_viewer:
        viewer_uid, viewer, viewer_note = make_viewer(api)

    print()
    print(row({n: n for n, _w in COLS}))
    print("-+-".join("-" * w for _n, w in COLS))
    failed = []
    for exp, slug, detected, extra in uploaded:
        name = exp.get("file") or exp.get("name")
        values = {"file": name, "expected": exp["media_type"]}
        if not slug:
            values.update(detected="-", result="FAIL")
            print(row(values))
            failed.append(f"{name} ({extra})")
            continue
        cells, problems = check_item(api, viewer, exp, slug, detected)
        if slug in stuck:
            problems.append("still processing at the timeout")
        values.update(cells, result="PASS" if not problems else "FAIL")
        if not exp.get("file"):
            values.update({"/f/": "n/a", "raw": "n/a"})
        print(row(values))
        if problems:
            failed.append(f"{name} ({'; '.join(problems)})")

    files = sum(1 for u in uploaded if u[0].get("file"))
    print()
    if viewer is None and not args.no_viewer:
        print(f"viewer checks: {viewer_note}")
    asked = [u[0]["file"] for u in uploaded if u[0].get("file") and isinstance(u[3], int)]
    if asked:
        print(f"queued a question (Needs your input) for: {', '.join(asked)}")

    if viewer_uid is not None:
        status, body = api.json("POST", f"/api/users/{viewer_uid}/delete")
        print(f"temporary viewer account deleted: {status == 200}" + ("" if status == 200 else f" ({_err(body)})"))
    if args.keep:
        print(f"--keep: left {len(slugs)} item(s) on the install (description '{NOTE}')")
    else:
        ok, msg = cleanup(api, slugs, trash_before)
        print(f"cleanup: {msg}")
        if not ok:
            failed.append(f"cleanup ({msg})")

    total = len(uploaded)
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)
    print(f"ALL PASS: {files} files + {total - files} content items landed as their expected types")


if __name__ == "__main__":
    main()
