#!/usr/bin/env python3
"""Blind man's buff (#709): a write may not touch an item the caller can't see. Throwaway DB, no server.

    python scripts/test_bulk_visibility_709.py

Same fixtures as test_ownership_sensitive.py: real sessions for an admin A, editors E1 and E2 and a
viewer V, anonymous, and the per-run install token. E1 uploads a SENSITIVE file (only E1 and admins
can see it) and E2 an ordinary one.
  1. web, as E2: POST /api/delete, /api/bulk/add-to-project, /api/bulk/attach-tags (and sensitive /
     provenance) with the hidden slug alone and mixed with visible ones -> 403 `forbidden` that says
     why (owner decision on #709: "tell them no, and why"): "Nothing was changed: 1 of the 3 selected
     items is marked sensitive, ...", details {refused, reason, reasons}, and NOTHING written (change
     log, rows, card members, tags, trash all unchanged); a restricted TYPE says so; two hidden = "2 of";
  2. unknown slugs keep their old meaning (skipped; a request of only unknown slugs is a 200 no-op);
     viewer 403, anonymous 401 (the route roles, unchanged);
  3. the uploader E1, the admin A and the token succeed on the same item;
  4. the service ops refuse the same way (403 forbidden, AppError with details) when run as a non-admin actor (user:os_ed2): items.delete,
     update, relate, unrelate, redact, membership add/remove, tags attach/detach/set/merge;
  5. the MCP tools, run as a non-admin actor (what a per-user token would be, #604 step 5), refuse
     with the same 403 forbidden, message and details, and write nothing; run as the MCP itself (admin) they still work.
Prints `N failure(s)` or `all checks passed`.
"""

import io
import os
import re
import secrets
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("bulkvis709-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, items, membership, policy, users  # noqa: E402
from core import tags as tags_svc  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def new_client():
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def png_bytes(seed):
    from PIL import Image
    img = Image.new("RGB", (32, 24), (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def err(r):
    try:
        body = r.json()
    except ValueError:
        return None, None
    e = body.get("error", {}) if isinstance(body, dict) else {}
    return e.get("code"), e.get("message")


db.init_db()
users.limiter.reset()

print("--- fixtures ---")
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("bv_admin", "admin"), ("bv_ed1", "editor"), ("bv_ed2", "editor"), ("bv_view", "viewer")):
        PW[name] = "bv-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
C = {"anonymous": new_client(), "token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for key, name in (("A", "bv_admin"), ("E1", "bv_ed1"), ("E2", "bv_ed2"), ("V", "bv_view")):
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    check(f"{key} signs in", r.status_code == 200, r.text[:200])
    C[key] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in ("A", "E1", "E2", "V") else dict(SAME)) for k, c in C.items()}


def upload(who, filename, seed, sensitive=False, data=None, mime="image/png"):
    fields = {"description": f"bulkvis {filename}"}
    if sensitive:
        fields["sensitive"] = "true"
    body = data if data is not None else png_bytes(seed)
    r = C[who].post("/api/upload", files={"file": (filename, body, mime)}, data=fields, headers=H[who])
    check(f"{who} uploads {filename}", r.status_code == 200, r.text[:200])
    return r.json()["slug"]


SECRET = upload("E1", "bv-secret.png", 1, sensitive=True)
SECRET2 = upload("E1", "bv-secret2.png", 2, sensitive=True)
VIS = upload("E2", "bv-visible.png", 3)
VIS2 = upload("E2", "bv-visible2.png", 4)
VIS3 = upload("E2", "bv-visible3.png", 5)
PEM = (b"-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUOwnsensFixtureNotARealCert0wCgYIKoZIzj0EAwIw\n"
       b"-----END CERTIFICATE-----\n")
CERT = upload("E1", "bv-cert.pem", 0, data=PEM, mime="application/x-pem-file")
check("fixture: CERT is a restricted type (not flagged), hidden from E2",
      policy.is_type_restricted(db.get_by_slug(CERT)) and not policy.is_flagged(db.get_by_slug(CERT))
      and not policy.can_view(db.get_by_slug(CERT), "user:bv_ed2"))
check("fixture: SECRET is hidden from E2 and visible to E1",
      not policy.can_view(db.get_by_slug(SECRET), "user:bv_ed2") and policy.can_view(db.get_by_slug(SECRET), "user:bv_ed1"))
CARD = C["token"].post("/api/projects", data={"title": "Bulkvis Card"}).json()
CARD_ID = CARD["id"]
with actor.acting_as(actor.ACTOR_SCRIPT):
    tags_svc.attach(SECRET, ["bv-existing"])  # something for detach to (not) remove
    membership.add_files(CARD_ID, [VIS2], **membership.NO_EFFECTS)  # something for remove to (not) remove
    items.relate(VIS2, SECRET)  # something for unrelate to (not) remove
UNKNOWN = "no-such-slug-709"

conn = sqlite3.connect(db.DB_PATH)
conn.row_factory = sqlite3.Row


def snapshot():
    q = lambda sql, *a: [tuple(r) for r in conn.execute(sql, a).fetchall()]  # noqa: E731
    return {
        "change_log": q("SELECT COUNT(*), MAX(id) FROM audit_log WHERE op IS NOT NULL"),
        "items": q("SELECT slug, tags, description, sensitive, redacted FROM capture_events ORDER BY slug"),
        "members": q("SELECT project_id, post_slug FROM project_items ORDER BY 1, 2"),
        "post_tags": q("SELECT post_slug, tag_id FROM post_tags ORDER BY 1, 2"),
        "tags": q("SELECT id, name FROM blog_tags ORDER BY id"),
        "relations": q("SELECT slug_a, slug_b FROM capture_event_relations ORDER BY 1, 2"),
        "trash": q("SELECT batch_id, slug FROM trash ORDER BY 1, 2"),
        "cover": q("SELECT cover_slug FROM projects WHERE id = ?", CARD_ID),
    }


def unchanged(before):
    after = snapshot()
    return [k for k in before if before[k] != after[k]]


# ---- 1. web, as E2: the hidden item refuses the whole request -----------------------------------
print("--- 1. web: E2 vs E1's sensitive item ---")
DOORS = {
    "delete": ("/api/delete", {}),
    "add-to-card": ("/api/bulk/add-to-project", {"project_id": str(CARD_ID)}),
    "tag": ("/api/bulk/attach-tags", {"tag_names": ["bv-new-tag"]}),
}
SENS_ONE = "Nothing was changed: this item is marked sensitive, and only its uploader or an admin can change it."
SENS_1_OF = "Nothing was changed: 1 of the {n} selected items is marked sensitive, and only its uploader or an admin can change it."
SENS_2_OF = ("Nothing was changed: 2 of the {n} selected items are marked sensitive, and only their uploader or an "
             "admin can change them.")
CERT_1_OF = ("Nothing was changed: 1 of the {n} selected items is a restricted file type (keys, certificates), which "
             "only an admin or its uploader can change.")
MIXED_2_OF = ("Nothing was changed: 2 of the {n} selected items are marked sensitive or a restricted file type, and "
              "only their uploaders or an admin can change them.")


def details(r):
    try:
        return (r.json().get("error") or {}).get("details") or {}
    except ValueError:
        return {}


def refused_ok(label, r, before, want_msg, want_refused, want_reason):
    code, msg = err(r)
    diff = unchanged(before)
    d = details(r)
    check(f"{label}: 403 forbidden, nothing written", r.status_code == 403 and code == "forbidden" and not diff,
          f"{r.status_code} {code} {diff} {r.text[:200]}")
    check(f"{label}: the message says how many and why", msg == want_msg, msg)
    check(f"{label}: details name the refused slugs and the reason",
          d.get("refused") == want_refused and d.get("reason") == want_reason, d)
    return r


FIRST = None
for label, (path, extra) in DOORS.items():
    for combo_name, slugs, want in (("alone", [SECRET], SENS_ONE),
                                    ("mixed with a visible one", [VIS, SECRET], SENS_1_OF.format(n=2)),
                                    ("hidden first, then visible + unknown", [SECRET, VIS, UNKNOWN], SENS_1_OF.format(n=3))):
        before = snapshot()
        r = C["E2"].post(path, data={"slugs": slugs, **extra}, headers=H["E2"])
        refused_ok(f"E2 {label} ({combo_name})", r, before, want, [SECRET], "sensitive")
        FIRST = FIRST or r
    before = snapshot()
    r = C["E2"].post(path, data={"slugs": [VIS, SECRET, SECRET2], **extra}, headers=H["E2"])
    refused_ok(f"E2 {label} (two hidden of three)", r, before, SENS_2_OF.format(n=3), [SECRET, SECRET2], "sensitive")
    before = snapshot()
    r = C["E2"].post(path, data={"slugs": [VIS, CERT], **extra}, headers=H["E2"])
    refused_ok(f"E2 {label} (a restricted type)", r, before, CERT_1_OF.format(n=2), [CERT], "restricted_type")
    before = snapshot()
    r = C["E2"].post(path, data={"slugs": [VIS, SECRET, CERT], **extra}, headers=H["E2"])
    refused_ok(f"E2 {label} (one of each)", r, before, MIXED_2_OF.format(n=3), [SECRET, CERT], "mixed")
check("the web answer keeps `detail` = the message (the old page JS reads it)",
      FIRST.json().get("detail") == FIRST.json()["error"]["message"], FIRST.text[:200])
for label, path, extra in (("sensitive", "/api/bulk/sensitive", {"sensitive": "true"}),
                           ("provenance", "/api/bulk/provenance", {"provenance": "found"})):
    before = snapshot()
    r = C["E2"].post(path, data={"slugs": [VIS, SECRET], **extra}, headers=H["E2"])
    refused_ok(f"E2 bulk {label} [visible, hidden]", r, before, SENS_1_OF.format(n=2), [SECRET], "sensitive")
    before = snapshot()
    r = C["E2"].post(path, data={"slugs": [VIS, UNKNOWN], **extra}, headers=H["E2"])
    check(f"E2 bulk {label} [visible, unknown]: still 404 not_found for the unknown slug, nothing written",
          r.status_code == 404 and err(r)[0] == "not_found" and not unchanged(before), r.text[:200])
check("the hidden item still exists after all that", db.get_by_slug(SECRET) is not None)
check("no tag 'bv-new-tag' was created", tags_svc.find("bv-new-tag") is None)

# ---- 2. unknown slugs, roles ----------------------------------------------------------------------
print("--- 2. unknown slugs and roles ---")
before = snapshot()
r = C["E2"].post("/api/delete", data={"slugs": [UNKNOWN]}, headers=H["E2"])
check("E2 delete of only an unknown slug: 200, deleted 0, nothing written",
      r.status_code == 200 and r.json()["deleted"] == 0 and not unchanged(before), r.text[:200])
r = C["E2"].post("/api/bulk/attach-tags", data={"slugs": [VIS, UNKNOWN], "tag_names": ["bv-e2-tag"]}, headers=H["E2"])
check("E2 tag [visible, unknown]: 200, count 1 (the unknown one skipped)",
      r.status_code == 200 and r.json()["count"] == 1 and "bv-e2-tag" in db.get_by_slug(VIS)["tags"], r.text[:200])
r = C["E2"].post("/api/bulk/add-to-project", data={"slugs": [VIS, UNKNOWN], "project_id": str(CARD_ID)}, headers=H["E2"])
check("E2 add-to-card [visible, unknown]: 200, count 1, the visible one is on the card",
      r.status_code == 200 and r.json()["count"] == 1
      and VIS in [i["slug"] for i in db.list_project_items(CARD_ID)], r.text[:200])
r = C["E2"].post("/api/bulk/add-to-project", data={"slugs": [VIS2], "project_id": "999999"}, headers=H["E2"])
check("E2 add-to-card with an unknown card: still the silent no-op (200, count 1)",
      r.status_code == 200 and r.json() == {"count": 1}, r.text[:200])
before = snapshot()
r = C["E2"].post("/api/bulk/add-to-project", data={"slugs": [SECRET], "project_id": "999999"}, headers=H["E2"])
check("E2 add-to-card of the hidden item with an unknown card: refused too (403, nothing written)",
      r.status_code == 403 and not unchanged(before), r.text[:200])
r = C["E2"].post("/api/delete", data={"slugs": [VIS3, UNKNOWN]}, headers=H["E2"])
check("E2 delete [own visible, unknown]: 200, deleted 1",
      r.status_code == 200 and r.json()["deleted"] == 1 and db.get_by_slug(VIS3) is None, r.text[:200])
for label, (path, extra) in DOORS.items():
    rv = C["V"].post(path, data={"slugs": [VIS], **extra}, headers=H["V"])
    ra = C["anonymous"].post(path, data={"slugs": [VIS], **extra}, headers=H["anonymous"])
    check(f"{label}: viewer 403, anonymous 401 (route roles unchanged)",
          rv.status_code == 403 and ra.status_code == 401, f"{rv.status_code} {ra.status_code}")

# ---- 3. the uploader, the admin and the token succeed ---------------------------------------------
print("--- 3. who may ---")
r = C["E1"].post("/api/bulk/attach-tags", data={"slugs": [SECRET, VIS], "tag_names": ["bv-e1-tag"]}, headers=H["E1"])
check("E1 (uploader) tags [own hidden, visible]: 200, count 2",
      r.status_code == 200 and r.json()["count"] == 2 and "bv-e1-tag" in db.get_by_slug(SECRET)["tags"], r.text[:200])
r = C["E1"].post("/api/bulk/add-to-project", data={"slugs": [SECRET], "project_id": str(CARD_ID)}, headers=H["E1"])
check("E1 adds own hidden item to the card: 200, count 1",
      r.status_code == 200 and r.json()["count"] == 1 and SECRET in [i["slug"] for i in db.list_project_items(CARD_ID)],
      r.text[:200])
r = C["A"].post("/api/bulk/attach-tags", data={"slugs": [SECRET2], "tag_names": ["bv-admin-tag"]}, headers=H["A"])
check("admin tags E1's hidden item: 200", r.status_code == 200 and r.json()["count"] == 1, r.text[:200])
r = C["token"].post("/api/bulk/add-to-project", data={"slugs": [SECRET2], "project_id": str(CARD_ID)})
check("token adds E1's hidden item to the card: 200", r.status_code == 200 and r.json()["count"] == 1, r.text[:200])
r = C["A"].post("/api/delete", data={"slugs": [SECRET2, UNKNOWN]}, headers=H["A"])
check("admin deletes E1's hidden item: 200, deleted 1", r.status_code == 200 and r.json()["deleted"] == 1
      and db.get_by_slug(SECRET2) is None, r.text[:200])
undo = C["A"].post(f"/api/changes/{r.json()['batch_id']}/undo", headers=H["A"])
check("admin's delete undoes", undo.status_code == 200 and db.get_by_slug(SECRET2) is not None, undo.text[:200])

# ---- 4. the service ops refuse a hidden item for a non-admin actor ---------------------------------
print("--- 4. service ops as user:bv_ed2 ---")
TAG_ID = tags_svc.find("bv-existing")["id"]
SERVICE = {
    "items.delete(missing_ok)": lambda: items.delete([VIS, SECRET], missing_ok=True),
    "items.delete": lambda: items.delete([SECRET]),
    "items.update": lambda: items.update(SECRET, description="pwned"),
    "items.set_caption": lambda: items.set_caption(SECRET, "pwned"),
    "items.relate": lambda: items.relate(VIS, SECRET),
    "items.unrelate": lambda: items.unrelate(VIS2, SECRET),
    "items.redact": lambda: items.redact(SECRET),
    "items.set_provenance": lambda: items.set_provenance([VIS, SECRET], None),
    "membership.add_files(missing_ok)": lambda: membership.add_files(CARD_ID, [VIS, SECRET], missing_ok=True, **membership.UI_EFFECTS),
    "membership.remove_files": lambda: membership.remove_files(CARD_ID, [SECRET]),
    "tags.attach": lambda: tags_svc.attach(SECRET, ["pwned"]),
    "tags.detach": lambda: tags_svc.detach(SECRET, TAG_ID),
    "tags.set_item_tags": lambda: tags_svc.set_item_tags(SECRET, []),
    "tags.merge_item_tags": lambda: tags_svc.merge_item_tags([VIS, SECRET], ["pwned"]),
}
for label, fn in SERVICE.items():
    before = snapshot()
    with actor.acting_as("user:bv_ed2"):
        try:
            fn()
            got = "no error"
        except AppError as exc:
            got = f"{exc.code}: {exc.status} {exc.details.get('refused')} {exc}"
        except Exception as exc:  # noqa: BLE001 -- reported as a failure below
            got = f"{type(exc).__name__}: {exc}"
    diff = unchanged(before)
    check(f"service {label} as E2: 403 forbidden naming the hidden item, nothing written",
          got.startswith(f"forbidden: 403 ['{SECRET}']") and "marked sensitive" in got and not diff, f"{got} {diff}")
with actor.acting_as("user:bv_ed2"):
    r_unk = items.delete([UNKNOWN], missing_ok=True)
check("service items.delete(missing_ok) of an unknown slug as E2: no-op, as before", r_unk.data["deleted"] == 0)
with actor.acting_as("user:bv_ed1"):
    tags_svc.attach(SECRET, ["bv-e1-direct"])
check("service tags.attach as E1 (the uploader) works",
      conn.execute("SELECT COUNT(*) FROM post_tags WHERE post_slug = ? AND tag_id = ?",
                   (SECRET, tags_svc.find("bv-e1-direct")["id"])).fetchone()[0] == 1)

# ---- 5. the MCP tools, as a non-admin actor (a future per-user token) ------------------------------
print("--- 5. MCP tools ---")
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)

_real_call = mcp._call_as_mcp


def as_user(name):
    def call(fn, args, kwargs):
        with actor.acting_as(f"user:{name}"):
            try:
                return True, fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- same shape as the real wrapper
                payload = mcp._error_payload(exc)
                if payload is None:
                    raise
                return False, payload
    return call


def is_refused(value):
    e = (value.get("error") or {}) if isinstance(value, dict) else {}
    return (value.get("ok") is False and e.get("code") == "forbidden" and SECRET in (e.get("details") or {}).get("refused", [])
            and e.get("message", "").startswith("Nothing was changed:") and "marked sensitive" in e.get("message", ""))


MCP_CALLS = {
    "delete": lambda: mcp.constructicon_delete(SECRET),
    "delete_multiple": lambda: mcp.constructicon_delete_multiple([VIS, SECRET, UNKNOWN]),
    "add_to_project": lambda: mcp.constructicon_add_to_project(SECRET, CARD_ID),
    "add_items_to_project": lambda: mcp.constructicon_add_items_to_project(CARD_ID, [VIS, SECRET]),
    "remove_from_project": lambda: mcp.constructicon_remove_from_project(SECRET, CARD_ID),
    "set_project_writeup": lambda: mcp.constructicon_set_project_writeup(CARD_ID, SECRET),
    "attach_tags": lambda: mcp.constructicon_attach_tags(SECRET, ["pwned"]),
    "detach_tag": lambda: mcp.constructicon_detach_tag(SECRET, "bv-existing"),
    "add_related": lambda: mcp.constructicon_add_related(VIS, SECRET),
    "remove_related": lambda: mcp.constructicon_remove_related(VIS, SECRET),
    "update": lambda: mcp.constructicon_update(SECRET, description="pwned"),
    "set_sensitive": lambda: mcp.constructicon_set_sensitive(SECRET, True),
}
mcp._call_as_mcp = as_user("bv_ed2")
try:
    for label, fn in MCP_CALLS.items():
        before = snapshot()
        value = fn()
        diff = unchanged(before)
        check(f"MCP {label} as E2: 403 forbidden with the message and details, nothing written",
              is_refused(value) and not diff, f"{value!r:.300} {diff}")
finally:
    mcp._call_as_mcp = _real_call
value = mcp.constructicon_attach_tags(SECRET, ["bv-mcp-tag"])
check("MCP attach_tags as the MCP itself (admin): works on the hidden item",
      isinstance(value, dict) and value.get("slug") == SECRET and value.get("ok") is not False, f"{value!r:.200}")
value = mcp.constructicon_delete_multiple([SECRET2, UNKNOWN])
check("MCP delete_multiple as the MCP itself: deletes it, the unknown slug skipped",
      isinstance(value, dict) and value.get("deleted") == 1 and db.get_by_slug(SECRET2) is None, f"{value!r:.200}")

conn.close()
print()
print(f"{len(FAILS)} failure(s)" if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
