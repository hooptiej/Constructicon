#!/usr/bin/env python3
"""Self-contained check for #551 item 5, swallowed errors. Throwaway SQLite DB, no server:

    python scripts/test_swallowed_errors.py

Covers: the rate-limited best-effort logger, a representative set of best-effort sites (the
failure is forced; a warning is logged and the operation still completes), the sites that now
fail properly (clean 400s), the audit middleware's `_unparsed` marker, and
scripts/check_no_silent_except.py (clean tree passes; a silent except fails; `# silent-ok:` and
logging both pass). Exits 1 if any check fails.
"""
import json
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="swallowed-")
os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(TMP, "test.db")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here
import _testenv  # noqa: E402
_testenv.use_token()  # #467 step 2: the web part below sends the per-run install token

from core import besteffort, card_rules, captions, curator, db, physical_piece  # noqa: E402
from core import version as version_info  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def warnings(self):
        return [r for r in self.records if r.levelno == logging.WARNING]

    def text(self):
        return "\n".join(r.getMessage() for r in self.warnings())


cap = Capture()
logging.getLogger("constructicon").addHandler(cap)
logging.getLogger("constructicon").setLevel(logging.DEBUG)
db.init_db()


def fresh():
    cap.records.clear()
    besteffort.reset()


# ---- 1. the helper ----------------------------------------------------------------------------
fresh()
log = logging.getLogger("constructicon.selftest")
for i in range(5):
    besteffort.warn(log, "a hot path", ValueError("boom"), slug="abc")
check("rate limit: 5 identical failures log once", len(cap.warnings()) == 1, len(cap.warnings()))
check("the line names the site, the exception and the context",
      "a hot path" in cap.text() and "boom" in cap.text() and "slug='abc'" in cap.text(), cap.text())
check("the first failure carries the traceback", cap.warnings()[0].exc_info is not None)
besteffort.warn(log, "another site", ValueError("x"))
check("a different site is not suppressed", len(cap.warnings()) == 2)

# ---- 2. best-effort sites: forced failure -> warning, operation still completes -----------------
fresh()
real = captions._ollama_get
captions._ollama_get = lambda *a, **k: (_ for _ in ()).throw(OSError("connection refused"))
try:
    up = captions.is_ollama_up()
finally:
    captions._ollama_get = real
check("captions.is_ollama_up: still answers False", up is False)
check("...and logs why", "Ollama health probe" in cap.text() and "connection refused" in cap.text(), cap.text())

fresh()
real_po = card_rules._po
card_rules._po = lambda: (_ for _ in ()).throw(RuntimeError("no table"))
try:
    label = card_rules.file_provenance_label("some_custom_key")
finally:
    card_rules._po = real_po
check("card_rules.file_provenance_label: falls back to the raw key", label == "some_custom_key", label)
check("...and logs it", "file provenance label" in cap.text() and "some_custom_key" in cap.text(), cap.text())

fresh()
check("curator._auto_caption: unreadable type_metadata is 'no caption'",
      curator._auto_caption({"slug": "item-1", "type_metadata": "{not json"}) is False)
check("...and logs the slug", "item-1" in cap.text(), cap.text())

fresh()
check("physical_piece.date_made_epoch: unreadable JSON is None", physical_piece.date_made_epoch("{bad") is None)
check("...and logs it", "physical_piece" in cap.text(), cap.text())
fresh()
check("physical_piece.date_made_epoch: a real value still works",
      physical_piece.date_made_epoch({"date_made": "2020-05-01"}) is not None and not cap.warnings())

fresh()
saved_file = version_info.VERSION_FILE
bad = Path(TMP) / "VERSION.json"
bad.write_text("{corrupt", encoding="utf-8")
version_info.VERSION_FILE = bad
try:
    info = version_info.get_version_info()
    check("version: a corrupt VERSION.json still yields a version dict", isinstance(info, dict) and "version" in info)
    check("...and logs it", "VERSION.json" in cap.text(), cap.text())
    fresh()
    version_info.VERSION_FILE = Path(TMP) / "missing.json"
    version_info.get_version_info()
    check("version: a MISSING file (a dev checkout) stays quiet", not cap.warnings(), cap.text())
finally:
    version_info.VERSION_FILE = saved_file

from web import common as web_common  # noqa: E402

fresh()
check("web.common.static_version: a missing static file renders '0'", web_common.static_version("css/nope.css") == "0")
check("...and logs the path", "css/nope.css" in cap.text(), cap.text())

from core.object_types import image as image_type  # noqa: E402


class _BadExif:
    def __bool__(self):
        return True

    def get_ifd(self, tag):
        raise ValueError("corrupt IFD")


class _FakeImg:
    def getexif(self):
        return _BadExif()


fresh()
check("image.read_capture_datetime: a corrupt EXIF IFD is 'no date'", image_type.read_capture_datetime(_FakeImg()) is None)
check("...and logs it", "EXIF IFD" in cap.text(), cap.text())

# ---- 3. web: sites that now fail properly (b), and the audit middleware ---------------------------
from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402

client = _testenv.client(webapp.app)

r = client.post("/api/content", data={"media_type": "url", "external_url": "https://example.com/", "tags": "not json"})
check("POST /api/content with tags that aren't JSON: clean 400 (was: tags silently dropped)",
      r.status_code == 400 and "tags must be a JSON list" in r.text, f"{r.status_code} {r.text[:150]}")
check("...in the shared error shape", (r.json().get("error") or {}).get("code") == "bad_request", r.text[:150])

r = client.post("/api/export/build", content=b"{garbled", headers={"content-type": "application/json"})
check("POST /api/export/build with a garbled JSON body: clean 400 (was: built with {})",
      r.status_code == 400 and "not valid JSON" in r.text, f"{r.status_code} {r.text[:150]}")
r = client.post("/api/export/publish", content=b"{garbled", headers={"content-type": "application/json"})
check("POST /api/export/publish with a garbled JSON body: clean 400",
      r.status_code == 400 and "not valid JSON" in r.text, f"{r.status_code} {r.text[:150]}")


def last_audit(path):
    c = db.get_conn()
    try:
        row = c.execute("SELECT form_body, status_code FROM audit_log WHERE op IS NULL AND path = ? "
                        "ORDER BY id DESC LIMIT 1", (path,)).fetchone()
        return (row["form_body"], json.loads(row["form_body"]), row["status_code"]) if row else None
    finally:
        c.close()


PATH = "/api/pending-decisions/999999/resolve"  # a real mutating route; it answers 404, which is fine here
fresh()
r = client.post(PATH, content=b"password=hunter2&x=1", headers={"content-type": "text/plain"})
raw, parsed, status = last_audit(PATH)
check("audit: an unparseable body -> the request still completes", r.status_code in (404, 422) and status == r.status_code,
      f"{r.status_code} vs audit {status}")
check("audit: ...and the row holds the _unparsed marker, not {}",
      parsed == {"_unparsed": True, "content_type": "text/plain", "bytes": 20}, raw)
check("audit: ...with none of the body content", "hunter2" not in raw and "password" not in raw, raw)
check("audit: ...and the parse failure is logged", "parsing the request body" in cap.text(), cap.text())

fresh()
client.post(PATH, content=b"{not json", headers={"content-type": "application/json"})
raw, parsed, _ = last_audit(PATH)
check("audit: malformed JSON -> marker with the byte count",
      parsed == {"_unparsed": True, "content_type": "application/json", "bytes": 9}, raw)

client.post(PATH, content=b'["a","b"]', headers={"content-type": "application/json"})
raw, parsed, _ = last_audit(PATH)
check("audit: a JSON array (not an object) -> marker, and the audit write didn't crash",
      parsed.get("_unparsed") is True and "a" not in parsed, raw)

client.post(PATH, content=b'{"choice": "x", "api_token": "s3cret"}', headers={"content-type": "application/json"})
raw, parsed, _ = last_audit(PATH)
check("audit: a valid JSON object is recorded (it used to be logged as {})", parsed.get("choice") == "x", raw)
check("audit: ...with secret-looking fields still redacted", "s3cret" not in raw, raw)

client.post(PATH, data={"choice": "x"})
raw, parsed, _ = last_audit(PATH)
check("audit: a normal form body is unchanged", parsed == {"choice": "x"}, raw)

client.post(PATH)
raw, parsed, _ = last_audit(PATH)
check("audit: no body at all is still {}", parsed == {}, raw)

# ---- 4. the checker -----------------------------------------------------------------------------
import check_no_silent_except as checker  # noqa: E402

check("check_no_silent_except: the tree is clean", checker.main() == 0)

probe = Path(ROOT) / "core" / "_silent_probe_551.py"
cases = {
    "pass": "def f():\n    try:\n        x()\n    except Exception:\n        pass\n",
    "return": "def f():\n    try:\n        x()\n    except Exception:\n        return None\n",
    "assign": "def f():\n    try:\n        x()\n    except Exception:\n        v = {}\n",
    "continue": "def f(xs):\n    for x in xs:\n        try:\n            x()\n        except Exception:\n            continue\n",
}
try:
    for name, src in cases.items():
        probe.write_text(src, encoding="utf-8")
        check(f"checker flags a silent `except: {name}`", len(checker.find_silent(probe)) == 1)
    probe.write_text("def f():\n    try:\n        x()\n    except Exception as e:\n        log.warning('x: %r', e)\n", encoding="utf-8")
    check("checker accepts a handler that logs", checker.find_silent(probe) == [])
    probe.write_text("def f():\n    try:\n        x()\n    except Exception as e:\n        raise RuntimeError('y') from e\n", encoding="utf-8")
    check("checker accepts a handler that raises", checker.find_silent(probe) == [])
    probe.write_text("def f():\n    try:\n        x()\n    except ValueError:  # silent-ok: malformed = none\n        return None\n", encoding="utf-8")
    check("checker accepts a commented `# silent-ok:` handler", checker.find_silent(probe) == [])
    probe.write_text("def f():\n    try:\n        x()\n    except ValueError:  # silent-ok:\n        return None\n", encoding="utf-8")
    check("...but not one with no reason", len(checker.find_silent(probe)) == 1)
finally:
    probe.unlink(missing_ok=True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("all checks passed")
