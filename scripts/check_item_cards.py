"""Item cards (#514): the client renderer must emit the same card as the Jinja macro.

web/static/js/cards.js builds the asset card (mini size) for the item grids in JS;
web/templates/_card.html builds the same card server-side from core.cards.file_face() (#596).
This renders both for the same fixture items, at mini and small size, and compares them
structurally (tags, attributes, text); checks no face shows provenance (#596) even when the
record carries one; then checks the hostile-input fixture comes out escaped. Needs `node` on PATH (skips with a note if absent,
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
sys.path.insert(0, str(ROOT))
HOSTILE = '<img src=x onerror=alert(1)>"\'&'

ITEMS = [
    {"slug": "abc123", "display_name": "Desk build 01.jpg", "type_label": "Image", "card_date": "Mar 4, 2021",
     "thumb_url": "/f/abc123/thumb", "has_thumbnail": True, "stacked": "Desk Build", "highlight": False, "codes": []},
    {"slug": "def456", "display_name": "Frame.stl", "type_label": "3D model", "card_date": "Jan 9, 2019",
     "thumb_url": None, "has_thumbnail": False, "stacked": None, "highlight": True, "codes": ["3DP", "COL"]},
    {"slug": "x9", "display_name": HOSTILE, "type_label": HOSTILE, "card_date": HOSTILE,
     "thumb_url": '/f/x9/thumb?a="b"&c=<d>', "has_thumbnail": True, "stacked": HOSTILE, "highlight": False,
     "codes": [HOSTILE]},
]
# #596: no file face shows provenance, whatever the record carries.
PROVENANCE_WORDS = ("Found", "Made", "Created", "Documented")


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


def macro_html(item, size="mini"):
    """The macro drawing core.cards.file_face() (#596) for the same item record cards.js gets."""
    from core import cards  # noqa: E402 (repo root is on sys.path, below)
    env = Environment(loader=FileSystemLoader(str(ROOT / "web" / "templates")),
                      autoescape=select_autoescape(["html"]))
    tpl = env.from_string('{% from "_card.html" import card %}{{ card(c, size) }}')
    c = cards.file_face(slug=item["slug"], title=item["display_name"], dates=item["card_date"],
                        type_line=item["type_label"], cover_url=item["thumb_url"], href="/object/" + item["slug"],
                        codes=item["codes"], highlight=item["highlight"], stacked=item.get("stacked"))
    return tpl.render(c=c, size=size)


def js_html(item, size="mini"):
    shim = ("global.window = {}; require(process.argv[1]); "
            "process.stdout.write(window.ItemCards.face(JSON.parse(process.argv[2]), {size: process.argv[3]}));")
    r = subprocess.run(["node", "-e", shim, str(ROOT / "web" / "static" / "js" / "cards.js"), json.dumps(item), size],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise SystemExit("node failed: " + r.stderr)
    return r.stdout


def main():
    if not shutil.which("node"):
        print("node not found: skipping the JS/macro comparison")
        return 0
    bad = 0
    for item, size in [(i, s) for i in ITEMS for s in ("mini", "small")]:
        a, b = flat(macro_html(dict(item, provenance="found"), size)), flat(js_html(dict(item, provenance="found"), size))
        texts = " ".join(t[1] for t in a + b if t[0] == "#text")
        if any(w in texts for w in PROVENANCE_WORDS):
            bad += 1
            print("PROVENANCE on the face for", item["slug"], size)
        if a != b:
            bad += 1
            print("MISMATCH for", item["slug"], size)
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
