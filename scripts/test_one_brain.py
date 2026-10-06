#!/usr/bin/env python3
"""Self-contained check for #549 (web owns background work; MCP enqueues).

Throwaway DB, no server, no GPU:

    python scripts/test_one_brain.py

Covers: schema_migrations records every step once and a second boot re-runs none; the MCP
schema-only path (init_db(migrate=False)) applies no data migration and reports them pending;
a pre-recorded step is skipped; the audit-row secret scrub runs once; in the MCP role
captions.run_caption enqueues (and never calls the model) while the web role runs; the queue
worker drains one at a time and leaves the queue alone while the breaker is open.
Exits 1 if any check fails.
"""

import json
import os
import sys
import tempfile
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("onebrain-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import captions, db  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


names = [n for n, _ in db.MIGRATIONS]

# --- MCP path first, on a fresh DB: schema only ---
db.init_db(migrate=False)
check("mcp boot applies no data migration", db.applied_migrations() == {}, str(db.applied_migrations()))
check("mcp boot reports all pending", db.pending_migrations() == names)

# --- web boot: every step recorded once ---
db.init_db()
done = db.applied_migrations()
check("web boot records every step", sorted(done) == sorted(names), str(sorted(done)))
check("second web boot applies nothing", db.run_pending_migrations() == [])
before = dict(done)
db.init_db()
check("re-boot keeps applied_at stable", db.applied_migrations() == before)
check("mcp boot after web: nothing pending", db.pending_migrations() == [])

# --- a recorded step is skipped even if its effect is absent ---
conn = db.get_conn()
conn.execute("DELETE FROM schema_migrations WHERE name = 'curator_snooze_519'")
conn.execute("INSERT INTO curator_dismissals (nudge_key, action, snooze_until, created_at) VALUES ('x', 'snooze', ?, ?)",
             (time.time() + 999, time.time()))
conn.commit()
conn.close()
check("a missing step is re-applied", db.run_pending_migrations() == ["curator_snooze_519"])
conn = db.get_conn()
check("snooze became defer", conn.execute("SELECT action FROM curator_dismissals WHERE nudge_key='x'").fetchone()["action"] == "defer")
conn.execute("INSERT INTO curator_dismissals (nudge_key, action, snooze_until, created_at) VALUES ('y', 'snooze', ?, ?)",
             (time.time() + 999, time.time()))
conn.commit()
conn.close()
db.init_db()
conn = db.get_conn()
check("recorded step is not re-run on boot", conn.execute("SELECT action FROM curator_dismissals WHERE nudge_key='y'").fetchone()["action"] == "snooze")
conn.close()

# --- captions: the MCP role enqueues, never runs ---
calls = []
captions.caption_once = lambda *a, **k: calls.append((a, k)) or {"caption": "a cat", "elapsed_seconds": 0.0, "restarted": False,
                                                              "restart_seconds": 0.0, "restart_reason": None, "error": None}
try:
    slug = "slug-one-brain-1"
    db.insert_upload(slug, "pic.png", "stored1.png", "tester", file_size=10, media_type="image")
    slug2 = "slug-one-brain-2"
    db.insert_upload(slug2, "pic2.png", "stored2.png", "tester", file_size=10, media_type="image")
except Exception as e:
    print("setup insert failed:", repr(e))
    sys.exit(1)

# source image lookup is bypassed: pretend a file exists
captions._caption_source_path = lambda row, spec: "/dev/null"

os.environ["CONSTRUCTICON_ROLE"] = "mcp"
captions.run_caption(slug)
check("mcp role: no model call", calls == [])
q = db.peek_caption_queue()
check("mcp role: queued", q is not None and q["slug"] == slug, str(q))
check("mcp role: marked pending", db.get_by_slug(slug)["type_metadata"].get(captions.STATUS_KEY) == "pending")
captions.run_caption(slug2)
os.environ["CONSTRUCTICON_ROLE"] = ""

captions._breaker_reason = "test breaker"
check("breaker open: worker leaves queue alone", captions._drain_one() is False and db.peek_caption_queue()["slug"] == slug)
captions._breaker_reason = None
check("worker drains oldest first", captions._drain_one() is True and len(calls) == 1)
check("drained row done", db.get_by_slug(slug)["type_metadata"].get(captions.STATUS_KEY) == "done")
check("drained row dequeued", db.peek_caption_queue()["slug"] == slug2)
check("second drain", captions._drain_one() is True and len(calls) == 2)
check("queue empty -> no work", captions._drain_one() is False and db.peek_caption_queue() is None)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
