#!/usr/bin/env python3
"""Bouncers at Every Door (batch 17: #730, #749, #766, #779). Throwaway DB, no server.

    python scripts/test_bouncers_730_749_766_779.py

Real sessions for an admin A, an editor ED (the uploader of a SENSITIVE file, so ED sees it), an
outsider OUT (an editor who can't see that file, or a private card A owns) and a viewer V, plus the
per-run install token.
  #730  every single-item WEB write route refuses a hidden item with 403 `forbidden` and #709's
        wording and details shape ("Nothing was changed: this item is marked sensitive, ..."), writes
        nothing; a private-card item says "private card"; a two-item route says "1 of the 2"; a
        missing slug stays 404; READS of a hidden item stay 404 (page and API); the owner's PR #727
        exception holds (POST /api/image/{slug}/sensitive keeps its 404); ED (the uploader) still writes.
  #749  revision chains (supersede, remove, a hidden neighbour), move / copy / split / merge between
        cards refuse a hidden file (403 saying why; merge counts, never names); list_changes says how
        many rows it withheld (web-less: core + MCP) and an admin sees all; undo of a hidden row refuses.
  #766  Admin > Users lists each user's MCP key count, names and last use (never a secret); Revoke all
        keys is admin only (web 403 for others, service 403 naming the role), revokes every key, writes
        one change-log row (no secret), and mcp_tokens.authenticate refuses those keys afterwards while
        another user's key still works; a user with no keys says nothing changed; undo refuses it.
  #779  role-gated controls render greyed for a viewer (and admin-only ones for an editor), each with
        the shared role-lock attributes and a message naming the role; working for those who hold it;
        role-lock.js on every page; a role refusal (web and MCP) names the role needed.
Prints PASS/FAIL per check, then `N failure(s)` or `all checks passed`.
"""

import html as html_mod
import io
import json
import os
import re
import secrets
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("bouncers17-")
os.environ.setdefault("CAPTION_DISABLED", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, hobbies, items, mcp_tokens, membership, policy, revisions, sharing, users  # noqa: E402
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
        return None, None, None
    e = body.get("error", {}) if isinstance(body, dict) else {}
    return e.get("code"), e.get("message"), e.get("details")


def refusal_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e
    return None


db.init_db()
users.limiter.reset()

print("--- fixtures ---")
PW, UID = {}, {}
NAMES = {"A": "bz_admin", "ED": "bz_editor", "OUT": "bz_outsider", "V": "bz_viewer"}
ROLE = {"A": "admin", "ED": "editor", "OUT": "editor", "V": "viewer"}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for k, name in NAMES.items():
        PW[k] = "bz-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[k], ROLE[k])
        UID[k] = db.get_user(username=name)["id"] if "username" in db.get_user.__code__.co_varnames else None
ACT = {k: f"user:{n}" for k, n in NAMES.items()}
C = {"token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for k, name in NAMES.items():
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": PW[k]}, headers=SAME)
    check(f"{k} signs in", r.status_code == 200, r.text[:200])
    C[k] = c
    UID[k] = r.json().get("user", {}).get("id") if UID.get(k) is None else UID[k]
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in NAMES else dict(SAME)) for k, c in C.items()}


def upload(who, filename, seed, sensitive=False):
    fields = {"description": f"bouncers {filename}"}
    if sensitive:
        fields["sensitive"] = "true"
    r = C[who].post("/api/upload", files={"file": (filename, png_bytes(seed), "image/png")}, data=fields, headers=H[who])
    check(f"{who} uploads {filename}", r.status_code == 200, r.text[:200])
    return r.json()["slug"]


SECRET = upload("ED", "bz-secret.png", 1, sensitive=True)
SECRET2 = upload("ED", "bz-secret2.png", 2, sensitive=True)
VIS = upload("ED", "bz-visible.png", 3)
VIS2 = upload("ED", "bz-visible2.png", 4)
VIS3 = upload("ED", "bz-visible3.png", 5)
PRIV = upload("A", "bz-private.png", 6)
check("fixture: SECRET hidden from OUT, visible to ED",
      not policy.can_view(db.get_by_slug(SECRET), ACT["OUT"]) and policy.can_view(db.get_by_slug(SECRET), ACT["ED"]))

with actor.acting_as(actor.ACTOR_SCRIPT):
    C1 = cards.create("Bouncers Open One").data["card"]
    C2 = cards.create("Bouncers Open Two").data["card"]
    CP = cards.create("Bouncers Private").data["card"]
    membership.add_files(C1["id"], [VIS, SECRET], **membership.NO_EFFECTS)
    membership.add_files(CP["id"], [PRIV], **membership.NO_EFFECTS)
    sharing.set_private("card", CP["id"], True)
    HOB = hobbies.create("Bouncers Hobby").data["hobby"]
check("fixture: PRIV (on a private card) hidden from OUT", not policy.can_view(db.get_by_slug(PRIV), ACT["OUT"]))

conn = sqlite3.connect(db.DB_PATH)
conn.row_factory = sqlite3.Row


def log_count():
    """Change-log rows (method CORE), not the request log."""
    return conn.execute("SELECT COUNT(*) FROM audit_log WHERE method = 'CORE'").fetchone()[0]


SENSITIVE_MSG = "Nothing was changed: this item is marked sensitive, and only its uploader or an admin can change it."

# =================================================================================================
print("--- #730: single-item web writes say why ---")
TAG_ID = None
with actor.acting_as(actor.ACTOR_SCRIPT):
    from core import tags as tags_svc
    tags_svc.attach(SECRET, ["bz-tag"])
    TAG_ID = next(t["id"] for t in tags_svc.chips(SECRET) if t["name"] == "bz-tag")

ROUTES = [
    ("update", f"/api/image/{SECRET}", {"description": "x"}),
    ("delete", f"/api/image/{SECRET}/delete", {}),
    ("redact", f"/api/image/{SECRET}/redact", {}),
    ("unredact", f"/api/image/{SECRET}/unredact", {}),
    ("recover-redacted", f"/api/image/{SECRET}/recover-redacted", {}),
    ("ocr", f"/api/image/{SECRET}/ocr", {}),
    ("parse-limits/clear", f"/api/image/{SECRET}/parse-limits/clear", {}),
    ("caption retry", f"/api/image/{SECRET}/caption", {}),
    ("caption mark-used", f"/api/image/{SECRET}/caption/mark-used", {}),
    ("caption accept", f"/api/captions/{SECRET}/accept", {"text": "x"}),
    ("caption skip", f"/api/captions/{SECRET}/skip", {}),
    ("date", f"/api/image/{SECRET}/date", {"display_date": "1500000000"}),
    ("date-guess", f"/api/image/{SECRET}/date-guess", {"source": "filename"}),
    ("thumbnail refresh", f"/api/image/{SECRET}/thumbnail/refresh", {}),
    ("type action", f"/api/image/{SECRET}/action/whatever", {}),
    ("related add", f"/api/image/{SECRET}/related", {"related_slug": VIS3}),
    ("related remove", f"/api/image/{SECRET}/related/remove", {"related_slug": VIS3}),
    ("superseded-by", f"/api/image/{SECRET}/superseded-by", {"new_slug": VIS3}),
    ("supersedes", f"/api/image/{SECRET}/supersedes", {"old_slug": VIS3}),
    ("revisions remove", f"/api/image/{SECRET}/revisions/remove", {}),
    ("add to card", f"/api/image/{SECRET}/project", {"project_id": str(C2["id"])}),
    ("remove from card", f"/api/image/{SECRET}/project/remove", {"project_id": str(C1["id"])}),
    ("tag chip remove", f"/api/image/{SECRET}/tags/{TAG_ID}/remove", {}),
]
before_rows = log_count()
for label, path, data in ROUTES:
    r = C["OUT"].post(path, data=data, headers=H["OUT"])
    code, msg, det = err(r)
    two = label in ("related add", "superseded-by", "supersedes")
    want_msg = ("Nothing was changed: 1 of the 2 selected items is marked sensitive, and only its uploader or an admin "
                "can change it.") if two else SENSITIVE_MSG
    check(f"#730 OUT {label}: 403 forbidden, #709's words and details",
          r.status_code == 403 and code == "forbidden" and msg == want_msg and isinstance(det, dict)
          and det.get("refused") == [SECRET] and det.get("reason") == "sensitive"
          and det.get("reasons") == {SECRET: "sensitive"}, (r.status_code, code, msg, det))
check("#730 nothing written by any refused write", log_count() == before_rows, (before_rows, log_count()))
check("#730 the item and its tag are still there", db.get_by_slug(SECRET) is not None
      and any(t["name"] == "bz-tag" for t in tags_svc.chips(SECRET)))
r = C["OUT"].post(f"/api/image/{PRIV}/delete", headers=H["OUT"])
code, msg, det = err(r)
check("#730 OUT delete an item on a private card: 403 naming the private card",
      r.status_code == 403 and code == "forbidden" and "private card" in (msg or "") and det.get("reason") == "private", (r.status_code, msg))
r = C["OUT"].post("/api/image/no-such-slug-730/delete", headers=H["OUT"])
check("#730 a missing slug stays 404", r.status_code == 404 and err(r)[0] == "not_found", r.status_code)
for path in (f"/api/image/{SECRET}", f"/object/{SECRET}", f"/api/image/{SECRET}/revisions", f"/api/image/{SECRET}/tags"):
    r = C["OUT"].get(path)
    check(f"#730 read {path.replace(SECRET, '<hidden>')} stays 404 (existence not revealed)", r.status_code == 404, r.status_code)
r = C["OUT"].post(f"/api/image/{SECRET}/sensitive", data={"sensitive": "true"}, headers=H["OUT"])
check("#730 PR #727 exception: POST .../sensitive keeps its 404", r.status_code == 404, (r.status_code, r.text[:120]))
r = C["ED"].post(f"/api/image/{SECRET}", data={"description": "the uploader edits"}, headers=H["ED"])
check("#730 ED (the uploader) still writes", r.status_code == 200 and db.get_by_slug(SECRET)["description"] == "the uploader edits",
      (r.status_code, r.text[:120]))
r = C["A"].post(f"/api/image/{SECRET}/parse-limits/clear", headers=H["A"])
check("#730 the admin still writes", r.status_code == 200, r.status_code)

# =================================================================================================
print("--- #749: revision chains, reshaping, the change log ---")
with actor.acting_as(ACT["OUT"]):
    e = refusal_of(revisions.mark_superseded, VIS2, SECRET)
check("#749 OUT mark_superseded(visible, hidden): 403 forbidden saying why",
      e is not None and e.code == "forbidden" and e.status == 403 and "marked sensitive" in e.message
      and e.details.get("refused") == [SECRET], e and (e.code, e.message))
check("#749 ... no revision row written", conn.execute("SELECT COUNT(*) FROM item_revisions WHERE old_slug = ?", (VIS2,)).fetchone()[0] == 0)
with actor.acting_as(ACT["A"]):
    revisions.mark_superseded(VIS3, SECRET2)  # VIS3 -> SECRET2: OUT can see VIS3, not its successor
with actor.acting_as(ACT["OUT"]):
    e = refusal_of(revisions.remove_from_chain, VIS3)
check("#749 OUT remove_from_chain with a hidden neighbour: 403 hidden_neighbour, the neighbour not named",
      e is not None and e.code == "forbidden" and e.details.get("reason") == "hidden_neighbour"
      and SECRET2 not in e.message and SECRET2 not in json.dumps(e.details), e and (e.code, e.message, e.details))
check("#749 ... the chain is unchanged", conn.execute("SELECT new_slug FROM item_revisions WHERE old_slug = ?", (VIS3,)).fetchone()[0] == SECRET2)
with actor.acting_as(ACT["OUT"]):
    e = refusal_of(revisions.remove_from_chain, SECRET2)
check("#749 OUT remove_from_chain(hidden item): 403 forbidden (sensitive)", e is not None and e.code == "forbidden"
      and e.details.get("reason") == "sensitive", e and e.message)
with actor.acting_as(ACT["ED"]):
    res = revisions.remove_from_chain(VIS3, dry_run=True)
check("#749 ED (sees both) may remove it (dry run)", res.get("removed") is True, res)

for label, fn in (("move_files", cards.move_files), ("copy_files", cards.copy_files)):
    with actor.acting_as(ACT["OUT"]):
        e = refusal_of(fn, [SECRET], C1["id"], C2["id"])
    check(f"#749 OUT {label}(hidden file): 403 forbidden saying why", e is not None and e.code == "forbidden"
          and e.details.get("refused") == [SECRET], e and e.message)
    with actor.acting_as(ACT["OUT"]):
        ok = fn([VIS], C1["id"], C2["id"], dry_run=True)
    check(f"#749 OUT {label}(visible file) still works (dry run)", ok.ok)
with actor.acting_as(ACT["OUT"]):
    e = refusal_of(cards.split_card, C1["id"], [{"title": "Split part", "file_slugs": [SECRET]}])
check("#749 OUT split_card naming a hidden file: 403 forbidden", e is not None and e.code == "forbidden"
      and e.details.get("refused") == [SECRET], e and e.message)
with actor.acting_as(ACT["OUT"]):
    e = refusal_of(cards.merge_cards, C2["id"], [C1["id"]])
check("#749 OUT merge_cards absorbing a card with a hidden file: 403 hidden_files, counted not named",
      e is not None and e.code == "forbidden" and e.details.get("reason") == "hidden_files"
      and e.details.get("hidden") == {C1["slug"]: 1} and SECRET not in e.message and "1 file you can't see" in e.message,
      e and (e.message, e.details))
check("#749 ... both cards still there", db.get_project(C1["id"]) is not None and db.get_project(C2["id"]) is not None)
with actor.acting_as(ACT["ED"]):
    ok = cards.merge_cards(C2["id"], [C1["id"]], dry_run=True)
check("#749 ED (sees every file) may merge (dry run)", ok.ok)

with actor.acting_as(ACT["OUT"]):
    out_log = cards.list_changes(limit=500)
with actor.acting_as(ACT["A"]):
    adm_log = cards.list_changes(limit=500)
touches_secret = lambda rows: any(SECRET in (r.get("affected_slugs") or []) or SECRET in json.dumps(r["mutations"]) for r in rows)
check("#749 list_changes as OUT: no row touching the hidden item", not touches_secret(out_log["changes"]))
check("#749 list_changes as OUT: says how many it withheld, and why",
      out_log["withheld"] > 0 and out_log["withheld"] == len(adm_log["changes"]) - len(out_log["changes"])
      and "left out" in (out_log["withheld_note"] or "") and "admin" in out_log["withheld_note"], (out_log["withheld"], out_log["withheld_note"]))
check("#749 list_changes as A: nothing withheld, the hidden rows included",
      adm_log["withheld"] == 0 and adm_log["withheld_note"] is None and touches_secret(adm_log["changes"]))
from mcp_server import server as mcp  # noqa: E402
from mcp_server import auth as mcp_auth  # noqa: E402


def as_user(key, fn, *a, **kw):
    tok = mcp_auth.IDENTITY.set({"actor": ACT[key], "user_id": UID[key], "username": NAMES[key]})
    try:
        return fn(*a, **kw)
    finally:
        mcp_auth.IDENTITY.reset(tok)


m = as_user("OUT", mcp.constructicon_list_changes, limit=500)
check("#749 MCP list_changes as OUT: {changes, withheld, withheld_note}",
      isinstance(m, dict) and m.get("withheld", 0) > 0 and not touches_secret(m["changes"]), str(m)[:200])
m = mcp.constructicon_list_changes(limit=5)
check("#749 MCP list_changes with the install token: withheld 0", isinstance(m, dict) and m.get("withheld") == 0, str(m)[:200])
secret_batch = next(r["batch_id"] for r in adm_log["changes"] if SECRET in (r.get("affected_slugs") or []) and r["mutations"])
with actor.acting_as(ACT["OUT"]):
    e = refusal_of(cards.undo, secret_batch)
check("#749 undo of a row touching a hidden item, as OUT: 403 forbidden saying why",
      e is not None and e.code == "forbidden" and "can't see" in e.message, e and e.message)
m = as_user("OUT", mcp.constructicon_revision, VIS2, "supersede", new=SECRET)
check("#749 MCP revision(supersede) as OUT naming a hidden item: forbidden, saying why",
      isinstance(m, dict) and (m.get("error") or {}).get("code") == "forbidden"
      and "marked sensitive" in m["error"]["message"], str(m)[:200])
m = as_user("OUT", mcp.constructicon_card_files, C2["slug"], [SECRET], action="move", from_card=C1["slug"])
check("#749 MCP card_files(move) as OUT with a hidden file: forbidden, saying why",
      isinstance(m, dict) and (m.get("error") or {}).get("code") == "forbidden", str(m)[:200])

# =================================================================================================
print("--- #766: Admin > Users sees and revokes MCP keys ---")
secrets_made = []
for who, name in (("ED", "windows claude"), ("ED", "mac claude"), ("V", "viewer laptop")):
    r = C[who].post("/api/account/mcp-tokens", json={"name": name}, headers=H[who])
    check(f"{who} makes a key {name!r}", r.status_code == 200 and r.json().get("token", "").startswith("ctu_"), r.text[:200])
    secrets_made.append((who, r.json()["token"]))
ED_KEYS = [t for w, t in secrets_made if w == "ED"]
V_KEY = next(t for w, t in secrets_made if w == "V")
mcp_tokens.authenticate(ED_KEYS[0])  # a last use to show
r = C["A"].get("/api/users")
listed = {u["username"]: u for u in r.json()["users"]}
ed = listed[NAMES["ED"]]
check("#766 GET /api/users: ED has 2 keys, by name, with last use",
      ed["mcp_token_count"] == 2 and sorted(k["name"] for k in ed["mcp_tokens"]) == ["mac claude", "windows claude"]
      and any(k.get("last_used_at") for k in ed["mcp_tokens"]), ed)
check("#766 ... never a secret or hash", not any(t in r.text for _, t in secrets_made) and "token_hash" not in r.text)
check("#766 ... A has none listed", listed[NAMES["A"]]["mcp_token_count"] == 0)
r = C["A"].get("/admin")
check("#766 the Admin page draws the MCP keys column and Revoke all keys",
      r.status_code == 200 and "<th>MCP keys</th>" in r.text and "Revoke all keys" in r.text
      and "/mcp-tokens/revoke-all" in r.text, r.status_code)
for who in ("ED", "V"):
    r = C[who].post(f"/api/users/{UID['ED']}/mcp-tokens/revoke-all", json={}, headers=H[who])
    check(f"#766 {who} revoke-all: 403 (admin only), naming the role",
          r.status_code == 403 and "admin role" in (err(r)[1] or ""), (r.status_code, r.text[:150]))
with actor.acting_as(ACT["ED"]):
    e = refusal_of(mcp_tokens.revoke_all, UID["ED"])
check("#766 service revoke_all as ED: 403 forbidden, details need admin",
      e is not None and e.code == "forbidden" and e.details.get("need") == "admin", e and e.message)
before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
r = C["A"].post(f"/api/users/{UID['ED']}/mcp-tokens/revoke-all", json={}, headers=H["A"])
body = r.json()
check("#766 A revoke-all: 200, 2 revoked, by name, a batch and a message",
      r.status_code == 200 and body.get("revoked") == 2 and sorted(t["name"] for t in body["tokens"]) == ["mac claude", "windows claude"]
      and body.get("batch_id") and "Revoked 2 MCP keys" in body.get("message", ""), r.text[:300])
row = conn.execute("SELECT * FROM audit_log WHERE batch_id = ? ORDER BY id DESC", (body.get("batch_id"),)).fetchone()
row_text = json.dumps(dict(row)) if row else ""
row_details = json.loads(row["form_body"]) if row and row["form_body"] else {}
n_rows = conn.execute("SELECT COUNT(*) FROM audit_log WHERE batch_id = ? AND method = 'CORE'",
                      (body.get("batch_id"),)).fetchone()[0]
check("#766 one change-log row mcp_token_revoke_all, naming the user and count, no secret",
      row is not None and n_rows == 1 and "mcp_token_revoke_all" in row_text and row_details.get("username") == NAMES["ED"]
      and row_details.get("count") == 2 and not any(t in row_text for _, t in secrets_made), row_text[:300])
whole = json.dumps([dict(x) for x in conn.execute("SELECT * FROM audit_log WHERE id > ?", (before,))])
check("#766 no key secret anywhere in the new audit rows", not any(t in whole for _, t in secrets_made))
for t in ED_KEYS:
    try:
        mcp_tokens.authenticate(t)
        refused = False
    except mcp_tokens.TokenRefused as exc:
        refused = "not valid" in str(exc)
    check("#766 authenticate refuses a revoked key, saying why", refused)
check("#766 another user's key still works", mcp_tokens.authenticate(V_KEY)["username"] == NAMES["V"])
r = C["A"].post(f"/api/users/{UID['ED']}/mcp-tokens/revoke-all", json={}, headers=H["A"])
check("#766 again: 0 revoked, says nothing changed, no batch",
      r.status_code == 200 and r.json()["revoked"] == 0 and r.json()["batch_id"] is None
      and "nothing was changed" in r.json()["message"], r.text[:200])
r = C["A"].post(f"/api/changes/{body.get('batch_id')}/undo", headers=H["A"])
check("#766 the revoke can't be undone (refused, says so)", r.status_code >= 400 and err(r)[0] == "undo_refused", r.text[:200])
r = C["A"].post("/api/users/999999/mcp-tokens/revoke-all", json={}, headers=H["A"])
check("#766 an unknown user: 404", r.status_code == 404, r.status_code)

# =================================================================================================
print("--- #779: greyed, labelled, explained ---")


def tag_with(html, needle):
    """The opening tag that contains `needle`."""
    i = html.find(needle)
    if i < 0:
        return ""
    return html[html.rfind("<", 0, i):html.find(">", i) + 1]


def locked(tag, need):
    t = html_mod.unescape(tag)
    return ("role-locked" in t and 'aria-disabled="true"' in t and f'data-locked-role="{need}"' in t
            and f"needs the {need} role" in t)


obj = {k: C[k].get(f"/object/{VIS}").text for k in ("V", "ED", "A")}
for needle, what in (('id="delete-btn"', "Delete entirely"), ('id="redact-btn"', "Remove file"),
                     ('class="dp-edit-btn', "a details-panel Edit"), (">This is sensitive<", "This is sensitive")):
    tag = tag_with(obj["V"], needle) if needle.startswith(("id=", "class=")) else \
        obj["V"][obj["V"].rfind("<button", 0, obj["V"].find(needle)):obj["V"].find(needle)]
    check(f"#779 viewer, item page: {what} shown greyed, needs editor", locked(tag, "editor"), tag[:300])
check("#779 viewer, item page: Replace file shown greyed (was hidden)",
      "Replace file" in obj["V"] and locked(obj["V"][obj["V"].rfind("<button", 0, obj["V"].find("Replace file&hellip;")):obj["V"].find("Replace file&hellip;")], "editor"))
check("#779 viewer: the message says their role and who to ask",
      "Your account is a viewer. Ask an admin" in html_mod.unescape(obj["V"]))
check("#779 editor, item page: those controls work (no lock on delete / redact / Edit)",
      "role-locked" not in tag_with(obj["ED"], 'id="delete-btn"') and "role-locked" not in tag_with(obj["ED"], 'id="redact-btn"')
      and 'class="dp-edit-btn role-locked' not in obj["ED"] and 'id="replace-file-btn"' in obj["ED"])
check("#779 admin, item page: nothing locked", "data-locked-message" not in obj["A"])

proj = {k: C[k].get(f"/project/{C2['slug']}").text for k in ("V", "ED", "A")}
check("#779 viewer, card page: Delete project greyed (editor)", locked(tag_with(proj["V"], 'id="delete-project-btn"'), "editor"))
check("#779 viewer, card page: Convert greyed (admin)", locked(tag_with(proj["V"], 'id="convert-to-hobby-btn"'), "admin"))
check("#779 viewer, card page: the rename pencil greyed", locked(tag_with(proj["V"], 'id="project-title-edit-btn"'), "editor"))
check("#779 editor, card page: Convert greyed (admin only), Delete works",
      locked(tag_with(proj["ED"], 'id="convert-to-hobby-btn"'), "admin")
      and "role-locked" not in tag_with(proj["ED"], 'id="delete-project-btn"'))
check("#779 admin, card page: nothing locked", "data-locked-message" not in proj["A"])
hob = {k: C[k].get(f"/hobby/{HOB['slug']}").text for k in ("ED", "A")}
check("#779 editor, hobby page: Convert to card greyed (admin)", locked(tag_with(hob["ED"], 'id="hobby-convert-btn"'), "admin"))
check("#779 admin, hobby page: Convert to card works", "role-locked" not in tag_with(hob["A"], 'id="hobby-convert-btn"'))
home = {k: C[k].get("/").text for k in ("V", "ED")}
check("#779 viewer, home: + New project greyed", locked(tag_with(home["V"], 'id="new-project-btn"'), "editor"))
check("#779 viewer, home: the file grid's Select shown greyed (was hidden)",
      locked(tag_with(home["V"], '-select-locked"'), "editor"))
check("#779 editor, home: + New project and Select work",
      "role-locked" not in tag_with(home["ED"], 'id="new-project-btn"') and "-select-locked" not in home["ED"])
acct = C["V"].get("/account/password").text
check("#779 viewer, Account: the backup download uses the shared lock (admin)",
      locked(tag_with(acct, 'id="account-backup-locked"'), "admin"))
check("#779 role-lock.js loads on every page", all("/static/js/role-lock.js" in h for h in (obj["V"], proj["V"], home["V"], acct)))
js = open(os.path.join(ROOT, "web", "static", "js", "role-lock.js"), encoding="utf-8").read()
check("#779 role-lock.js: a capture-phase click shows data-locked-message and stops the action",
      "data-locked-message" in js and "stopImmediatePropagation" in js and "}, true);" in js and "textContent" in js)
css = open(os.path.join(ROOT, "web", "static", "style.css"), encoding="utf-8").read()
rule = re.search(r"\.role-locked \{([^}]*)\}", css)
check("#779 .role-locked is greyscale, never translucent", rule and "grayscale(1)" in rule.group(1) and "opacity" not in rule.group(1))

r = C["V"].post(f"/api/image/{VIS}/delete", headers=H["V"])
code, msg, det = err(r)
check("#779 web role refusal names the role needed and the caller's",
      r.status_code == 403 and "needs the editor role" in (msg or "") and "your account is a viewer" in (msg or "")
      and det == {"need": "editor", "have": "viewer"}, (r.status_code, msg, det))
m = as_user("V", mcp.constructicon_update, VIS, fields={"description": "x"})
check("#779 MCP role refusal names the role needed (details too)",
      isinstance(m, dict) and (m.get("error") or {}).get("code") == "forbidden"
      and "needs the editor role" in m["error"]["message"] and (m["error"].get("details") or {}).get("need") == "editor", str(m)[:300])

# =================================================================================================
print("--- check_policy_doors.py rules 6 and 7 catch a regression ---")
import ast  # noqa: E402
import check_policy_doors as cpd  # noqa: E402


def mutated(rel_target, old, new):
    cache = {}

    def tree(rel):
        if rel not in cache:
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            if rel == rel_target:
                assert old in src, (rel, old)
                src = src.replace(old, new, 1)
            cache[rel] = ast.parse(src)
        return cache[rel]
    return tree


f = []
cpd._check_single_writes(mutated("web/routes/items.py", "", ""), f)
check("rule 6: the real routes pass", f == [], f)
f = []
cpd._check_single_writes(mutated("web/routes/items.py",
                                 "    policy.writable_item(slug)  # #730: 404 if missing, 403 saying why if hidden\n    result = items.delete([slug])",
                                 "    policy.viewable_item(slug)\n    result = items.delete([slug])"), f)
check("rule 6: a write route back on viewable_item fails (both halves)",
      any("api_delete_image" in x and "viewable_item" in x for x in f)
      and any("api_delete_image" in x and "doesn't call policy.writable_item" in x for x in f), f)
f = []
cpd._check_change_log(mutated("core/cards/history.py", '"withheld": withheld, ', '"held": withheld, '), f)
check("rule 7: list_changes that stops reporting 'withheld' fails", any("withheld" in x for x in f), f)
f = []
cpd._check_change_log(mutated("core/cards/history.py", "_sharing.check_undo_allowed(rows, actor)", "pass  # (removed)"), f)
check("rule 7: undo without check_undo_allowed fails", any("check_undo_allowed" in x for x in f), f)

print()
if FAILS:
    print(f"{len(FAILS)} failure(s):")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("all checks passed")
