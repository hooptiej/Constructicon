"""Item cards (#514): the client renderer must emit the same card as the Jinja macro.

web/static/js/cards.js builds the asset card (mini size) for the item grids in JS;
web/templates/_card.html builds the same card server-side. This renders both for the same
fixture items and compares them structurally (tags, attributes, text), then checks the
hostile-input fixture comes out escaped. Needs `node` on PATH (skips with a note if absent,
exit 0). Run from the repo root:

    python scripts/check_item_cards.py
"""
import json
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
HOSTILE = '<img src=x onerror=alert(1)>"\'&'

ITEMS = [
    {"slug": "abc123", "display_name": "Desk build 01.jpg", "type_label": "Image", "card_date": "Mar 4, 2021",
     "thumb_url": "/f/abc123/thumb", "has_thumbnail": True, "provenance": "found", "highlight": False, "codes": []},
    {"slug": "def456", "display_name": "Frame.stl", "type_label": "3D model", "card_date": "Jan 9, 2019",
     "thumb_url": None, "has_thumbnail": False, "provenance": "", "highlight": True, "codes": ["3DP", "COL"]},
    {"slug": "x9", "display_name": HOSTILE, "type_label": HOSTILE, "card_date": HOSTILE,
     "thumb_url": '/f/x9/thumb?a="b"&c=<d>', "has_thumbnail": True, "provenance": "made", "highlight": False,
     "codes": [HOSTILE]},
]


class Flat(HTMLParser):
    """Flatten markup to [(tag, sorted attrs)] and [text] so whitespace/quoting don't matter."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        self.out.append(("<" + tag, tuple(sorted(attrs))))

    def handle_endtag(self, tag):
        self.out.append(("</" + tag, ()))

    def handle_data(self, data):
        if data.strip():
            self.out.append(("#text", data.strip()))


def flat(markup):
    p = Flat()
    p.feed(markup)
    return p.out


def macro_html(item):
    env = Environment(loader=FileSystemLoader(str(ROOT / "web" / "templates")),
                      autoescape=select_autoescape(["html"]))
    tpl = env.from_string('{% from "_card.html" import card %}{{ card(c, "mini") }}')
    prov = (item["provenance"] or "").strip().capitalize()
    c = {
        "slug": item["slug"], "kind": "asset", "title": item["display_name"], "dates": item["card_date"],
        "type_line": item["type_label"], "provenance": prov, "cover_url": item["thumb_url"],
        "href": "/object/" + item["slug"], "show_level": False, "codes": item["codes"],
        "highlight": item["highlight"], "facts": [], "stats": [],
    }
    return tpl.render(c=c)


def js_html(item):
    shim = ("global.window = {}; require(process.argv[1]); "
            "process.stdout.write(window.ItemCards.face(JSON.parse(process.argv[2]), {size: 'mini'}));")
    r = subprocess.run(["node", "-e", shim, str(ROOT / "web" / "static" / "js" / "cards.js"), json.dumps(item)],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise SystemExit("node failed: " + r.stderr)
    return r.stdout


def main():
    if not shutil.which("node"):
        print("node not found: skipping the JS/macro comparison")
        return 0
    bad = 0
    for item in ITEMS:
        a, b = flat(macro_html(item)), flat(js_html(item))
        if a != b:
            bad += 1
            print("MISMATCH for", item["slug"])
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    print("  first difference at token", i, "\n    macro:", x, "\n    js:   ", y)
                    break
            else:
                print("  lengths differ:", len(a), "vs", len(b))
    raw = js_html(ITEMS[2])
    for needle in ("<img src=x", 'onerror=alert(1)>"', "<d>"):
        if needle in raw:
            bad += 1
            print("UNESCAPED in JS output:", needle)
    # the full wrapper (tools strip) must escape too
    shim = ("global.window = {}; require(process.argv[1]); "
            "process.stdout.write(window.ItemCards.html(JSON.parse(process.argv[2]), "
            "{selectable: 'row', unfiledSlugs: new Set(['x9'])}));")
    item = dict(ITEMS[2], client=HOSTILE, tags=[HOSTILE, "a", "b", "c"], uploaded_by_display=HOSTILE,
                type_icon=HOSTILE, ocr_status="done", extracted_text=HOSTILE)
    r = subprocess.run(["node", "-e", shim, str(ROOT / "web" / "static" / "js" / "cards.js"), json.dumps(item)],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise SystemExit("node failed: " + r.stderr)
    for needle in ("<img src=x", 'onerror=alert(1)>"'):
        if needle in r.stdout:
            bad += 1
            print("UNESCAPED in wrapper output:", needle)
    print("item cards: %d fixture(s) compared, %s" % (len(ITEMS), "FAILED" if bad else "all match, hostile input escaped"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
