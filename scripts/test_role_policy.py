#!/usr/bin/env python3
"""The role seam and the item policy (#557, groundwork for auth #467). Throwaway DB, no server.

    python scripts/test_role_policy.py

1. Today (nothing flipped): every route has exactly one role label; a restricted item (a real
   certificate-only PEM, type certkey) is served by its direct doors and listed on its card, its
   blog entry and its neighbour's Related list, but hidden from search and never exported; the
   request log records each write's required role.
2. The #467 switch (policy.RESTRICTED_VIEW_ROLE = admin, the actor a viewer): every door refuses
   the restricted item with 404 not_found in the shared error shape, every list drops it, and an
   ordinary item is untouched. As an admin it is served again.
3. A stricter proof that no door bypasses the policy: can_view monkeypatched to deny EVERYTHING,
   and every direct door refuses even an ordinary item.
4. The role hook (roles.ENFORCE, role_of): a viewer gets 403 forbidden on admin and editor routes,
   keeps viewer and public routes; an admin passes.
Everything is reset afterwards. Exits 1 if any check fails.
"""

import datetime
import io
import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="rolepolicy-")
os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["CONSTRUCTICON_STORAGE_DIR"] = os.path.join(TMP, "storage")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, blog, cards, db, items, membership, policy, roles, site_export, storage  # noqa: E402
from web import app as webapp  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def pem_certificate():
    """A real self-signed certificate (certificate only, no private key in the file)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "role-policy test push cert")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


db.init_db()
storage.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
HOST = "testhost.local:8000"
client = TestClient(webapp.app, base_url=f"http://{HOST}")

CERT, PLAIN = "rp-cert-1", "rp-plain-1"
(storage.STORAGE_DIR / f"{CERT}.pem").write_bytes(pem_certificate())
(storage.STORAGE_DIR / f"{PLAIN}.txt").write_text("an ordinary file about the role-policy card\n")
db.insert_upload(CERT, "role-policy-push.pem", f"{CERT}.pem", "tester", media_type="certkey",
                 description="role-policy restricted fixture")
db.insert_upload(PLAIN, "role-policy-notes.txt", f"{PLAIN}.txt", "tester", media_type="document",
                 description="role-policy ordinary fixture")
with actor.acting_as(actor.ACTOR_UI):
    cards.create("Role Policy Card")
    CARD = next(p for p in db.list_projects() if p["title"] == "Role Policy Card")
    membership.add_files(CARD["id"], [CERT, PLAIN], **membership.UI_EFFECTS)
    items.relate(PLAIN, CERT)
    ENTRY = blog.create("Role policy entry").data["entry"]
    blog.set_items(ENTRY["slug"], [(CERT, ""), (PLAIN, "")])

from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)


def is_not_found(r):
    try:
        body = r.json()
    except Exception:
        return False
    return (r.status_code == 404 and body.get("ok") is False and body.get("error", {}).get("code") == "not_found"
            and "detail" in body)


def mcp_not_found(out):
    return isinstance(out, dict) and out.get("ok") is False and out.get("error", {}).get("code") == "not_found"


def has(slug, payload):
    return slug in (payload if isinstance(payload, str) else json.dumps(payload))


def zip_slugs(project_id):
    r = client.get(f"/api/projects/{project_id}/export.zip")
    return r.status_code, zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json").decode()


def export_media():
    out = Path(TMP) / f"site-{len(os.listdir(TMP))}"
    with actor.acting_as(actor.ACTOR_UI):
        site_export.build_site({"project_slugs": [CARD["slug"]], "blog_entry_slugs": [ENTRY["slug"]]}, out_dir=out)
    return " ".join(p.as_posix() for p in out.rglob("*")) + " ".join(
        p.read_text(errors="ignore") for p in out.rglob("*.html"))


def mcp_lists():
    with actor.acting_as(actor.ACTOR_MCP):
        return {
            "get_project": has(CERT, mcp.constructicon_get_project(CARD["slug"]).get("items", [])),
            "get_related(plain)": has(CERT, mcp.constructicon_get_related(PLAIN)),
            "get_blog_entry": has(CERT, mcp.constructicon_get_blog_entry(ENTRY["slug"]).get("items", [])),
            "search": has(CERT, mcp.constructicon_search("role-policy")),
        }


def mcp_direct(slug):
    with actor.acting_as(actor.ACTOR_MCP):
        return {
            "get": mcp.constructicon_get(slug),
            "download": mcp.constructicon_download(slug),
            "get_related": mcp.constructicon_get_related(slug),
            "list_revisions": mcp.constructicon_list_revisions(slug),
        }


DIRECT_WEB = ["/object/{s}", "/api/image/{s}", "/api/image/{s}/revisions", "/f/{s}", "/f/{s}/thumb"]

# ---- 1. today ------------------------------------------------------------------------------
print("--- 1. today: labels recorded, restricted served directly, hidden from browsing ---")
from check_routes_roles import inventory  # noqa: E402
inv = inventory(webapp.app)
check("every route has exactly one role", all(p is None for *_, p in inv), [x for x in inv if x[3]])
check("every role is used", {r for r, *_ in inv} == set(roles.ORDER))

for path in DIRECT_WEB[:4]:
    r = client.get(path.format(s=CERT))
    check(f"today: GET {path} serves the restricted item (200)", r.status_code == 200, r.status_code)
check("today: /f serves the certificate bytes", client.get(f"/f/{CERT}").content.startswith(b"-----BEGIN CERTIFICATE"))
check("today: /project lists it", has(CERT, client.get(f"/project/{CARD['slug']}").text))
check("today: the neighbour's Related list shows it", has(CERT, client.get(f"/object/{PLAIN}").text))
check("today: the blog entry lists it", has(CERT, client.get(f"/api/blog-entries/{ENTRY['slug']}").json()))
check("today: search hides it", not has(CERT, client.get("/api/search?query=role-policy").json())
      and has(PLAIN, client.get("/api/search?query=role-policy").json()))
check("today: gallery hides it", not has(CERT, client.get("/api/gallery?query=role-policy").json()))
check("today: home hides it", not has(CERT, client.get("/").text) and has(PLAIN, client.get("/").text))
st, manifest = zip_slugs(CARD["id"])
check("today: project zip excludes it", st == 200 and not has(CERT, manifest) and has(PLAIN, manifest))
media = export_media()
check("today: static export excludes it", CERT not in media and "role-policy-push" not in media and PLAIN in media)
d = mcp_direct(CERT)
check("today: MCP get / download / get_related / list_revisions serve it",
      d["get"].get("slug") == CERT and bool(d["download"].get("content_base64"))
      and isinstance(d["get_related"], list) and d["list_revisions"].get("ok") is True, d)
lists = mcp_lists()
check("today: MCP get_project, get_related(neighbour), get_blog_entry list it; search hides it",
      lists == {"get_project": True, "get_related(plain)": True, "get_blog_entry": True, "search": False}, lists)

# the request log records the route's role
same = {"Origin": f"http://{HOST}"}
client.post("/api/settings", data={"key": "thingiverse_app_token", "value": "dummy-not-real"}, headers=same)
client.post(f"/api/image/{PLAIN}", data={"display_name": "Role policy notes"}, headers=same)
c = sqlite3.connect(db.DB_PATH)
logged = dict(c.execute("SELECT path, required_role FROM audit_log WHERE op IS NULL AND path IN (?, ?) ORDER BY id",
                        ("/api/settings", f"/api/image/{PLAIN}")).fetchall())
c.close()
check("request log: POST /api/settings recorded as admin", logged.get("/api/settings") == "admin", logged)
check("request log: POST /api/image/{slug} recorded as editor", logged.get(f"/api/image/{PLAIN}") == "editor", logged)

# ---- 2. the #467 switch ----------------------------------------------------------------------
print("--- 2. the switch: restricted needs admin, the actor is a viewer ---")
orig_role_of = roles.role_of
policy.RESTRICTED_VIEW_ROLE = roles.ADMIN
roles.role_of = lambda a: roles.VIEWER
try:
    for path in DIRECT_WEB:
        r = client.get(path.format(s=CERT))
        check(f"switch: GET {path} refuses it (404 not_found, shared shape)", is_not_found(r)
              and r.json()["detail"] == "not found", f"{r.status_code} {r.text[:120]}")
    r = client.get(f"/api/image/{CERT}/similar")
    check("switch: GET /api/image/{s}/similar refuses it", is_not_found(r), r.status_code)
    check("switch: /project drops it, keeps the ordinary file",
          not has(CERT, client.get(f"/project/{CARD['slug']}").text) and has(PLAIN, client.get(f"/project/{CARD['slug']}").text))
    check("switch: the neighbour's Related list drops it", not has(CERT, client.get(f"/object/{PLAIN}").text))
    check("switch: the blog entry drops it", not has(CERT, client.get(f"/api/blog-entries/{ENTRY['slug']}").json()))
    check("switch: search still hides it", not has(CERT, client.get("/api/search?query=role-policy").json()))
    st, manifest = zip_slugs(CARD["id"])
    check("switch: project zip still excludes it", not has(CERT, manifest) and has(PLAIN, manifest))
    check("switch: static export still excludes it", CERT not in export_media())
    for name, out in mcp_direct(CERT).items():
        check(f"switch: MCP {name} refuses it (not_found)", mcp_not_found(out), out)
    lists = mcp_lists()
    check("switch: MCP get_project / get_related / get_blog_entry / search all drop it",
          not any(lists.values()), lists)
    for path in DIRECT_WEB[:4]:
        check(f"switch: an ordinary item is untouched at {path}", client.get(path.format(s=PLAIN)).status_code == 200)
    check("switch: MCP get of an ordinary item is untouched", mcp_direct(PLAIN)["get"].get("slug") == PLAIN)
    roles.role_of = lambda a: roles.ADMIN
    check("switch, as admin: /object serves it again", client.get(f"/object/{CERT}").status_code == 200)
    check("switch, as admin: /f serves it again", client.get(f"/f/{CERT}").status_code == 200)
    check("switch, as admin: MCP get serves it again", mcp_direct(CERT)["get"].get("slug") == CERT)
finally:
    policy.RESTRICTED_VIEW_ROLE = None
    roles.role_of = orig_role_of

# ---- 3. deny everything: nothing bypasses can_view -------------------------------------------
print("--- 3. can_view denies everything: every door refuses even an ordinary item ---")
orig_can_view = policy.can_view
policy.can_view = lambda item, actor=None: False
try:
    for path in DIRECT_WEB[:4] + ["/f/{s}/thumb"]:
        r = client.get(path.format(s=PLAIN))
        check(f"deny-all: GET {path} refuses an ordinary item", is_not_found(r), f"{r.status_code} {r.text[:100]}")
    for name, out in mcp_direct(PLAIN).items():
        check(f"deny-all: MCP {name} refuses an ordinary item", mcp_not_found(out), out)
    check("deny-all: /project lists no files", not has(PLAIN, client.get(f"/project/{CARD['slug']}").text))
    check("deny-all: search returns nothing", client.get("/api/search?query=role-policy").json() == [])
    with actor.acting_as(actor.ACTOR_MCP):
        check("deny-all: MCP get_project lists no files", mcp.constructicon_get_project(CARD["slug"])["items"] == [])
finally:
    policy.can_view = orig_can_view

# ---- 4. the role hook ------------------------------------------------------------------------
print("--- 4. the role hook: ENFORCE on, the actor a viewer ---")
roles.ENFORCE = True
roles.role_of = lambda a: roles.VIEWER
try:
    def forbidden(r):
        return r.status_code == 403 and r.json().get("error", {}).get("code") == "forbidden" and r.json().get("ok") is False
    check("enforce: viewer GET /api/settings (admin) -> 403 forbidden", forbidden(client.get("/api/settings")))
    check("enforce: viewer GET /admin (admin) -> 403", forbidden(client.get("/admin")))
    check("enforce: viewer POST /api/image/{slug} (editor) -> 403",
          forbidden(client.post(f"/api/image/{PLAIN}", data={"display_name": "x"}, headers=same)))
    check("enforce: viewer GET / (viewer) -> 200", client.get("/").status_code == 200)
    check("enforce: viewer GET /f/{slug} (public) -> 200", client.get(f"/f/{PLAIN}").status_code == 200)
    roles.role_of = lambda a: roles.PUBLIC
    check("enforce: public GET / (viewer) -> 403", forbidden(client.get("/")))
    check("enforce: public GET /healthz -> 200", client.get("/healthz").status_code == 200)
    roles.role_of = lambda a: roles.ADMIN
    check("enforce: admin GET /api/settings -> 200", client.get("/api/settings").status_code == 200)
finally:
    roles.ENFORCE = False
    roles.role_of = orig_role_of

check("reset: nothing refused again", client.get(f"/object/{CERT}").status_code == 200
      and client.get("/api/settings").status_code == 200)

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
