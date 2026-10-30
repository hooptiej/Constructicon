#!/usr/bin/env python3
"""Whose Claude Is It Anyway (#560 remaining, #778). Throwaway DB, no server.

    python scripts/test_whose_claude_560_778.py

  1. the channel column and its migration (capture_channel_560): each mapping rule on fixture rows
     (migration label, change-log row, request-log row, a card's write-up, the label alone, unknown),
     fill-only and idempotent, one change-log row with the counts;
  2. the owner backfill (owner_upload_backfill_560): the owner's manual-upload label + web channel ->
     the owner's account, install-token uploads with that label included (owner, 2026-10-30); MCP rows,
     another user's upload and rows already owned are left alone; counts and slugs in one change-log row; idempotent; an ambiguous owner
     (no admin with the owner name, two admins with it, no owner name and two admins) is refused
     loudly, writes nothing, stays pending, and the boot carries on;
  3. MCP `uploaded_by` is a source note, never an identity: the install token's upload naming the
     owner's label stays admin-owned and is not in the owner's gallery; a personal key's upload is
     that key's user's whatever the note says; the note and the channel are in the MCP item shape;
  4. the per-user gallery: own items, another user's, an unknown name (a 404 page that says so, and
     the grid API's not_found), an old /gallery/user/<label> link (the label view, saying so), and
     /gallery/source/<label>;
  5. #778 credit: work made with a personal key reads "<user>'s <token name>" on the item and card
     ORIGIN panels, the MCP shapes and the change log's `via`; a rename shows the new name; a revoked
     key falls back to "<user>'s claude"; install-token work stays admin-owned with no credit; the same
     for hobbies (hobby page ORIGIN, MCP list_hobbies) and blog entries (web API + builder, MCP).
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import base64
import contextlib
import io
import json
import os
import re
import secrets
import sys
import time

import _testenv  # noqa: E402
TMP = _testenv.isolate("whoseclaude-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, install_config, install_token, mcp_tokens, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}
OWNER_NAME = "Wc Owner"
LABEL = f"{OWNER_NAME} (me) — manual upload"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def sql(q, args=()):
    conn = db.get_conn()
    try:
        cur = conn.execute(q, args)
        rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def set_config(**vals):
    for k, v in vals.items():
        if v is None:
            sql("DELETE FROM install_config WHERE key = ?", (k,))
        else:
            sql("INSERT OR REPLACE INTO install_config (key, value, updated_at) VALUES (?, ?, ?)", (k, v, time.time()))
    install_config._cache["values"] = None


def row(slug):
    return dict(sql("SELECT * FROM capture_events WHERE slug = ?", (slug,))[0])


def last_change(op):
    r = sql("SELECT * FROM audit_log WHERE op = ? ORDER BY id DESC LIMIT 1", (op,))
    return dict(r[0]) if r else None


def rerun(*names):
    """Forget the named migrations and run the pending ones; returns (ran, printed output)."""
    for n in names:
        sql("DELETE FROM schema_migrations WHERE name = ?", (n,))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ran = db.run_pending_migrations()
    return ran, out.getvalue()


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


db.init_db()
users.limiter.reset()
set_config(owner_name=OWNER_NAME, owner_label=f"{OWNER_NAME} (me)")
check("setup: the owner's manual-upload label is what the web writes", db.source_manual_upload() == LABEL,
      db.source_manual_upload())
NAMES = {"O": ("wc_owner", "admin", OWNER_NAME), "A2": ("wc_admin2", "admin", "Other Admin"),
         "E": ("wc_ed", "editor", "Ed Itor"), "V": ("wc_view", "viewer", "Vee Ewer")}
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for k, (n, role, disp) in NAMES.items():
        PW[k] = "wc-" + secrets.token_urlsafe(16)
        users.create_user(n, PW[k], role, display_name=disp)
UID = {k: db.get_user(username=n)["id"] for k, (n, _r, _d) in NAMES.items()}
C = {"token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for k, (n, _r, _d) in NAMES.items():
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": n, "password": PW[k]}, headers=SAME)
    check(f"{k} signs in", r.status_code == 200, r.text[:200])
    C[k] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in NAMES else dict(SAME)) for k, c in C.items()}

# ---- 1. the channel migration ---------------------------------------------------------------------
print("--- 1. channel migration ---")
NOW = time.time() - 86400
FIX = {}


def old_item(key, tech, filename=None, owner=None, at_offset=0):
    """An item as it looked before #560: no channel, no uploader unless `owner`."""
    slug = f"wc-{key}-{secrets.token_hex(3)}"
    with actor.acting_as(actor.ACTOR_SCRIPT):
        db.insert_upload(slug, filename or f"{key}.png", None, tech)
    ts = NOW + at_offset
    sql("UPDATE capture_events SET channel = NULL, uploaded_by_user_id = ?, timestamp = ? WHERE slug = ?",
        (owner, ts, slug))
    FIX[key] = slug
    return slug, ts


def request_row(path, actor_name, ts, filename=None, url=None):
    body = {"file": f"<file: {filename}>"} if filename else {"external_url": url}
    sql("INSERT INTO audit_log (method, path, form_body, affected_slugs, status_code, timestamp, actor) "
        "VALUES ('POST', ?, ?, '[]', 200, ?, ?)", (path, json.dumps(body), ts + 2, actor_name))


def change_row(op, actor_name, slug, ts):
    sql("INSERT INTO audit_log (method, path, form_body, affected_slugs, status_code, timestamp, op, actor, batch_id, "
        "mutations) VALUES ('CORE', ?, '{}', ?, 200, ?, ?, ?, 'fixture', '[]')",
        (f"core:{op}", json.dumps([slug]), ts + 1, op, actor_name))


old_item("migr", "Claude — migrated from hooptiej.com", at_offset=10)
s, ts = old_item("mcplog", "Claude — authored", at_offset=20)
change_row("upload_tags", "mcp", s, ts)
s, ts = old_item("webreq", LABEL, filename="webreq.png", at_offset=30)
request_row("/api/upload", None, ts, filename="webreq.png")
s, ts = old_item("tokreq", LABEL, filename="tokreq.png", at_offset=40)
request_row("/api/upload", "token", ts, filename="tokreq.png")
old_item("labelonly", LABEL, at_offset=50)
old_item("authored", "Claude — authored", at_offset=60)
old_item("weird", "a legacy tech value", at_offset=70)
s, ts = old_item("mcpowner", LABEL, at_offset=80)
change_row("automatch_tags", "mcp", s, ts)
s, ts = old_item("edreq", LABEL, filename="edreq.png", at_offset=90)
request_row("/api/upload", "user:wc_ed", ts, filename="edreq.png")
s, ts = old_item("edowned", LABEL, filename="edowned.png", owner=UID["E"], at_offset=100)
request_row("/api/upload", "user:wc_ed", ts, filename="edowned.png")
with actor.acting_as("user:wc_ed"):
    wcard = cards.create("Whose Claude Writeup Card").data["card"]
WRITEUP = db.get_project(wcard["id"])["writeup_slug"]
sql("UPDATE capture_events SET channel = NULL WHERE slug = ?", (WRITEUP,))

ran, out = rerun("capture_channel_560", "owner_upload_backfill_560")
check("both #560 migrations ran", ran == ["capture_channel_560", "owner_upload_backfill_560"], f"{ran} {out}")
want = {"migr": "migration:hooptiej.com", "mcplog": "mcp", "webreq": "web", "tokreq": "web", "labelonly": "web",
        "authored": "mcp", "weird": "unknown", "mcpowner": "mcp", "edreq": "web", "edowned": "web"}
got = {k: row(s)["channel"] for k, s in FIX.items()}
check("channel per rule (migration label, change log, request log, label, unknown)", got == want, got)
check("a card's write-up takes the channel of the create_card row (user:wc_ed -> web, not the label's mcp)",
      row(WRITEUP)["channel"] == "web", row(WRITEUP)["channel"])
ch = last_change("migration_capture_channel_560")
counts = json.loads(ch["form_body"])["counts"]
check("one change-log row with the count per rule", ch["actor"] == "migration" and counts["migration_label"] == 1
      and counts["change_log"] == 2 and counts["request_log"] == 4 and counts["writeup"] == 1
      and counts["unknown"] == 1 and counts["label"] >= 2, counts)

# ---- 2. the owner backfill ------------------------------------------------------------------------
print("--- 2. owner backfill ---")
owned = {k: row(s)["uploaded_by_user_id"] for k, s in FIX.items()}
check("the owner's own web uploads (no request row, or a pre-actor one) become the owner's",
      owned["webreq"] == UID["O"] and owned["labelonly"] == UID["O"], owned)
check("an install-token upload with the owner's label becomes the owner's too (owner, 2026-10-30)",
      owned["tokreq"] == UID["O"], owned["tokreq"])
check("an MCP row with the owner's label stays admin-owned", owned["mcpowner"] is None)
check("another user's upload (request by user:wc_ed) stays as it was", owned["edreq"] is None and owned["edowned"] == UID["E"])
check("other labels stay admin-owned", owned["migr"] is None and owned["authored"] is None and owned["weird"] is None)
bf = last_change("migration_owner_uploads_560")
d = json.loads(bf["form_body"])
c = d["counts"]
check("one change-log row: counts, the owner and the slugs it set",
      c["assigned"] == 3 and c["of_which_install_token"] == 1 and c["left_not_web"] == 1 and c["left_other_user"] == 1
      and "left_install_token" not in c and c["owner"]["id"] == UID["O"]
      and sorted(d["slugs"]) == sorted([FIX["webreq"], FIX["labelonly"], FIX["tokreq"]]), d)
ran, out = rerun("capture_channel_560", "owner_upload_backfill_560")
d2 = json.loads(last_change("migration_owner_uploads_560")["form_body"])
check("idempotent: a second run assigns nothing and fills no channel",
      d2["counts"]["assigned"] == 0 and json.loads(last_change("migration_capture_channel_560")["form_body"])["counts"]["rows"] == 0, d2)

# ambiguous owners
sql("UPDATE capture_events SET uploaded_by_user_id = NULL WHERE slug = ?", (FIX["labelonly"],))


def deferred_case(name):
    before = sql("SELECT COUNT(*) FROM audit_log WHERE op = 'migration_owner_uploads_560'")[0][0]
    ran, out = rerun("owner_upload_backfill_560")
    after = sql("SELECT COUNT(*) FROM audit_log WHERE op = 'migration_owner_uploads_560'")[0][0]
    pending = "owner_upload_backfill_560" in db.pending_migrations()
    check(f"ambiguous owner ({name}): refused loudly, nothing written, still pending",
          "owner_upload_backfill_560" not in ran and "DEFERRED owner_upload_backfill_560" in out and pending
          and before == after and row(FIX["labelonly"])["uploaded_by_user_id"] is None, out[-400:])
    return out


set_config(owner_name="Nobody Here")
out = deferred_case("no admin has the owner name")
check("... the message says what to fix", "no enabled admin has that display name" in out, out[-300:])
set_config(owner_name="Wc Owner")
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("wc_owner_twin", "wc-" + secrets.token_urlsafe(16), "admin", display_name="wc owner")
deferred_case("two admins match it")
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.delete_user(db.get_user(username="wc_owner_twin")["id"])
set_config(owner_name=None)
deferred_case("no owner name, two admins")
check("the boot still completes with a deferred migration (init_db returns)", (db.init_db() or True))
set_config(owner_name=OWNER_NAME)
ran, out = rerun()
check("once the owner can be told, the next start applies it", "owner_upload_backfill_560" in ran
      and row(FIX["labelonly"])["uploaded_by_user_id"] == UID["O"], out[-300:])

# ---- 3 + 5. MCP: the note is not an identity; the personal-key credit ----------------------------
print("--- 3. MCP uploaded_by is a note; 5. credit ---")
from mcp_server import auth as mcp_auth  # noqa: E402
from mcp_server import server as mcp  # noqa: E402
INSTALL = install_token.load()
r = C["E"].post("/api/account/mcp-tokens", json={"name": "windows claude"}, headers=H["E"])
E_TOK, E_TID = r.json()["token"], r.json()["id"]
app = mcp_auth.wrap(mcp.mcp.streamable_http_app(host="testserver", stateless_http=True), INSTALL)


def rpc(client, token, name, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    r = client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}",
                                                "Accept": "application/json, text/event-stream",
                                                "MCP-Protocol-Version": "2025-06-18"})
    text = r.text
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        datas = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = datas[-1] if datas else ""
    try:
        msg = json.loads(text)
    except ValueError:
        return r.status_code, None, text[:300]
    res = msg.get("result") or {}
    payload = (res.get("content") or [{}])[0].get("text", "")
    try:
        payload = json.loads(payload)
    except (ValueError, TypeError):
        pass
    return r.status_code, res.get("isError", False), payload


def b64(text):
    return base64.b64encode(text.encode()).decode()


with TestClient(app, base_url="http://testserver") as mc:
    st, err, out = rpc(mc, INSTALL, "constructicon_upload",
                       {"filename": "wc-install-note.txt", "content_base64": b64("install token upload " + secrets.token_hex(4)),
                        "uploaded_by": LABEL})
    check("install token upload naming the owner's label: works", st == 200 and not err, str(out)[:300])
    INST_SLUG = out.get("slug") if isinstance(out, dict) else None
    ir = row(INST_SLUG)
    check("... stays admin-owned (the label is a note, not who)", ir["uploaded_by_user_id"] is None and ir["tech"] == LABEL)
    check("... channel mcp, no key credit", ir["channel"] == "mcp" and ir["uploaded_via_token_id"] is None, ir["channel"])
    st, err, out = rpc(mc, INSTALL, "constructicon_get", {"slug": INST_SLUG})
    check("MCP get: source_note is the note, channel mcp, uploaded_by_user null, uploaded_via null",
          not err and out.get("source_note") == LABEL and out.get("channel") == "mcp"
          and out.get("uploaded_by_user") is None and out.get("uploaded_via") is None, str(out)[:300])

    st, err, out = rpc(mc, E_TOK, "constructicon_upload",
                       {"filename": "wc-ed-note.txt", "content_base64": b64("ed key upload " + secrets.token_hex(4)),
                        "uploaded_by": LABEL})
    ED_SLUG = out.get("slug") if isinstance(out, dict) else None
    er = row(ED_SLUG) if ED_SLUG else {}
    check("a personal key's upload naming the OWNER's label is the key's user's (Ed), not the owner's",
          er.get("uploaded_by_user_id") == UID["E"] and er.get("uploaded_via_token_id") == E_TID and er.get("channel") == "mcp", er)
    st, err, out = rpc(mc, E_TOK, "constructicon_get", {"slug": ED_SLUG})
    check("MCP get: uploaded_via 'Ed Itor's windows claude'", not err and (out.get("uploaded_via") or {}).get("text")
          == "Ed Itor's windows claude", str(out)[:300])
    st, err, out = rpc(mc, E_TOK, "constructicon_create_project", {"title": "Whose Claude Ed Card"})
    check("a card made with Ed's key", st == 200 and not err, str(out)[:300])
    ED_CARD = next(p for p in db.list_projects() if p["title"] == "Whose Claude Ed Card")
    check("... created_by Ed, created_via the key id", ED_CARD["created_by_user_id"] == UID["E"]
          and ED_CARD["created_via_token_id"] == E_TID)
    st, err, out = rpc(mc, E_TOK, "constructicon_get_project", {"id_or_slug": ED_CARD["slug"]})
    check("MCP get_project: created_via 'Ed Itor's windows claude'",
          not err and (out.get("created_via") or {}).get("text") == "Ed Itor's windows claude", str(out)[:300])
    st, err, out = rpc(mc, E_TOK, "constructicon_create_hobby", {"name": "Whose Claude Ed Hobby"})
    check("a hobby made with Ed's key", st == 200 and not err, str(out)[:300])
    ED_HOBBY = out.get("slug") if isinstance(out, dict) else None
    st, err, out = rpc(mc, INSTALL, "constructicon_create_hobby", {"name": "Whose Claude Install Hobby"})
    INST_HOBBY = out.get("slug") if isinstance(out, dict) else None
    st, err, out = rpc(mc, E_TOK, "constructicon_create_blog_entry", {"title": "Whose Claude Ed Entry"})
    check("a blog entry made with Ed's key: created_via in the answer",
          st == 200 and not err and (out.get("created_via") or {}).get("text") == "Ed Itor's windows claude", str(out)[:300])
    ED_ENTRY = out.get("slug") if isinstance(out, dict) else None
    st, err, out = rpc(mc, INSTALL, "constructicon_create_blog_entry", {"title": "Whose Claude Install Entry"})
    INST_ENTRY = out.get("slug") if isinstance(out, dict) else None
    hs = {r[0]: (r[1], r[2]) for r in sql("SELECT bt.slug, hs.created_by_user_id, hs.created_via_token_id FROM blog_tags bt "
                                           "LEFT JOIN hobby_settings hs ON hs.hobby_tag_id = bt.id WHERE bt.is_hobby = 1")}
    check("hobby rows: Ed's has Ed + the key, the install token's has neither",
          hs.get(ED_HOBBY) == (UID["E"], E_TID) and hs.get(INST_HOBBY, (None, None)) == (None, None), hs)
    with actor.acting_as(actor.ACTOR_MCP):  # a list tool answers one content block per element: call it directly
        out = mcp.constructicon_list_hobbies()
    hm = {h["slug"]: h for h in out} if isinstance(out, list) else {}
    check("MCP list_hobbies: created_via 'Ed Itor's windows claude'; install-token hobby: both null",
          (hm.get(ED_HOBBY, {}).get("created_via") or {}).get("text") == "Ed Itor's windows claude"
          and hm.get(INST_HOBBY, {}).get("created_by_user") is None and hm.get(INST_HOBBY, {}).get("created_via") is None
          and INST_HOBBY in hm, str(out)[:300])
    st, err, out = rpc(mc, INSTALL, "constructicon_get_blog_entry", {"slug": INST_ENTRY})
    check("MCP get_blog_entry (install token's): admin-owned, no credit",
          not err and out.get("created_by_user") is None and out.get("created_via") is None, str(out)[:300])
    with actor.acting_as(actor.ACTOR_MCP):
        out = mcp.constructicon_list_blog_entries()
    bm = {e["slug"]: e for e in out} if isinstance(out, list) else {}
    check("MCP list_blog_entries: Ed's entry credited", (bm.get(ED_ENTRY, {}).get("created_via") or {}).get("text")
          == "Ed Itor's windows claude", str(out)[:300])
    st, err, out = rpc(mc, INSTALL, "constructicon_create_project", {"title": "Whose Claude Install Card"})
    INST_CARD = next(p for p in db.list_projects() if p["title"] == "Whose Claude Install Card")
    check("install-token card: admin-owned, no key", INST_CARD["created_by_user_id"] is None
          and INST_CARD["created_via_token_id"] is None)


def via_of(title):
    ch = cards.list_changes(limit=200)["changes"]
    return [(c["actor"], c["via"]) for c in ch if c["op"] == "create_card"
            and any(title.lower().replace(" ", "-") in s for s in c["affected_slugs"])]


with actor.acting_as(actor.ACTOR_SCRIPT):
    check("change log: Ed's card row is user:wc_ed via 'Ed Itor's windows claude'",
          via_of("Whose Claude Ed Card") == [("user:wc_ed", "Ed Itor's windows claude")], via_of("Whose Claude Ed Card"))
    check("change log: the install-token card row is actor mcp with no via (as today)",
          via_of("Whose Claude Install Card") == [("mcp", None)], via_of("Whose Claude Install Card"))
check("the change-log row stores the token id", sql("SELECT via_token_id FROM audit_log WHERE op = 'create_card' AND "
      "actor = 'user:wc_ed' ORDER BY id DESC LIMIT 1")[0][0] == E_TID)


def page_has(client, url, text):
    r = client.get(url)
    return r.status_code == 200 and text in r.text.replace("&#39;", "'")


check("item page ORIGIN: Uploaded by 'Ed Itor's windows claude', Arrived via the MCP",
      page_has(C["O"], f"/object/{ED_SLUG}", "Ed Itor's windows claude") and page_has(C["O"], f"/object/{ED_SLUG}", "the MCP"))
check("card page ORIGIN: Created by 'Ed Itor's windows claude'", page_has(C["O"], f"/project/{ED_CARD['slug']}", "Ed Itor's windows claude"))
check("item page: install-token upload says Admin (no user recorded)", page_has(C["O"], f"/object/{INST_SLUG}", "Admin (no user recorded)"))
check("hobby page ORIGIN: Created by 'Ed Itor's windows claude'", page_has(C["O"], f"/hobby/{ED_HOBBY}", "Ed Itor's windows claude")
      and page_has(C["O"], f"/hobby/{ED_HOBBY}", "ORIGIN"))
check("hobby page ORIGIN (install token's hobby): Admin (no user recorded)",
      page_has(C["O"], f"/hobby/{INST_HOBBY}", "Admin (no user recorded)"))


def blog_via(slug):
    one = C["O"].get(f"/api/blog-entries/{slug}").json()
    listed = {e["slug"]: e for e in C["O"].get("/api/blog-entries").json()}
    return ((one.get("created_via") or {}).get("text"), (listed[slug].get("created_via") or {}).get("text"),
            (one.get("created_by_user") or {}).get("username"))


check("web blog entry API (one and list): 'Ed Itor's windows claude', created_by wc_ed",
      blog_via(ED_ENTRY) == ("Ed Itor's windows claude", "Ed Itor's windows claude", "wc_ed"), blog_via(ED_ENTRY))
check("web blog entry API: install token's entry admin-owned, no credit", blog_via(INST_ENTRY) == (None, None, None))
r = C["O"].get("/")
check("the blog builder carries the creator line (#blog-entry-creator, filled from created_via)",
      'id="blog-entry-creator"' in r.text and "created_via" in r.text, r.status_code)
r = C["E"].post(f"/api/account/mcp-tokens/{E_TID}/rename", json={"name": "mac claude"}, headers=H["E"])
check("rename the key (200, one change-log row)", r.status_code == 200 and r.json()["name"] == "mac claude"
      and last_change("mcp_token_rename") is not None, r.text[:200])
check("rename: the item and card now credit 'Ed Itor's mac claude'",
      page_has(C["O"], f"/object/{ED_SLUG}", "Ed Itor's mac claude") and page_has(C["O"], f"/project/{ED_CARD['slug']}", "Ed Itor's mac claude"))
check("rename: the hobby page and the blog entry follow",
      page_has(C["O"], f"/hobby/{ED_HOBBY}", "Ed Itor's mac claude") and blog_via(ED_ENTRY)[0] == "Ed Itor's mac claude")
with actor.acting_as(actor.ACTOR_SCRIPT):
    check("rename: the change log's via follows", via_of("Whose Claude Ed Card") == [("user:wc_ed", "Ed Itor's mac claude")])
r = C["E"].post(f"/api/account/mcp-tokens/{E_TID}/rename", json={"name": "Mac Claude "}, headers=H["E"])
check("renaming to a name that only differs in case/space is a real rename, not refused", r.status_code == 200, r.text[:200])
C["E"].post("/api/account/mcp-tokens", json={"name": "other"}, headers=H["E"])
r = C["E"].post(f"/api/account/mcp-tokens/{E_TID}/rename", json={"name": "OTHER"}, headers=H["E"])
check("renaming onto another of your names: 409 token_name_taken", r.status_code == 409
      and r.json()["error"]["code"] == "token_name_taken", r.text[:200])
r = C["V"].post(f"/api/account/mcp-tokens/{E_TID}/rename", json={"name": "stolen"}, headers=H["V"])
check("renaming someone else's key: 404", r.status_code == 404, r.status_code)
r = C["E"].post(f"/api/account/mcp-tokens/{E_TID}/revoke", headers=H["E"])
check("revoke the key", r.status_code == 200)
cr = mcp_tokens.credit(UID["E"], E_TID)
check("revoked: credit falls back to 'Ed Itor's claude' (revoked True)", cr["text"] == "Ed Itor's claude" and cr["revoked"], cr)
check("revoked: the item page says 'Ed Itor's claude' and that the key was revoked",
      page_has(C["O"], f"/object/{ED_SLUG}", "Ed Itor's claude") and page_has(C["O"], f"/object/{ED_SLUG}", "since been revoked"))
check("revoked: hobby page and blog entry fall back to 'Ed Itor's claude'",
      page_has(C["O"], f"/hobby/{ED_HOBBY}", "Ed Itor's claude") and page_has(C["O"], f"/hobby/{ED_HOBBY}", "since been revoked")
      and blog_via(ED_ENTRY)[0] == "Ed Itor's claude")
with actor.acting_as(actor.ACTOR_SCRIPT):
    check("revoked: the change log's via is 'Ed Itor's claude'", via_of("Whose Claude Ed Card") == [("user:wc_ed", "Ed Itor's claude")])
check("credit of no token is None (web / install token)", mcp_tokens.credit(UID["E"], None) is None)

# a web upload by a signed-in user records channel web and no key
with actor.acting_as("user:wc_ed"):
    WEB_SLUG = "wc-webed-" + secrets.token_hex(3)
    db.insert_upload(WEB_SLUG, "webed.png", None, LABEL)
wr = row(WEB_SLUG)
check("a signed-in web write: channel web, uploader Ed, no key", wr["channel"] == "web" and wr["uploaded_by_user_id"] == UID["E"]
      and wr["uploaded_via_token_id"] is None)
with actor.acting_as(actor.ACTOR_SCRIPT), actor.acting_via(actor.CHANNEL_MCP, 999):
    s2 = "wc-borrow-" + secrets.token_hex(3)
    db.insert_upload(s2, "b.png", None, "x")
check("a token id never rides on a write by an actor other than the key's user", row(s2)["uploaded_via_token_id"] is None)

# ---- 4. the per-user gallery -----------------------------------------------------------------------
print("--- 4. per-user gallery ---")


def grid_slugs(client, source):
    r = client.get("/api/items/grid", params={"source": source, "limit": "500"})
    return r.status_code, {i["slug"] for i in (r.json().get("items") or [])} if r.status_code == 200 else r.json()


st, mine = grid_slugs(C["V"], "user:wc_owner")
check("owner's gallery: the backfilled owner uploads (the install-token web one too)",
      st == 200 and {FIX["webreq"], FIX["labelonly"], FIX["tokreq"]} <= mine, mine)
check("owner's gallery: not the MCP install-token upload naming the owner's label, nor Ed's", INST_SLUG not in mine
      and ED_SLUG not in mine and WEB_SLUG not in mine)
st, eds = grid_slugs(C["V"], "user:WC_ED")
check("Ed's gallery (any case): Ed's items only", st == 200 and {ED_SLUG, WEB_SLUG, FIX["edowned"]} <= eds
      and FIX["webreq"] not in eds, eds)
r = C["V"].get("/gallery/user/wc_owner")
check("GET /gallery/user/wc_owner: 200, titled with the display name", r.status_code == 200 and OWNER_NAME in r.text)
r = C["V"].get("/gallery/user/nobody-at-all")
check("unknown user: a 404 page that says so (not an empty gallery)", r.status_code == 404
      and "no user or Source label" in r.text and 'id="gallery-not-found"' in r.text, r.status_code)
st, body = grid_slugs(C["V"], "user:nobody-at-all")
check("unknown user over the grid API: 404 not_found with the reason", st == 404 and body["error"]["code"] == "not_found"
      and "no user or Source label" in body["error"]["message"], body)
r = C["V"].get(f"/gallery/user/{OWNER_NAME} (me)")
check("old /gallery/user/<label> link: 200, the label view, saying it is one and linking the owner's gallery",
      r.status_code == 200 and 'id="gallery-label-note"' in r.text and "/gallery/user/wc_owner" in r.text, r.status_code)
st, lab = grid_slugs(C["V"], f"user:{OWNER_NAME} (me)")
check("... its grid lists every item carrying the label (the install-token ones too)",
      st == 200 and {FIX["tokreq"], FIX["webreq"], INST_SLUG} <= lab, st)
r = C["V"].get("/gallery/source/Claude")
check("/gallery/source/<label>: 200", r.status_code == 200 and 'id="gallery-label-note"' in r.text, r.status_code)
r = C["V"].get("/gallery/source/no-such-label-xyz")
check("/gallery/source/<unknown>: 404 that says so", r.status_code == 404 and "No item carries" in r.text)
r = C["O"].get(f"/object/{FIX['webreq']}")
check("item page: 'Uploaded by' links to the uploader's gallery", "/gallery/user/wc_owner" in r.text)

print()
print(("FAILED: " + ", ".join(FAILS)) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
