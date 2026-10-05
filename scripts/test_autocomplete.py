"""Standalone test for card free-text autocomplete (#523): db.distinct_card_values
and the datalists in project_detail.html. Disposable SQLite DB. Run:
python scripts/test_autocomplete.py
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# cairosvg needs the native cairo lib (absent on dev boxes); this test never renders SVGs.
import types
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))

import core.db as db
from jinja2 import Environment, select_autoescape

db.DB_PATH = tempfile.mktemp(suffix=".db")
db.init_db()

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


credits = ["Printables", "Printables", "Printables", "Thingiverse", "Thingiverse", "alice", "<b>x</b>\"&'", "  Padded  ", "Padded"]
for i, c in enumerate(credits):
    pid = db._create_project(f"p{i}")["id"]
    conn = db.get_conn()
    conn.execute("UPDATE projects SET provenance_credit = ? WHERE id = ?", (c, pid))
    conn.commit()
    conn.close()
for i, c in enumerate([None, "", "   "]):
    pid = db._create_project(f"empty{i}")["id"]
    conn = db.get_conn()
    conn.execute("UPDATE projects SET provenance_credit = ? WHERE id = ?", (c, pid))
    conn.commit()
    conn.close()

vals = db.distinct_card_values("provenance_credit")
# Printables x3; Padded x2 (after trim) and Thingiverse x2 tie, alphabetical.
check("most-used first, ties alphabetical", vals[:3] == ["Printables", "Padded", "Thingiverse"])
check("padded value trimmed and merged", vals.count("Padded") == 1 and "  Padded  " not in vals)
check("no empty values", all(v.strip() for v in vals))
check("distinct", len(vals) == len(set(vals)))
check("capped", len(db.distinct_card_values("provenance_credit", limit=2)) == 2)
check("whereabouts_note allowed (empty here)", db.distinct_card_values("whereabouts_note") == [])
for bad in ("title", "provenance_credit; DROP TABLE projects", "id", ""):
    try:
        db.distinct_card_values(bad)
        check(f"rejects {bad!r}", False)
    except ValueError:
        check(f"rejects {bad!r}", True)

# Render the real template's datalist lines with autoescape on.
src = open(os.path.join(ROOT, "web", "templates", "project_detail.html"), encoding="utf-8").read()
m = re.search(r'<datalist id="provenance-credit-suggest">.*?</datalist>', src)
check("datalist present in template", bool(m))
html = Environment(autoescape=select_autoescape(default=True)).from_string(m.group(0)).render(suggest_credit=vals)
check("hostile value escaped", "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html and "&#34;" in html)
check("label for credit input", 'for="provenance-credit"' in src and 'list="provenance-credit-suggest"' in src)
check("label for whereabouts note", 'for="whereabouts-note"' in src and 'list="whereabouts-note-suggest"' in src)

print("FAILED: %s" % fails if fails else "all passed")
sys.exit(1 if fails else 0)
