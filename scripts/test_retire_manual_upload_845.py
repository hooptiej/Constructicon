#!/usr/bin/env python3
"""#845: the "<name> — manual upload" Source label is retired. Throwaway DB, no server:

    python scripts/test_retire_manual_upload_845.py

  1. a web upload (/api/upload, /api/content) writes NO Source note (tech ''), whoever uploads; who is
     uploaded_by_user_id and how is channel 'web'; the label's recogniser stays for old rows;
  2. the item page leaves out a Source line that is the retired shape (also on a row that still carries
     it) and keeps a genuine label ("Claude — authored"); a duplicate upload says who, not an empty label;
  3. the retire_manual_upload_845 migration:
       * is registered last, after capture_channel_560 and owner_upload_backfill_560, and DEFERS (loudly,
         nothing changed, retried at the next start) while the owner backfill is itself deferred, so the
         backfill still reads the label; once it applies, the label is cleared;
       * clears exactly the retired label with a name this install used (the owner's label, the neutral
         "Owner", the owner's name, any user's display name or username) on `web` items, to '' and nothing
         else on the row;
       * leaves alone: a genuine label, a migrated-from label, an "— automated upload" label, a
         manual-upload label on an mcp / script item, a manual-upload-shaped label naming nobody this
         install knows (counted in the print and the log), and already-empty notes;
       * is idempotent (a second run changes nothing and writes no second log row);
       * is logged as one change-log row with a row image per item, and the generic undo puts every
         label back (and a redo clears them again);
       * the owner's old uploads still got their owner from the backfill that ran first.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import contextlib
import io
import json
import os
import secrets
import sys
import time

import _testenv  # noqa: E402
TMP = _testenv.isolate("manual-upload-845-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core import actor, db, install_config, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
OWNER_NAME = "Hoop Owner"
OWNER_LABEL = f"{OWNER_NAME} (me)"
OWNER_NOTE = f"{OWNER_LABEL} — manual upload"
NEUTRAL_NOTE = "Owner — manual upload"
MIG = "retire_manual_upload_845"
OP = "migration_retire_manual_upload_845"


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


def tech(slug):
    return row(slug)["tech"]


def rerun_migration():
    """Forget that the migration ran, run the pending ones, and return (names applied, what it printed)."""
    sql("DELETE FROM schema_migrations WHERE name = ?", (MIG,))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ran = db.run_pending_migrations()
    return ran, buf.getvalue()


def log_rows():
    return sql("SELECT id, actor, batch_id, mutations, affected_slugs, form_body FROM audit_log WHERE op = ?", (OP,))


db.init_db()
users.limiter.reset()
set_config(owner_name=OWNER_NAME, owner_label=OWNER_LABEL)

with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("hoop_owner", "pw-" + secrets.token_urlsafe(12), "admin", display_name=OWNER_NAME)
    users.create_user("hoop_ed", "pw-" + secrets.token_urlsafe(12), "editor", display_name="Ed Itor")
    users.create_user("hoop_plain", "pw-" + secrets.token_urlsafe(12), "editor", display_name="")
UID = {n: db.get_user(username=n)["id"] for n in ("hoop_owner", "hoop_ed", "hoop_plain")}
client = _testenv.client(webapp.app)
with client:
    pass

# ============================================================================================
# 1. nothing writes the label any more
# ============================================================================================
print("--- 1. a web upload writes no Source note ---")
r = client.post("/api/upload", files={"file": ("a.txt", b"first file", "text/plain")}, data={"modified_at": "1700000000000"})
check("POST /api/upload (install token): 200", r.status_code == 200, (r.status_code, r.text[:200]))
SLUG_A = r.json()["slug"]
check("... tech is empty, channel is web, no uploader (the token is admin-owned)",
      tech(SLUG_A) == "" and row(SLUG_A)["channel"] == "web" and row(SLUG_A)["uploaded_by_user_id"] is None, row(SLUG_A))
r = client.post("/api/content", data={"external_url": "https://example.com/845", "content_description": "a link"})
check("POST /api/content: 200 and no Source note either", r.status_code == 200 and tech(r.json()["slug"]) == "",
      (r.status_code, r.text[:200]))
check("the item JSON's free-text note (uploaded_by) is empty", r.json().get("uploaded_by") == "", r.json().get("uploaded_by"))
check("db.SOURCE_NONE is the empty note; the recogniser still knows the old shape",
      db.SOURCE_NONE == "" and db.is_retired_source_note(OWNER_NOTE) and db.is_retired_source_note(NEUTRAL_NOTE)
      and not db.is_retired_source_note("Claude — authored") and not db.is_retired_source_note("")
      and not db.is_retired_source_note(None) and db.source_manual_upload() == OWNER_NOTE)

# ============================================================================================
# 2. the item page
# ============================================================================================
print("--- 2. the item page ---")
with actor.acting_as(actor.ACTOR_UI):
    OLD_OWNER = "old-owner-" + secrets.token_hex(3)
    db.insert_upload(OLD_OWNER, "old.png", None, OWNER_NOTE)
with actor.acting_as(actor.ACTOR_MCP):
    GENUINE = "genuine-" + secrets.token_hex(3)
    db.insert_upload(GENUINE, "g.png", None, db.SOURCE_AUTHORED)
page = client.get(f"/object/{SLUG_A}").text
check("new item page: no 'Source:' line, but 'Uploaded by' and 'Arrived via the web app'",
      "Source:" not in page and "Uploaded by" in page and "the web app" in page)
page = client.get(f"/object/{OLD_OWNER}").text
check("an old row still carrying the retired note: the page leaves it out (the data is not touched yet)",
      "Source:" not in page and tech(OLD_OWNER) == OWNER_NOTE)
page = client.get(f"/object/{GENUINE}").text
check("a genuine label still shows ('Source: Claude — authored')", f"Source: {db.SOURCE_AUTHORED}" in page)
dup = client.post("/api/upload", files={"file": ("a.txt", b"first file", "text/plain")}, data={"modified_at": "1700000000000"})
check("a duplicate upload says it was already uploaded, with no empty 'by' (409)",
      dup.status_code == 409 and "Already uploaded on " in dup.text and "by  on" not in dup.text, dup.text[:200])

# ============================================================================================
# 3. the migration
# ============================================================================================
print("--- 3. the migration ---")
names = [n for n, _ in db.MIGRATIONS]
check("registered last, after both #560 steps",
      names[-1] == MIG and names.index("capture_channel_560") < names.index("owner_upload_backfill_560") < names.index(MIG), names[-4:])
check("it ran at boot on this (fresh) DB and wrote nothing, because there was nothing to clear",
      MIG in db.applied_migrations() and not log_rows())

# --- 3a. deferred while the owner backfill is deferred -------------------------------------------
# Two admins and no owner name: the backfill cannot tell who the owner is, so it defers; the retire step
# must wait for it (else the backfill would find no labels left to read).
set_config(owner_name=None, owner_label="Zed")
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("hoop_admin2", "pw-" + secrets.token_urlsafe(12), "admin", display_name="Second Admin")
with actor.acting_as(actor.ACTOR_UI):
    DEFER = "defer-" + secrets.token_hex(3)
    db.insert_upload(DEFER, "d.png", None, "Zed — manual upload")
sql("DELETE FROM schema_migrations WHERE name = 'owner_upload_backfill_560'")
ran, out = rerun_migration()
check("with the owner backfill deferred, the retire step defers too: neither is recorded",
      "owner_upload_backfill_560" not in ran and MIG not in ran and MIG not in db.applied_migrations(), ran)
check("... loudly, saying what to fix", f"DEFERRED {MIG}" in out and "owner_upload_backfill_560" in out
      and "nothing was changed" in out, out[-400:])
check("... and the label is still there for the backfill to read", tech(DEFER) == "Zed — manual upload")

# --- 3b. the owner is now known: the backfill goes first, then the retire step --------------------
set_config(owner_name=OWNER_NAME, owner_label=OWNER_LABEL)
with actor.acting_as(actor.ACTOR_UI):
    DEFER2 = "defer2-" + secrets.token_hex(3)
    db.insert_upload(DEFER2, "d2.png", None, OWNER_NOTE)
sql("UPDATE capture_events SET tech = ? WHERE slug = ?", (OWNER_NOTE, DEFER))  # the label this owner would have written
# the matrix of rows, each made the way the app really makes it
def mk(prefix, filename, note, who):
    slug = f"{prefix}-{secrets.token_hex(3)}"
    with actor.acting_as(who):
        db.insert_upload(slug, filename, None, note)
    return slug


M = {
    "owner_web": mk("m-owner", "o.png", OWNER_NOTE, actor.ACTOR_UI),
    "neutral_web": mk("m-neutral", "n.png", NEUTRAL_NOTE, actor.ACTOR_UI),
    "ownername_web": mk("m-oname", "on.png", f"{OWNER_NAME} — manual upload", actor.ACTOR_UI),
    "ed_web": mk("m-ed", "e.png", "Ed Itor — manual upload", "user:hoop_ed"),
    "plain_web": mk("m-plain", "p.png", "hoop_plain — manual upload", "user:hoop_plain"),
    "claude_web": mk("m-claude", "c.png", "Claude — authored", actor.ACTOR_UI),
    "migrated": mk("m-mig", "m.png", "Claude — migrated from hooptiej.com", actor.ACTOR_SCRIPT),
    "auto_web": mk("m-auto", "au.png", f"{OWNER_LABEL} — automated upload", actor.ACTOR_UI),
    "owner_mcp": mk("m-mcp", "mc.png", OWNER_NOTE, actor.ACTOR_MCP),
    "owner_script": mk("m-script", "sc.png", OWNER_NOTE, actor.ACTOR_SCRIPT),
    "stranger_web": mk("m-str", "st.png", "Some Stranger — manual upload", actor.ACTOR_UI),
    "empty_web": mk("m-empty", "em.png", "", actor.ACTOR_UI),
}
check("fixtures: the matrix rows have the channel the app gives them",
      row(M["owner_web"])["channel"] == "web" and row(M["owner_mcp"])["channel"] == "mcp"
      and row(M["owner_script"])["channel"] == "script" and row(M["migrated"])["channel"].startswith("migration:"),
      {k: row(v)["channel"] for k, v in M.items()})
snapshot = {s: dict(r) for s, r in ((r["slug"], r) for r in map(dict, sql("SELECT * FROM capture_events")))}

ran, out = rerun_migration()
check("the owner backfill now applies (the owner is known) and THEN the retire step, in that order",
      ran.index("owner_upload_backfill_560") < ran.index(MIG) if "owner_upload_backfill_560" in ran and MIG in ran else False, ran)
owner_id = UID["hoop_owner"]
check("the owner's old uploads still got their owner (the backfill read the label before it was cleared)",
      row(DEFER)["uploaded_by_user_id"] == owner_id and row(DEFER2)["uploaded_by_user_id"] == owner_id,
      (row(DEFER)["uploaded_by_user_id"], row(DEFER2)["uploaded_by_user_id"]))
cleared_keys = ["owner_web", "neutral_web", "ownername_web", "ed_web", "plain_web"]
check("cleared to '': the owner's label, the neutral 'Owner', the owner's name, a user's display name and username (web items)",
      all(tech(M[k]) == "" for k in cleared_keys) and tech(DEFER) == "" and tech(DEFER2) == "",
      {k: tech(M[k]) for k in cleared_keys})
check("left alone: a genuine label, a migrated-from label, an automated-upload label",
      tech(M["claude_web"]) == "Claude — authored" and tech(M["migrated"]) == "Claude — migrated from hooptiej.com"
      and tech(M["auto_web"]) == f"{OWNER_LABEL} — automated upload")
check("left alone: the manual-upload label on an mcp item and on a script item",
      tech(M["owner_mcp"]) == OWNER_NOTE and tech(M["owner_script"]) == OWNER_NOTE)
check("left alone: a manual-upload-shaped label naming nobody this install knows, and an empty note",
      tech(M["stranger_web"]) == "Some Stranger — manual upload" and tech(M["empty_web"]) == "")
check("the section-2 row made through the ui actor (channel web) was cleared too", tech(OLD_OWNER) == "")
after = {r["slug"]: dict(r) for r in map(dict, sql("SELECT * FROM capture_events"))}
changed = {s: {c for c in snapshot[s] if snapshot[s][c] != after[s][c]} for s in snapshot if snapshot[s] != after[s]}
check("only `tech` changed on the cleared rows (plus the owner backfill's uploaded_by_user_id on the owner's)",
      all(c <= {"tech", "uploaded_by_user_id"} for c in changed.values()), {s: c for s, c in changed.items() if not c <= {"tech", "uploaded_by_user_id"}})
check("... and no other row changed at all except those", set(changed) <= {M[k] for k in cleared_keys} | {DEFER, DEFER2, OLD_OWNER, M["owner_mcp"], M["owner_script"]},
      sorted(set(changed) - {M[k] for k in cleared_keys}))
check("the boot log line counts what was cleared and what was left, by label",
      "cleared the '— manual upload' note on" in out and "Some Stranger — manual upload" in out, out[-500:])

# --- 3c. logged, one row, with images ----------------------------------------------------------
logs = log_rows()
check("exactly one change-log row, under the `system` actor (so the generic undo accepts it)",
      len(logs) == 1 and logs[0]["actor"] == "system", [(l["id"], l["actor"]) for l in logs])
muts = json.loads(logs[0]["mutations"])
n_cleared = sum(1 for k in cleared_keys) + 3  # DEFER, DEFER2, OLD_OWNER
check("one row image per cleared item: table capture_events, before the label, after ''",
      len(muts) == n_cleared and all(m["table"] == "capture_events" and m["after"] == {"tech": ""}
                                     and m["before"]["tech"].endswith("— manual upload") for m in muts), (len(muts), n_cleared))
details = json.loads(logs[0]["form_body"])
check("the details say how many, which labels, and which were left alone",
      details["cleared"] == n_cleared and details["by_label"].get(OWNER_NOTE, 0) >= 3
      and details["left_alone"] == {"Some Stranger — manual upload": 1}, details)

# --- 3d. idempotent -----------------------------------------------------------------------------
ran, out = rerun_migration()
check("a second run clears nothing and writes no second log row", MIG in ran and len(log_rows()) == 1
      and "cleared the '— manual upload' note on 0 web item(s)" in out, out[-300:])

# --- 3e. undo / redo ----------------------------------------------------------------------------
batch = logs[0]["batch_id"]
u = client.post(f"/api/changes/{batch}/undo")
check("the generic undo accepts it (200)", u.status_code == 200, (u.status_code, u.text[:300]))
check("... and every label is back, exactly as it was",
      all(tech(M[k]) == snapshot[M[k]]["tech"] for k in cleared_keys) and tech(DEFER) == OWNER_NOTE
      and tech(OLD_OWNER) == OWNER_NOTE, {k: tech(M[k]) for k in cleared_keys})
undo_batch = u.json().get("batch_id")
redo = client.post(f"/api/changes/{undo_batch}/undo") if undo_batch else None
check("undoing the undo (a redo) clears them again", redo is not None and redo.status_code == 200
      and all(tech(M[k]) == "" for k in cleared_keys), redo.text[:200] if redo is not None else "no undo batch")

print()
print(("FAILED: " + ", ".join(FAILS)) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
