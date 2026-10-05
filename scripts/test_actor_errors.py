#!/usr/bin/env python3
"""Self-contained check for the actor context (#560) and the one error shape (#548).

Throwaway SQLite DB (CONSTRUCTICON_DB_PATH is set before core is imported), real `db.init_db()`,
the real FastAPI app through TestClient and the MCP tool functions (direct calls and the SDK's
own call_tool path). No server, no network:

    python scripts/test_actor_errors.py

Exits 1 if any check fails.
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import types

TMP = tempfile.mkdtemp(prefix="actorerrors-")
os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(TMP, "test.db")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from core import actor, captions, cards, changes, db, errors, ingest  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def last_change_actor(op=None):
    c = db.get_conn()
    try:
        sql = "SELECT op, actor FROM audit_log WHERE op IS NOT NULL"
        args = ()
        if op:
            sql += " AND op = ?"
            args = (op,)
        r = c.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return (r["op"], r["actor"]) if r else None
    finally:
        c.close()


def last_request_row(path):
    c = db.get_conn()
    try:
        r = c.execute("SELECT method, path, status_code, actor FROM audit_log WHERE op IS NULL AND path = ? "
                      "ORDER BY id DESC LIMIT 1", (path,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


db.init_db()

# ---- 1. the context itself -----------------------------------------------------------------
check("this script defaults to the script actor (run from scripts/)", actor.current_actor() == actor.ACTOR_SCRIPT)
check("_process_default: a scripts/ argv -> script",
      (lambda saved: (setattr(sys, "argv", [os.path.join(ROOT, "scripts", "x.py")]), actor._process_default(),
                      setattr(sys, "argv", saved))[1])(list(sys.argv)) == actor.ACTOR_SCRIPT)
check("_process_default: uvicorn argv -> none",
      (lambda saved: (setattr(sys, "argv", ["/usr/local/bin/uvicorn"]), actor._process_default(),
                      setattr(sys, "argv", saved))[1])(list(sys.argv)) is None)
check("changes.ACTOR_* are core.actor's constants (one source)",
      changes.ACTOR_UI is actor.ACTOR_UI and changes.ACTOR_MCP is actor.ACTOR_MCP)

# No context and no process default: falls back to system, one warning per call site.
saved_default = actor._PROCESS_DEFAULT
actor._PROCESS_DEFAULT = None
records = []


class _Grab(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


grab = _Grab()
logging.getLogger("constructicon.actor").addHandler(grab)
for _ in range(3):
    got = actor.current_actor()  # one call site, three calls
check("no context -> system", got == actor.ACTOR_SYSTEM)
check("warned once for that call site", len(records) == 1, records)
actor.current_actor()  # a second call site
check("a second call site warns once more", len(records) == 2, records)
check("the warning names this file", "test_actor_errors.py" in records[0], records[0])

with actor.acting_as(actor.ACTOR_MCP):
    check("acting_as sets it", actor.current_actor() == actor.ACTOR_MCP)
    with actor.acting_as(actor.ACTOR_UI):
        check("acting_as nests", actor.current_actor() == actor.ACTOR_UI)
    check("...and restores", actor.current_actor() == actor.ACTOR_MCP)
check("outside again: no context", not actor.has_context())

# ---- 2. thread hand-offs -------------------------------------------------------------------
seen = {}


def record(key):
    seen[key] = actor.current_actor()


with actor.acting_as(actor.ACTOR_MCP):
    actor.spawn(record, "spawn").join(5)
    t = threading.Thread(target=record, args=("plain",))
    t.start()
    t.join(5)
    ev = threading.Event()
    ingest.run_in_thread(lambda: (record("ingest"), ev.set()))
    ev.wait(5)
check("actor.spawn carries the actor into the thread", seen.get("spawn") == actor.ACTOR_MCP, seen)
check("a plain threading.Thread does NOT (falls back to system)", seen.get("plain") == actor.ACTOR_SYSTEM, seen)
check("ingest.run_in_thread carries it", seen.get("ingest") == actor.ACTOR_MCP, seen)

real_loop = captions.queue_worker_loop
captions.queue_worker_loop = lambda: record("caption_worker")
try:
    with actor.acting_as(actor.ACTOR_UI):  # even started from a request, the worker is system
        t = threading.Thread(target=actor.carry(captions._queue_worker_as_system))
        t.start()
        t.join(5)
finally:
    captions.queue_worker_loop = real_loop
check("caption queue worker runs as system", seen.get("caption_worker") == actor.ACTOR_SYSTEM, seen)
actor._PROCESS_DEFAULT = saved_default

# ---- 3. core writes record the context's actor ----------------------------------------------
card = db._create_project("Actor card")
with actor.acting_as(actor.ACTOR_UI):
    cards.set_status(card["id"], "paused")
check("core write under owner-ui context -> owner-ui", last_change_actor("set_status") == ("set_status", "owner-ui"),
      last_change_actor())
with actor.acting_as(actor.ACTOR_MCP):
    cards.set_status(card["id"], "done")
check("core write under mcp context -> mcp", last_change_actor("set_status") == ("set_status", "mcp"))
with actor.acting_as(actor.ACTOR_MCP):
    cards.set_status(card["id"], "in_progress", actor="override")
check("an explicit actor= still overrides", last_change_actor("set_status") == ("set_status", "override"))
cards.set_status(card["id"], "paused")
check("no context in a script -> script", last_change_actor("set_status") == ("set_status", "script"))

# ---- 4. web: actor + error shapes ------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402

client = TestClient(webapp.app)
r = client.post(f"/api/projects/{card['slug']}", data={"stage": "done"})
check("web card edit 200", r.status_code == 200, r.text[:200])
check("web card edit change-log row is owner-ui", last_change_actor("set_status") == ("set_status", "owner-ui"),
      last_change_actor())
req = last_request_row(f"/api/projects/{card['slug']}")
check("web request-log row records owner-ui", req and req["actor"] == "owner-ui", req)

# BackgroundTasks/run_in_threadpool see the request's actor: a sync route body runs in the threadpool.
r = client.post("/api/provenance-options/card", data={"key": "actor_test", "label": "Actor test"})
check("provenance option add 200", r.status_code == 200, r.text[:200])
check("provenance option change-log row is owner-ui",
      last_change_actor("provenance_option_add") == ("provenance_option_add", "owner-ui"), last_change_actor())


def shape(r, status, code, detail=None):
    try:
        b = r.json()
    except Exception:
        return False
    ok = (r.status_code == status and b.get("ok") is False and isinstance(b.get("error"), dict)
          and b["error"].get("code") == code and "detail" in b)
    if detail is not None:
        ok = ok and b["detail"] == detail
    if isinstance(b.get("detail"), str):
        ok = ok and b["error"]["message"] == b["detail"]
    return ok


r = client.get("/api/cards/no-such-card")
check("GET /api/cards/<missing>: 404 not_found WITH detail", shape(r, 404, "not_found", "No card 'no-such-card'."), r.text)
r = client.post(f"/api/projects/{card['slug']}", data={"stage": "bogus"})
check("bad card edit: 422 bad_status", shape(r, 422, "bad_status"), r.text)
r = client.post(f"/api/projects/{card['slug']}", data={"parent_id": "abc"})
check("HTTPException 400: bad_request, detail unchanged", shape(r, 400, "bad_request", "Invalid parent_id"), r.text)
r = client.post("/api/pending-decisions/999999/resolve", data={"choice": "x"})
check("unknown decision: 404 not_found, detail unchanged", shape(r, 404, "not_found", "No such pending decision"), r.text)
did = db.add_pending_decision("retype", "nope-slug", {"options": [{"key": "image", "label": "Image"}]})
r = client.post(f"/api/pending-decisions/{did}/resolve", data={"choice": "bogus"})
check("bad decision choice: 400 invalid_choice", shape(r, 400, "invalid_choice"), r.text)
check("...detail is the exception text", r.json()["detail"].startswith("'bogus' is not one of"), r.text)
r = client.post("/api/curator/needs/dismiss", data={"nudge_key": "not-a-key"})
check("bad queue key: 400 bad_request", shape(r, 400, "bad_request", "Not a queue item key: 'not-a-key'"), r.text)
r = client.get("/api/definitely-not-a-route")
check("unknown route: 404 not_found, detail 'Not Found'", shape(r, 404, "not_found", "Not Found"), r.text)
r = client.put("/api/projects")
check("wrong method: 405 method_not_allowed", shape(r, 405, "method_not_allowed", "Method Not Allowed"), r.text)
db.insert_content("item-a", "tester", "youtube", external_url="https://example.com/a", content_description="A")
db.insert_content("item-b", "tester", "youtube", external_url="https://example.com/b", content_description="B")
r = client.post("/api/image/item-a/superseded-by", data={})
b = r.json()
check("missing form field: 422 validation_error, detail list kept",
      r.status_code == 422 and b.get("ok") is False and b["error"]["code"] == "validation_error"
      and isinstance(b["detail"], list) and b["detail"][0]["loc"] == ["body", "new_slug"], r.text)
r = client.post("/api/image/item-a", data={"type_metadata": json.dumps({"date_made": "june 2009"})})
# #541 phase B: validated in core/items.py now, so the web gets the same code as the MCP.
check("bad physical-piece date: 400 bad_physical_piece, detail unchanged",
      shape(r, 400, "bad_physical_piece", "date_made must look like 2009, 2009-06 or 2009-06-14"), r.text)
client.post("/api/image/item-a/superseded-by", data={"new_slug": "item-b"})
r = client.post("/api/image/item-b/superseded-by", data={"new_slug": "item-a"})
check("revision cycle: 422 revision_cycle", shape(r, 422, "revision_cycle"), r.text)
r = client.post("/api/image/item-a/thumbnail/refresh")
check("thumbnail refresh of a no-thumbnail failure is an error status, not 200",
      r.status_code != 200 or r.json().get("ok") is True, r.text)
r = client.post("/api/image/item-a", data={"description": "x"}, headers={"Origin": "http://evil.example"})
check("request guard 403 already in the shape", shape(r, 403, "cross_origin"), r.text)
r = client.post("/api/export/build", content="{}", headers={"Content-Type": "text/plain"})
check("JSON gate 415: unsupported_media_type", shape(r, 415, "unsupported_media_type"), r.text)

# ---- 5. MCP: the wrapper -------------------------------------------------------------------
from mcp_server import server  # noqa: E402


def mcp_err(out, code):
    return isinstance(out, dict) and out.get("ok") is False and out.get("error", {}).get("code") == code \
        and isinstance(out["error"].get("message"), str)


check("every registered tool is wrapped", server._check_every_tool_wrapped() is None
      and len(server._WRAPPED_TOOLS) == len(server.mcp._tool_manager.list_tools()))
check("get missing -> not_found (not None)", mcp_err(server.constructicon_get("nope"), "not_found"))
check("get_project missing -> not_found", mcp_err(server.constructicon_get_project("nope"), "not_found"))
check("delete missing -> not_found (not False)", mcp_err(server.constructicon_delete("nope"), "not_found"))
check("set_status bad -> bad_status", mcp_err(server.constructicon_set_status(card["id"], "bogus"), "bad_status"))
check("resolve unknown decision -> not_found",
      mcp_err(server.constructicon_resolve_pending_decision(999999, choice="x"), "not_found"))
check("dismiss bad key -> bad_request", mcp_err(server.constructicon_dismiss_need("not-a-key"), "bad_request"))
check("get_posts_for_tag unknown -> not_found (was raise ValueError)",
      mcp_err(server.constructicon_get_posts_for_tag("no-such-tag"), "not_found"))
check("run_type_action unknown -> not_found (was {'error': str})",
      mcp_err(server.constructicon_run_type_action("nope", "x"), "not_found"))
check("set_content_date bad -> bad_date (#541: items.parse_date)",
      mcp_err(server.constructicon_set_content_date("item-a", "not a date"), "bad_date"))
check("update bad date_made -> bad_physical_piece",
      mcp_err(server.constructicon_update("item-a", type_metadata={"date_made": "june 2009"}), "bad_physical_piece"))
ok = server.constructicon_get("item-a")
check("a successful return is unchanged", isinstance(ok, dict) and ok.get("slug") == "item-a" and "ok" not in ok, ok)
server.constructicon_set_status(card["id"], "idea")
check("an MCP write is recorded as mcp", last_change_actor("set_status") == ("set_status", "mcp"), last_change_actor())
check("an MCP call leaves no context behind", actor.current_actor() == actor.ACTOR_SCRIPT)

# Through the SDK: an isError result carrying the same payload; success unchanged.
res = asyncio.run(server.mcp.call_tool("constructicon_get", {"slug": "nope"}))
check("SDK path: refusal is an isError result", res.is_error is True)
check("SDK path: structuredContent is the shared shape", mcp_err(res.structured_content, "not_found"), res)
check("SDK path: text content is the same JSON", mcp_err(json.loads(res.content[0].text), "not_found"))
res = asyncio.run(server.mcp.call_tool("constructicon_get_posts_for_tag", {"tag_name": "no-such-tag"}))
check("SDK path: a list-returning tool's refusal passes output validation", res.is_error is True
      and mcp_err(res.structured_content, "not_found"), res)
res = asyncio.run(server.mcp.call_tool("constructicon_get", {"slug": "item-a"}))
check("SDK path: success is not an error (structuredContent wraps a `dict | None` return in `result`, as before)",
      not res.is_error and (res.structured_content.get("result") or {}).get("slug") == "item-a", res)
res = asyncio.run(server.mcp.call_tool("constructicon_set_status", {"card": card["id"], "stage": "paused"}))
check("SDK path: write recorded as mcp (sync tool runs on a worker thread)",
      not res.is_error and last_change_actor("set_status") == ("set_status", "mcp"), last_change_actor())

# ---- 6. one error class family ---------------------------------------------------------------
from core import card_rules, curation_queue, decisions, physical_piece  # noqa: E402

check("CardError is an AppError", issubclass(card_rules.CardError, errors.AppError))
check("QueueError is an AppError (400)", curation_queue.QueueError("m").http_status == 400)
check("decisions.* are AppErrors", all(issubclass(c, errors.AppError) for c in (
    decisions.DecisionNotFound, decisions.DecisionAlreadyResolved, decisions.UnknownDecisionKind, decisions.InvalidChoice)))
try:
    physical_piece.clean_fields({"date_made": "nope"})
    pp = None
except ValueError as e:  # still a ValueError for older callers
    pp = e
check("physical_piece raises InvalidInput (still a ValueError)", isinstance(pp, errors.InvalidInput))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
