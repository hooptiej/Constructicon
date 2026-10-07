#!/usr/bin/env python3
"""Live check for the Office document types (#478): word, powerpoint, spreadsheet.

Builds its own small fixtures (Word/PowerPoint as raw OOXML parts with the
standard library, Excel with openpyxl, which the app image already has),
uploads them to a running instance over HTTP, checks each type, its
properties, previews and search, then deletes everything it uploaded.

LIVE-SERVER TEST (#578): unlike the in-process scripts (which use scripts/_testenv.py and a
throwaway DB/storage/exports), this one talks to a RUNNING instance over HTTP and so writes to that
instance's real storage. It only ever uploads its own uniquely tagged fixtures and deletes those
one by one (they sit in the instance's trash for 7 days, like any delete). It never calls
delete-all or empty-trash, and must never be changed to.

Run it against constructicon-test, never production:

    docker exec constructicon-test python3 scripts/test_office.py
    python3 scripts/test_office.py --base-url http://10.0.1.241

Exits 1 if any check fails. Old binary .doc/.xls/.ppt aren't generated here
(no stdlib writer); check those with real files by hand.
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

TAG = "zqoffice" + uuid.uuid4().hex[:6]
CT = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
CORE = (CT + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
        '<dc:title>{title}</dc:title><dc:creator>Test Script</dc:creator>'
        '<dcterms:created>2024-05-01T10:00:00Z</dcterms:created></cp:coreProperties>')


def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


def make_docx():
    paras = "".join(f"<w:p><w:r><w:t>Paragraph {i} about {TAG} &lt;b&gt;not bold&lt;/b&gt;</w:t></w:r></w:p>" for i in range(15))
    return _zip({"word/document.xml": f"{CT}<w:document {W_NS}><w:body>{paras}</w:body></w:document>",
                 "docProps/core.xml": CORE.format(title=f"{TAG} notes")})


def make_pptx():
    parts = {"ppt/presentation.xml": f"{CT}<p:presentation {A_NS}/>",
             "docProps/core.xml": CORE.format(title=f"{TAG} deck")}
    for i, title in enumerate(["Intro", "Layout", "Wiring"], start=1):
        parts[f"ppt/slides/slide{i}.xml"] = (f"{CT}<p:sld {A_NS}><p:cSld><p:spTree><p:sp><p:txBody>"
                                             f"<a:p><a:r><a:t>{title} {TAG}</a:t></a:r></a:p>"
                                             f"<a:p><a:r><a:t>body {i}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
    parts["ppt/notesSlides/notesSlide1.xml"] = f"{CT}<p:notes {A_NS}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>note {TAG}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>"
    return _zip(parts)


def make_xlsx():
    from openpyxl import Workbook
    import datetime
    wb = Workbook()
    ws = wb.active
    ws.title = "Devices"
    ws.append(["Address", "Type", "Installed"])
    for i in range(1, 31):
        ws.append([f"D{i:03d}", f"Smoke {TAG}", datetime.date(2025, 1, i % 28 + 1)])
    hidden = wb.create_sheet("Calc")
    hidden["A1"] = "x"
    hidden.sheet_state = "hidden"
    wb.properties.title = f"{TAG} devices"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def upload(self, name, data):
        b = uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request(self.base + "/api/upload", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=120).read())
        except urllib.error.HTTPError as e:
            return {"error": e.code}

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=60).read().decode()

    def delete(self, slug):
        urllib.request.urlopen(urllib.request.Request(f"{self.base}/api/image/{slug}/delete", data=b"", method="POST"), timeout=30)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base-url", default="http://localhost:80")
    args = ap.parse_args()
    import _http  # #467 step 2: send the install token to the app (scripts/_http.py)
    _http.install(args.base_url)
    c = Client(args.base_url)
    title = re.search(r"<title>([^<]*)</title>", c.get("/"))
    print("target:", args.base_url, "|", title.group(1) if title else "?")

    failures = []

    def check(label, cond):
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            failures.append(label)

    def props(slug):
        return dict(re.findall(r"<span>([^:<]+): ([^<]*)</span>", c.get(f"/object/{slug}")))

    slugs = {}
    try:
        for name, data, want in (("notes.docx", make_docx(), "word"),
                                 ("deck.pptx", make_pptx(), "powerpoint"),
                                 ("devices.xlsx", make_xlsx(), "spreadsheet")):
            r = c.upload(f"{TAG}-{name}", data)
            slugs[name] = r.get("slug")
            check(f"{name} -> {want}", r.get("media_type") == want)
        check("a .xlsx that isn't a workbook is refused", c.upload(f"{TAG}-fake.xlsx", b"nope").get("error") == 400)
        time.sleep(6)  # background text extraction

        p = props(slugs["notes.docx"])
        check("Word: title + word count", p.get("Title") == f"{TAG} notes" and p.get("Words") == "90")
        check("Word: opening paragraphs shown, escaped", "&lt;b&gt;not bold&lt;/b&gt;" in c.get(f"/object/{slugs['notes.docx']}"))
        # No embedded preview in this .docx: no thumbnail advertised, and the
        # thumb URL must 404, not serve the .docx itself to an <img>.
        # (the page always carries the URL inside a JS selector for the refresh
        # button, so look for an actual <img src=...>)
        check("Word without a preview: no thumbnail advertised",
              not re.search(r'src="/f/' + re.escape(slugs["notes.docx"]) + r'/thumb', c.get(f"/object/{slugs['notes.docx']}")))
        try:
            c.get(f"/f/{slugs['notes.docx']}/thumb")
            check("Word without a preview: thumb URL is 404, not the file", False)
        except urllib.error.HTTPError as e:
            check("Word without a preview: thumb URL is 404, not the file", e.code == 404)
        p = props(slugs["deck.pptx"])
        check("PowerPoint: 3 slides, notes on 1", p.get("Slides") == "3" and p.get("Speaker notes") == "1 slides")
        check("PowerPoint: slide outline", f"<li>Wiring {TAG}</li>" in c.get(f"/object/{slugs['deck.pptx']}"))
        p = props(slugs["devices.xlsx"])
        check("Excel: sheets with sizes, hidden flagged", "Devices (31 × 3)" in p.get("Sheets", "") and "Calc (1 × 1, hidden)" in p.get("Sheets", ""))
        page = c.get(f"/object/{slugs['devices.xlsx']}")
        check("Excel: first sheet as an open table", "<summary>Devices</summary>" in page and "<th>Address</th>" in page)
        check("Excel: dates as dates", "<td>2025-01-02</td>" in page)
        found = {r["slug"] for r in json.loads(c.get("/api/search?" + urllib.parse.urlencode({"query": TAG})))}
        for name in slugs:
            check(f"search finds {name} by its text", slugs[name] in found)
    finally:
        for s in slugs.values():
            if s:
                c.delete(s)
        print(f"cleaned up {len([s for s in slugs.values() if s])} uploads")

    print("ALL PASS" if not failures else f"{len(failures)} FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
