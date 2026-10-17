#!/usr/bin/env python3
"""The type sampler (#720): one test file of every registered object type, for drop-testing an install.

    python3 scripts/make_type_samples.py OUTDIR [--no-executables] [--list]

Writes into OUTDIR (created; existing sample files are overwritten, nothing else is touched):
  * one file per sample in SAMPLES below: every type in core/object_types/ and every extension a type
    claims (scripts/check_type_samples.py fails when one is missing, so the set can't drift);
  * content.json + urls.txt: the types that have no file (a YouTube link, a web page, a written post),
    which scripts/drop_test.py creates through /api/content;
  * manifest.json: per file, the type it should land as and what a correct install shows for it
    (thumbnail, text, how /f/ serves it, the raw view, restricted), derived from the registry at run
    time; scripts/drop_test.py reads it;
  * README.md: the same table for a human, plus the Windows Defender note.

Everything is generated from nothing: no archive data, no network, fixed dates, so the folder is the
same every run (except the files in VARYING: the two PKCS#12 files, whose encryption salt is random,
and the .7z, whose header py7zr stamps with the write time). Built with the standard library and what the app image already has
(Pillow, ffmpeg, fontTools, psd-tools, py7zr, openpyxl, cryptography). Formats with no writer in the
image (legacy .doc/.xls/.ppt/.msi, ISO, VHD, xar, rpm, PE, ELF, SketchUp, ...) are written by hand as
the smallest file the type's sniffer and parser accept; the README marks those "hand-built".

Credential samples (certkey, vpptoken) are OBVIOUSLY FAKE: every name says TEST / NOT REAL, the key
is derived from a public fixed string, the VPP token value is the words "TEST NOT A REAL TOKEN".

EXECUTABLES (owner rule, Windows Defender): samples marked executable (PE .exe files, installer
packages, the macOS app bundle, ELF firmware, the .dmg) are written ONLY on Linux, i.e. inside the
app container. Anywhere else they are skipped with a note, and they are never committed: the default
output folder (samples-out/) is gitignored. Run this in the container:

    docker run --rm --network none -v "$PWD":/app:ro -v "$PWD/samples-out":/out -w /app \\
        <constructicon image> python scripts/make_type_samples.py /out

Needs the app's packages (run it in the app image). Prints "OK: ..." at the end.
"""

import argparse
import base64
import datetime
import gzip
import hashlib
import io
import json
import mimetypes
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MARK = "CONSTRUCTICON SAMPLE"
FIXED = datetime.datetime(2001, 2, 3, 4, 5, 6, tzinfo=datetime.timezone.utc)
ZIP_TIME = (2001, 2, 3, 4, 5, 6)
CAN_WRITE_EXECUTABLES = sys.platform.startswith("linux")


# ============================================================================ small helpers

def _zip(entries, compress=zipfile.ZIP_DEFLATED):
    """A zip with fixed member times, in the given order. entries: [(name, bytes|str)]."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = compress
            info.external_attr = 0o644 << 16
            z.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)
    return buf.getvalue()


def _font(size):
    from PIL import ImageFont
    return ImageFont.load_default(size=size)


def _picture(label, size=(640, 360), mode="RGB"):
    """A white card with big black text (OCR reads it) and a green bar (the owner's colour)."""
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, h - h // 8, w, h], fill=(107, 142, 35))
    big = max(12, min(w // 12, h // 5))
    d.text((w // 20, h // 6), "CONSTRUCTICON", font=_font(big), fill=(0, 0, 0))
    d.text((w // 20, h // 6 + int(big * 1.4)), f"SAMPLE {label}", font=_font(big), fill=(0, 0, 0))
    return img.convert(mode) if mode != "RGB" else img


def _save(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _png(label, size=(320, 180)):
    return _save(_picture(label, size), "PNG")


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[-400:]}")


def _ffmpeg(args, suffix):
    """Run ffmpeg into a temp file and return its bytes (bit-exact flags, one thread)."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed (run this in the app image)")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"out{suffix}"
        _run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args, "-threads", "1",
              "-fflags", "+bitexact", "-flags", "+bitexact", str(out)])
        return out.read_bytes()


# ============================================================================ images

def image_jpeg_exif(_name):
    """JPEG with an EXIF DateTimeOriginal (the item's content_date should come from it, #265)."""
    from PIL import Image
    img = _picture("JPEG EXIF")
    exif = Image.Exif()
    exif[0x0132] = "2001:02:03 04:05:06"           # DateTime
    exif[0x010F] = "Constructicon type sampler"    # Make
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = "2001:02:03 04:05:06"            # DateTimeOriginal
    ifd[0x9004] = "2001:02:03 04:05:06"            # DateTimeDigitized
    return _save(img, "JPEG", quality=90, exif=exif)


def image_as(fmt, label, size=(640, 360), **kw):
    def build(_name):
        img = _picture(label, size)
        return _save(img, fmt, **kw)
    return build


def image_ico(_name):
    img = _picture("ICO", (256, 256))
    return _save(img, "ICO", sizes=[(256, 256)])


def gif_animated(_name):
    frames = [_picture(f"GIF {i}", (480, 270), "P") for i in (1, 2, 3)]
    return _save(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=400, loop=0)


def psd_file(_name):
    from psd_tools import PSDImage
    psd = PSDImage.frompil(_picture("PSD"))
    buf = io.BytesIO()
    psd.save(buf)
    return buf.getvalue()


def svg_file(_name):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">\n'
        '  <title>Constructicon type sampler SVG (#720)</title>\n'
        '  <rect width="640" height="360" fill="#ffffff"/>\n'
        '  <rect y="315" width="640" height="45" fill="#6b8e23"/>\n'
        '  <text x="32" y="120" font-family="DejaVu Sans, sans-serif" font-size="52" fill="#000">CONSTRUCTICON</text>\n'
        '  <text x="32" y="200" font-family="DejaVu Sans, sans-serif" font-size="52" fill="#000">SAMPLE SVG</text>\n'
        '</svg>\n'
    ).encode("utf-8")


def eps_file(_name):
    """Hand-built EPS (plain PostScript; ghostscript renders the thumbnail)."""
    return (
        "%!PS-Adobe-3.0 EPSF-3.0\n"
        "%%BoundingBox: 0 0 480 200\n"
        "%%Title: Constructicon type sampler EPS (#720)\n"
        "%%Creator: scripts/make_type_samples.py\n"
        "%%CreationDate: 2001-02-03\n"
        "%%EndComments\n"
        "1 1 1 setrgbcolor 0 0 480 200 rectfill\n"
        "0.42 0.56 0.14 setrgbcolor 0 0 480 24 rectfill\n"
        "0 0 0 setrgbcolor\n"
        "/Helvetica-Bold findfont 44 scalefont setfont\n"
        "24 120 moveto (CONSTRUCTICON) show\n"
        "24 56 moveto (SAMPLE EPS) show\n"
        "showpage\n"
        "%%EOF\n"
    ).encode("ascii")


def _pdf(lines, title, header=b"%PDF-1.4", creator="Constructicon type sampler (#720)"):
    """A hand-built one-page PDF with a real text layer (Helvetica), fixed dates, valid xref."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = ["BT", "/F1 28 Tf", "72 700 Td"]
    for i, line in enumerate(lines):
        if i:
            ops.append("0 -40 Td")
        ops.append(f"({esc(line)}) Tj")
    ops.append("ET")
    content = "\n".join(ops).encode("ascii")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Title ({esc(title)}) /Creator ({esc(creator)}) /Producer (make_type_samples.py) "
        f"/CreationDate (D:20010203040506Z) /ModDate (D:20010203040506Z) >>".encode("ascii"),
    ]
    out = bytearray(header + b"\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def pdf_file(_name):
    return _pdf([MARK, "PDF with a text layer", "Type sampler #720"], "Constructicon type sampler PDF")


def ai_file(_name):
    """Modern Illustrator files are PDF-compatible: a PDF named .ai (hand-built)."""
    return _pdf([MARK, "ILLUSTRATOR AI (PDF compatible)"], "Constructicon type sampler AI",
                creator="Adobe Illustrator(R) compatible sample, made by make_type_samples.py")


# ============================================================================ documents and text

def text_file(_name):
    return (f"{MARK}: plain text (#720)\n\nThis file was generated by scripts/make_type_samples.py.\n"
            "Line three. Unicode check: café, naïve, ✓.\n").encode("utf-8")


def markdown_file(_name):
    return (f"# {MARK}: Markdown\n\nGenerated by `scripts/make_type_samples.py` (#720).\n\n"
            "- a list item\n- **bold** and *italic*\n\n| column | value |\n|---|---|\n| a | 1 |\n").encode("utf-8")


def csv_file(_name):
    rows = ["id,name,installed,notes"] + [f"{i},device {i:02d},2001-02-{i:02d},{MARK} row {i}" for i in range(1, 21)]
    return ("\n".join(rows) + "\n").encode("utf-8")


def email_file(_name):
    """A hand-written multipart message (plain + HTML + a small attachment), fixed boundary."""
    b1, b2 = "=====sampler-720-mixed=====", "=====sampler-720-alt====="
    return (
        "From: Constructicon Sampler <sampler@example.invalid>\r\n"
        "To: Drop Test <droptest@example.invalid>\r\n"
        "Subject: Constructicon type sampler test message (#720)\r\n"
        "Date: Sat, 03 Feb 2001 04:05:06 +0000\r\n"
        "Message-ID: <type-sampler-720@example.invalid>\r\n"
        "MIME-Version: 1.0\r\n"
        f'Content-Type: multipart/mixed; boundary="{b1}"\r\n\r\n'
        f"--{b1}\r\n"
        f'Content-Type: multipart/alternative; boundary="{b2}"\r\n\r\n'
        f"--{b2}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{MARK}: email body (plain text part).\r\nNothing in this message is real.\r\n\r\n"
        f"--{b2}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        f"<p>{MARK}: email body (<b>HTML</b> part).</p><img src=\"https://example.invalid/track.png\">\r\n\r\n"
        f"--{b2}--\r\n\r\n"
        f"--{b1}\r\n"
        'Content-Type: text/plain; name="attachment.txt"\r\n'
        'Content-Disposition: attachment; filename="attachment.txt"\r\n\r\n'
        "an attached text file\r\n\r\n"
        f"--{b1}--\r\n"
    ).encode("utf-8")


CODE = {
    ".py": f'"""{MARK}: Python."""\n\n\ndef hello():\n    return "hello from the type sampler"\n\n\nprint(hello())\n',
    ".ino": f"// {MARK}: Arduino sketch\nvoid setup() {{ pinMode(13, OUTPUT); }}\nvoid loop() {{ digitalWrite(13, HIGH); delay(500); digitalWrite(13, LOW); delay(500); }}\n",
    ".js": f"// {MARK}: JavaScript\nfunction hello() {{ return 'hello'; }}\nconsole.log(hello());\n",
    ".html": (f"<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>{MARK}: HTML</title></head>\n"
              f"<body><h1>{MARK}: HTML</h1><p>Served as a download from /f/ (#610); the Rendered view is sandboxed.</p>\n"
              "<script>document.title = 'if you can read this in the tab title, the sandbox let script run';</script>\n"
              "</body></html>\n"),
    ".css": f"/* {MARK}: CSS */\nbody {{ color: #333; background: #fff; }}\n.sample {{ border: 1px solid #6b8e23; }}\n",
    ".json": json.dumps({"sample": MARK, "type": "code (.json)", "issue": 720, "items": [1, 2, 3]}, indent=2) + "\n",
    ".yaml": f"# {MARK}: YAML\nsample: true\nissue: 720\nitems:\n  - one\n  - two\n",
    ".yml": f"# {MARK}: YAML (.yml)\nsample: true\nissue: 720\n",
    ".sh": f"#!/bin/sh\n# {MARK}: shell script (never run by the app)\necho 'hello from the type sampler'\n",
    ".ps1": f"# {MARK}: PowerShell (never run by the app)\nWrite-Output 'hello from the type sampler'\n",
    ".lua": f"-- {MARK}: Lua\nlocal function hello() return 'hello' end\nprint(hello())\n",
    ".php": f"<?php\n// {MARK}: PHP (shown as text, never run)\necho '<b>hello</b>';\n",
    ".sql": f"-- {MARK}: SQL\nCREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT);\nINSERT INTO sample VALUES (1, 'type sampler');\n",
    ".c": f"/* {MARK}: C */\n#include <stdio.h>\nint main(void) {{ puts(\"hello\"); return 0; }}\n",
    ".h": f"/* {MARK}: C header */\n#ifndef SAMPLE_H\n#define SAMPLE_H\nint sample(void);\n#endif\n",
    ".cpp": f"// {MARK}: C++\n#include <iostream>\nint main() {{ std::cout << \"hello\" << std::endl; }}\n",
    ".cc": f"// {MARK}: C++ (.cc)\nint add(int a, int b) {{ return a + b; }}\n",
    ".cxx": f"// {MARK}: C++ (.cxx)\nint sub(int a, int b) {{ return a - b; }}\n",
    ".hpp": f"// {MARK}: C++ header\n#pragma once\nstruct Sample {{ int id; }};\n",
    ".hh": f"// {MARK}: C++ header (.hh)\n#pragma once\nint sample();\n",
}


def code_file(ext):
    def build(_name):
        return CODE[ext].encode("utf-8")
    return build


# ============================================================================ Office (OOXML by hand / openpyxl)

CT_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
CORE_XML = (CT_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>{title}</dc:title><dc:creator>Constructicon type sampler</dc:creator>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">2001-02-03T04:05:06Z</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">2001-02-03T04:05:06Z</dcterms:modified>'
            '</cp:coreProperties>')
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
THUMB_REL = '<Relationship Id="rIdThumb" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail" Target="docProps/thumbnail.png"/>'
CORE_REL = '<Relationship Id="rIdCore" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'

MAIN_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ".docm": "application/vnd.ms-word.document.macroEnabled.main+xml",
    ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    ".dotm": "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    ".pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
    ".potx": "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
    ".potm": "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
    ".ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
    ".ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    ".xltm": "application/vnd.ms-excel.template.macroEnabled.main+xml",
}


def _content_types(overrides):
    parts = "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in overrides)
    return (CT_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>' + parts +
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '</Types>')


def _root_rels(main_target):
    return (CT_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="{main_target}"/>'
            + CORE_REL + THUMB_REL + '</Relationships>')


def word_file(ext):
    """A minimal valid WordprocessingML package (hand-built) with an embedded thumbnail."""
    def build(_name):
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        paras = [f"{MARK}: Word {ext}", "A paragraph written by the type sampler (#720).",
                 "Macro-enabled variants carry no actual macro." if ext.endswith("m") else "Third paragraph."]
        body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
        return _zip([
            ("[Content_Types].xml", _content_types([("/word/document.xml", MAIN_TYPES[ext])])),
            ("_rels/.rels", _root_rels("word/document.xml")),
            ("word/document.xml", f"{CT_HEAD}<w:document {w}><w:body>{body}</w:body></w:document>"),
            ("docProps/core.xml", CORE_XML.format(title=f"Constructicon type sampler Word {ext}")),
            ("docProps/thumbnail.png", _png(f"WORD {ext.upper()[1:]}")),
        ])
    return build


def powerpoint_file(ext):
    """The parts the app reads (presentation.xml, three slides, a notes page, a thumbnail).
    Hand-built and minimal: PowerPoint itself may want a slide master to open it."""
    def build(_name):
        ns = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
              f'xmlns:r="{REL_NS}"')
        entries = [
            ("[Content_Types].xml", _content_types(
                [("/ppt/presentation.xml", MAIN_TYPES[ext])] +
                [(f"/ppt/slides/slide{i}.xml", "application/vnd.openxmlformats-officedocument.presentationml.slide+xml")
                 for i in (1, 2, 3)])),
            ("_rels/.rels", _root_rels("ppt/presentation.xml")),
            ("ppt/presentation.xml", f"{CT_HEAD}<p:presentation {ns}><p:sldIdLst>"
                                     + "".join(f'<p:sldId id="{255 + i}" r:id="rId{i}"/>' for i in (1, 2, 3))
                                     + "</p:sldIdLst></p:presentation>"),
            ("ppt/_rels/presentation.xml.rels",
             CT_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             + "".join(f'<Relationship Id="rId{i}" Type="{REL_NS}/slide" Target="slides/slide{i}.xml"/>' for i in (1, 2, 3))
             + "</Relationships>"),
        ]
        for i, title in enumerate([f"{MARK}: PowerPoint {ext}", "Second slide", "Third slide"], start=1):
            entries.append((f"ppt/slides/slide{i}.xml",
                            f"{CT_HEAD}<p:sld {ns}><p:cSld><p:spTree><p:sp><p:txBody>"
                            f"<a:p><a:r><a:t>{title}</a:t></a:r></a:p><a:p><a:r><a:t>slide {i} body</a:t></a:r></a:p>"
                            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"))
        entries.append(("ppt/notesSlides/notesSlide1.xml",
                         f"{CT_HEAD}<p:notes {ns}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>speaker note</a:t>"
                         "</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>"))
        entries.append(("docProps/core.xml", CORE_XML.format(title=f"Constructicon type sampler PowerPoint {ext}")))
        entries.append(("docProps/thumbnail.png", _png(f"SLIDES {ext.upper()[1:]}")))
        return _zip(entries)
    return build


def spreadsheet_file(ext):
    """openpyxl's workbook, repacked with fixed times and properties, the main part's content type set
    for the variant, and an embedded thumbnail."""
    def build(_name):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Devices"
        ws.append(["Address", "Name", "Installed", "Notes"])
        for i in range(1, 21):
            ws.append([f"D{i:03d}", f"device {i}", datetime.date(2001, 2, i), f"{MARK} row {i}"])
        wb.properties.title = f"Constructicon type sampler Excel {ext}"
        buf = io.BytesIO()
        wb.save(buf)
        entries = []
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
            for info in z.infolist():
                data = z.read(info.filename)
                if info.filename == "[Content_Types].xml":
                    data = data.replace(MAIN_TYPES[".xlsx"].encode(), MAIN_TYPES[ext].encode())
                    if b'Extension="png"' not in data:
                        data = data.replace(b"<Default ", b'<Default Extension="png" ContentType="image/png"/><Default ', 1)
                elif info.filename == "_rels/.rels":
                    data = data.replace(b"</Relationships>", THUMB_REL.encode() + b"</Relationships>")
                elif info.filename == "docProps/core.xml":
                    data = CORE_XML.format(title=f"Constructicon type sampler Excel {ext}").encode()
                entries.append((info.filename, data))
        entries.append(("docProps/thumbnail.png", _png(f"SHEET {ext.upper()[1:]}")))
        return _zip(entries)
    return build


# ============================================================================ OLE compound files (legacy Office, MSI)

def ole_file(streams):
    """A hand-built OLE2 compound file (CFB v3, 512-byte sectors) holding `streams` {name: bytes}.
    Each stream is padded to 4096 bytes so it lives in regular sectors (no mini stream)."""
    SECT, FREE, END, FATSECT, NOSTREAM = 512, 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
    names = sorted(streams, key=lambda n: (len(n), n.upper()))
    entries = len(names) + 1
    dir_sectors = (entries * 128 + SECT - 1) // SECT
    data = {n: streams[n] + b"\x00" * max(0, 4096 - len(streams[n])) for n in names}
    data = {n: d + b"\x00" * (-len(d) % SECT) for n, d in data.items()}
    fat = [FATSECT]
    first_dir = len(fat)
    fat += [first_dir + i + 1 for i in range(dir_sectors - 1)] + [END]
    starts = {}
    for n in names:
        count = len(data[n]) // SECT
        starts[n] = len(fat)
        fat += [starts[n] + i + 1 for i in range(count - 1)] + [END]
    if len(fat) > SECT // 4:
        raise ValueError("ole_file: too many sectors for one FAT sector")
    fat += [FREE] * (SECT // 4 - len(fat))

    def entry(name, etype, child=NOSTREAM, right=NOSTREAM, start=END, size=0):
        raw = name.encode("utf-16-le") + b"\x00\x00"
        return (raw.ljust(64, b"\x00") + struct.pack("<HBB", len(raw), etype, 1)
                + struct.pack("<III", NOSTREAM, right, child) + b"\x00" * 16 + struct.pack("<I", 0)
                + struct.pack("<QQ", 0, 0) + struct.pack("<IQ", start, size))

    dirs = entry("Root Entry", 5, child=1 if names else NOSTREAM)
    for i, n in enumerate(names, start=1):
        dirs += entry(n, 2, right=i + 1 if i < len(names) else NOSTREAM, start=starts[n], size=len(data[n]))
    empty = b"\x00" * 64 + struct.pack("<HBB", 0, 0, 0) + struct.pack("<III", NOSTREAM, NOSTREAM, NOSTREAM) + b"\x00" * 48
    while len(dirs) < dir_sectors * SECT:
        dirs += empty
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
              + b"\x00" * 6 + struct.pack("<IIIIIIIII", 0, 1, first_dir, 0, 4096, END, 0, END, 0)
              + struct.pack("<I", 0) + struct.pack("<108I", *([FREE] * 108)))
    body = struct.pack(f"<{SECT // 4}I", *fat) + dirs + b"".join(data[n] for n in names)
    return header + body


def legacy_office(stream_name, label):
    def build(_name):
        text = f"{MARK}: {label} (hand-built OLE container; no real document inside)".encode("utf-16-le")
        return ole_file({stream_name: text, "SamplerNote": f"{MARK} #720".encode("ascii")})
    return build


def msi_file(_name):
    """An OLE container with the MSI class layout's magic (the sniffer checks the magic; the parser is
    best-effort). Hand-built; it installs nothing and no tool would run it."""
    return ole_file({"SamplerNote": f"{MARK}: Windows Installer placeholder (TEST)".encode("ascii")})


# ============================================================================ archives

def _archive_members():
    return [("constructicon-sample/readme.txt", f"{MARK}: inside an archive (#720)\n".encode()),
            ("constructicon-sample/picture.png", _png("IN ARCHIVE", (160, 90))),
            ("constructicon-sample/notes/notes.md", b"# notes\n\nnested file\n")]


def archive_zip(_name):
    return _zip(_archive_members())


def _tar_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for name, data in _archive_members():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode, info.uname, info.gname = len(data), int(FIXED.timestamp()), 0o644, "sampler", "sampler"
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def archive_tar(_name):
    return _tar_bytes()


def archive_targz(_name):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as g:
        g.write(_tar_bytes())
    return buf.getvalue()


def archive_7z(_name):
    import py7zr
    buf = io.BytesIO()
    with py7zr.SevenZipFile(buf, "w") as z:
        for name, data in _archive_members():
            z.writestr(data, name)
    return buf.getvalue()


# ============================================================================ 3D, fonts, SketchUp

def stl_file(_name):
    """A binary STL cube (12 triangles)."""
    v = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    out = b"Constructicon type sampler cube (#720)".ljust(80, b" ") + struct.pack("<I", len(faces))
    for a, b, c in faces:
        out += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v[i]) for i in (a, b, c)) + b"\x00\x00"
    return out


def _font_builder(is_ttf):
    from fontTools.fontBuilder import FontBuilder
    fb = FontBuilder(1000, isTTF=is_ttf)
    order = [".notdef", "space", "box"]
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({32: "space", **{c: "box" for c in range(33, 127)}})
    return fb, order


def _finish_font(fb, family):
    fb.setupHorizontalMetrics({".notdef": (600, 50), "space": (300, 0), "box": (600, 60)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular",
                       "uniqueFontIdentifier": f"{family} (TEST, #720)", "version": "Version 0.720"})
    fb.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    fb.font.recalcTimestamp = False
    fixed = int((FIXED - datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds())
    fb.font["head"].created = fb.font["head"].modified = fixed
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def font_ttf(_name):
    """A tiny TrueType font: every printable ASCII character is one box glyph."""
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    fb, _ = _font_builder(True)

    def rect(x0, y0, x1, y1):
        pen = TTGlyphPen(None)
        pen.moveTo((x0, y0)); pen.lineTo((x0, y1)); pen.lineTo((x1, y1)); pen.lineTo((x1, y0)); pen.closePath()
        return pen.glyph()
    fb.setupGlyf({".notdef": rect(50, 0, 550, 700), "space": TTGlyphPen(None).glyph(), "box": rect(60, 0, 540, 700)})
    return _finish_font(fb, "Constructicon Sampler TTF")


def font_otf(_name):
    """The same font with CFF outlines (.otf)."""
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    fb, _ = _font_builder(False)

    def rect(x0, y0, x1, y1, width):
        pen = T2CharStringPen(width, None)
        pen.moveTo((x0, y0)); pen.lineTo((x0, y1)); pen.lineTo((x1, y1)); pen.lineTo((x1, y0)); pen.closePath()
        return pen.getCharString()
    empty = T2CharStringPen(300, None).getCharString()
    fb.setupCFF("ConstructiconSamplerOTF-Regular", {"FullName": "Constructicon Sampler OTF"},
                {".notdef": rect(50, 0, 550, 700, 600), "space": empty, "box": rect(60, 0, 540, 700, 600)}, {})
    return _finish_font(fb, "Constructicon Sampler OTF")


def sketchup_file(_name):
    """Hand-built: the SketchUp header the sniffer reads, then a preview PNG (no real model data)."""
    head = (b"\xff\xfe\xff\x0e" + "SketchUp Model".encode("utf-16-le") + b"\xff\xfe\xff\x0a"
            + "{21.0.391}".encode("utf-16-le"))
    return head + b"\x00" * 16 + _png("SKETCHUP", (320, 240)) + b"\x00" * 64


# ============================================================================ audio, video

def video_file(suffix):
    def build(_name):
        return _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                        "-metadata", "creation_time=2005-06-07T08:09:10Z",
                        "-metadata", f"title={MARK} video"], suffix)
    return build


def audio_file(suffix, codec_args):
    def build(_name):
        return _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", *codec_args,
                        "-metadata", f"title={MARK} {suffix[1:].upper()}",
                        "-metadata", "artist=Constructicon Sampler", "-metadata", "album=Type Sampler 720",
                        "-metadata", "date=2001", "-metadata", "genre=Test"], suffix)
    return build


# ============================================================================ credentials (all FAKE)

FAKE_KEY_SEED = b"CONSTRUCTICON-SAMPLE-KEY-TEST!!!"  # 32 public bytes: this "key" protects nothing


def _fake_key_and_cert():
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID
    key = ed25519.Ed25519PrivateKey.from_private_bytes(FAKE_KEY_SEED)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CONSTRUCTICON TEST CERTIFICATE - NOT REAL"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Constructicon type sampler (#720) TEST")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(0x720).not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
            .not_valid_after(datetime.datetime(2099, 12, 31, tzinfo=datetime.timezone.utc))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, None))
    return key, cert


def cert_pem(_name):
    from cryptography.hazmat.primitives import serialization
    _, cert = _fake_key_and_cert()
    return cert.public_bytes(serialization.Encoding.PEM)


def cert_der(_name):
    from cryptography.hazmat.primitives import serialization
    _, cert = _fake_key_and_cert()
    return cert.public_bytes(serialization.Encoding.DER)


def key_pem(_name):
    from cryptography.hazmat.primitives import serialization
    key, _ = _fake_key_and_cert()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def cert_and_key_pem(name):
    return cert_pem(name) + key_pem(name)


def csr_pem(_name):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import NameOID
    key, _ = _fake_key_and_cert()
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "constructicon-test-csr.invalid (TEST)")]))
           .sign(key, None))
    return csr.public_bytes(serialization.Encoding.PEM)


def pkcs12_file(_name):
    """Sealed with the password TEST (random salt, so these bytes differ every run)."""
    from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
    key, cert = _fake_key_and_cert()
    return pkcs12.serialize_key_and_certificates(b"constructicon-test", key, cert, None,
                                                 BestAvailableEncryption(b"TEST"))


def vpptoken_file(_name):
    body = {"token": "TEST-NOT-A-REAL-TOKEN-constructicon-type-sampler-720",
            "expDate": "2099-01-01T00:00:00+0000",
            "orgName": "Constructicon Sample Org (TEST, not real)"}
    return base64.b64encode(json.dumps(body, sort_keys=True).encode("utf-8")) + b"\n"


# ============================================================================ disk images (hand-built)

def _both16(n):
    return struct.pack("<H", n) + struct.pack(">H", n)


def _both32(n):
    return struct.pack("<I", n) + struct.pack(">I", n)


def iso_file(_name):
    """A minimal valid ISO 9660 volume (empty root directory, path tables, terminator)."""
    S = 2048
    rec_date = bytes([101, 2, 3, 4, 5, 6, 0])

    def dir_record(name, lba):
        body = (bytes([0]) + _both32(lba) + _both32(S) + rec_date + bytes([2, 0, 0]) + _both16(1)
                + bytes([len(name)]) + name)
        rec = bytes([len(body) + 1]) + body
        return rec + b"\x00" * (len(rec) % 2)

    def a(s, n):
        return s.upper().ljust(n)[:n].encode("ascii")

    date17 = b"2001020304050600" + b"\x00"
    pvd = bytearray(S)
    pvd[0:8] = b"\x01CD001\x01\x00"
    pvd[8:40] = a("LINUX", 32)
    pvd[40:72] = a("CONSTRUCTICON_SAMPLE", 32)
    pvd[80:88] = _both32(21)
    pvd[120:124] = _both16(1)
    pvd[124:128] = _both16(1)
    pvd[128:132] = _both16(S)
    pvd[132:140] = _both32(10)
    pvd[140:144] = struct.pack("<I", 19)
    pvd[148:152] = struct.pack(">I", 20)
    pvd[156:190] = dir_record(b"\x00", 18)
    pvd[190:318] = a("CONSTRUCTICON TYPE SAMPLER", 128)
    pvd[318:446] = a("CONSTRUCTICON TYPE SAMPLER TEST", 128)
    pvd[446:574] = a("MAKE_TYPE_SAMPLES.PY", 128)
    pvd[574:702] = a("CONSTRUCTICON #720", 128)
    for off in (813, 830, 864):
        pvd[off:off + 17] = date17
    pvd[847:864] = b"0" * 16 + b"\x00"
    pvd[881] = 1
    term = bytearray(S)
    term[0:7] = b"\xffCD001\x01"
    root = bytearray(S)
    dot, dotdot = dir_record(b"\x00", 18), dir_record(b"\x01", 18)
    root[0:len(dot)] = dot
    root[len(dot):len(dot) + len(dotdot)] = dotdot
    pt_l = bytes([1, 0]) + struct.pack("<I", 18) + struct.pack("<H", 1) + b"\x00\x00"
    pt_m = bytes([1, 0]) + struct.pack(">I", 18) + struct.pack(">H", 1) + b"\x00\x00"
    return (b"\x00" * (16 * S) + bytes(pvd) + bytes(term) + bytes(root)
            + pt_l.ljust(S, b"\x00") + pt_m.ljust(S, b"\x00"))


def raw_mbr_image(_name):
    """A small raw disk image with an MBR (one FAT32 partition entry) and nothing else."""
    img = bytearray(64 * 1024)
    img[446:462] = bytes([0x00, 0, 2, 0, 0x0C, 0, 0, 0]) + struct.pack("<II", 1, 127)
    img[510:512] = b"\x55\xaa"
    img[1024:1024 + 40] = f"{MARK} raw disk image".encode().ljust(40, b"\x00")
    return bytes(img)


def dmg_file(_name):
    """Raw data plus a UDIF 'koly' trailer (no partition map). Hand-built; mounts nothing."""
    data = bytearray(4096)
    data[0:40] = f"{MARK} dmg placeholder".encode().ljust(40, b"\x00")
    koly = bytearray(512)
    koly[0:4] = b"koly"
    koly[4:12] = struct.pack(">II", 4, 512)
    koly[492:500] = struct.pack(">Q", len(data) // 512)
    return bytes(data) + bytes(koly)


def sparseimage_file(_name):
    head = bytearray(4096)
    head[0:4] = b"sprs"
    head[4:16] = struct.pack(">III", 3, 8, 1)
    return bytes(head)


def vhdx_file(_name):
    head = bytearray(64 * 1024)
    head[0:8] = b"vhdxfile"
    creator = "Constructicon type sampler (TEST)".encode("utf-16-le")
    head[8:8 + len(creator)] = creator
    return bytes(head)


def qcow2_file(_name):
    """qcow2 v3 header for an empty 1 MiB disk."""
    h = struct.pack(">4sIQIIQIIQQIIQQQQII", b"QFI\xfb", 3, 0, 0, 16, 1024 * 1024, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 104)
    return h.ljust(512, b"\x00")


def vmdk_file(_name):
    return ("# Disk DescriptorFile\n# " + MARK + " (TEST)\nversion=1\nCID=07200720\nparentCID=ffffffff\n"
            'createType="monolithicFlat"\n\n# Extent description\nRW 2048 FLAT "sample-flat.vmdk" 0\n\n'
            '# The Disk Data Base\nddb.virtualHWVersion = "4"\nddb.adapterType = "ide"\n').encode("ascii")


def vhd_file(_name):
    """A fixed VHD: one 512-byte sector of data and the 'conectix' footer (checksum computed)."""
    stamp = int((FIXED - datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds())
    f = bytearray(512)
    f[0:8] = b"conectix"
    f[8:24] = struct.pack(">IIQ", 2, 0x00010000, 0xFFFFFFFFFFFFFFFF)
    f[24:40] = struct.pack(">I4sI4s", stamp, b"ctcn", 0x00010000, b"Wi2k")
    f[40:56] = struct.pack(">QQ", 512, 512)
    f[56:64] = struct.pack(">HBBI", 1, 1, 1, 2)
    f[68:84] = hashlib.md5(b"constructicon-sampler-720").digest()
    f[64:68] = struct.pack(">I", (~sum(f)) & 0xFFFFFFFF)
    data = f"{MARK} vhd sector".encode().ljust(512, b"\x00")
    return data + bytes(f)


# ============================================================================ executables, installers, bundles
# Written ONLY on Linux (see the module docstring): Defender cloud-scans synthetic PE files.

def _pe(overlay=b""):
    """A minimal 32-bit GUI PE: one 0x200-byte .text section whose entry point is a bare `ret`, plus an
    optional overlay. Same layout as scripts/test_round2.py's build_pe. It does nothing if run."""
    section = b"\xc3" + b"\x90" * 0x1FF
    opt = struct.pack("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII",
                      0x10B, 1, 0, 0x200, 0, 0, 0x1000, 0x1000, 0x2000, 0x400000, 0x1000, 0x200,
                      4, 0, 0, 0, 4, 0, 0, 0x2000, 0x200, 0, 2, 0, 0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dd = b"\x00" * (16 * 8)
    coff = struct.pack("<HHIIIHH", 0x14C, 1, int(FIXED.timestamp()), 0, 0, 96 + len(dd), 0x102)
    sect = struct.pack("<8sIIIIIIHHI", b".text", 0x200, 0x1000, 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
    header = b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x40) + b"PE\x00\x00" + coff + opt + dd + sect
    header += b"\x00" * (0x200 - len(header))
    return header + section + overlay


def pe_application(_name):
    return _pe()


def pe_installer_nsis(_name):
    """The PE plus an overlay carrying the NSIS marker, the fingerprint installer.py looks for. No payload."""
    return _pe(b"\xef\xbe\xad\xdeNullsoftInst" + f" {MARK}: NSIS-shaped placeholder (TEST)".encode().ljust(512, b"\x00"))


def _xar(files, ext_note):
    """A xar archive (macOS .pkg / .mpkg / .xip): header, zlib TOC, plain heap."""
    heap, toc_files = b"", []
    for i, (name, data) in enumerate(files, start=1):
        toc_files.append(f'<file id="{i}"><name>{name}</name><type>file</type><data><offset>{len(heap)}</offset>'
                         f'<length>{len(data)}</length><size>{len(data)}</size>'
                         '<encoding style="application/octet-stream"/></data></file>')
        heap += data
    toc = (f'<?xml version="1.0" encoding="UTF-8"?><xar><toc><creation-time>2001-02-03T04:05:06</creation-time>'
           f'{"".join(toc_files)}</toc></xar>').encode("utf-8")
    ctoc = zlib.compress(toc, 9)
    return struct.pack(">4sHHQQI", b"xar!", 28, 1, len(ctoc), len(toc), 0) + ctoc + heap


def pkg_file(_name):
    dist = (f'<?xml version="1.0" encoding="utf-8"?><installer-gui-script minSpecVersion="1">'
            f'<title>Constructicon Sampler (TEST)</title><product id="invalid.example.constructicon.sampler" '
            f'version="0.0.720"/></installer-gui-script>').encode()
    return _xar([("Distribution", dist)], "pkg")


def mpkg_file(name):
    return pkg_file(name)


def xip_file(_name):
    return _xar([("Content", f"{MARK}: xip placeholder (TEST)".encode()), ("Metadata", b"<plist/>")], "xip")


def deb_file(_name):
    control = (f"Package: constructicon-sampler-test\nVersion: 0.0.720\nArchitecture: all\n"
               f"Maintainer: Constructicon Sampler <sampler@example.invalid>\nInstalled-Size: 1\n"
               f"Description: {MARK} (TEST package, installs nothing)\n").encode()

    def tgz(members):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as t:
            for n, d in members:
                info = tarfile.TarInfo(n)
                info.size, info.mtime, info.mode = len(d), int(FIXED.timestamp()), 0o644
                t.addfile(info, io.BytesIO(d))
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as g:
            g.write(raw.getvalue())
        return out.getvalue()

    def member(n, d):
        hdr = f"{n:<16}{int(FIXED.timestamp()):<12}{0:<6}{0:<6}{100644:<8}{len(d):<10}`\n".encode("ascii")
        return hdr + d + (b"\n" if len(d) % 2 else b"")

    return (b"!<arch>\n" + member("debian-binary", b"2.0\n") + member("control.tar.gz", tgz([("./control", control)]))
            + member("data.tar.gz", tgz([])))


def rpm_file(_name):
    """RPM lead + an empty signature header + a header with name/version/release/summary/arch."""
    def header(tags):
        store, index = b"", b""
        for tag, value in tags:
            index += struct.pack(">IIII", tag, 6, len(store), 1)
            store += value.encode() + b"\x00"
        return b"\x8e\xad\xe8\x01" + b"\x00" * 4 + struct.pack(">II", len(tags), len(store)) + index + store

    lead = (b"\xed\xab\xee\xdb" + bytes([3, 0]) + struct.pack(">HH", 0, 1)
            + b"constructicon-sampler-test-0.0.720".ljust(66, b"\x00") + struct.pack(">HH", 1, 5) + b"\x00" * 16)
    sig = header([])
    sig += b"\x00" * (-(len(lead) + len(sig)) % 8)
    return lead + sig + header([(1000, "constructicon-sampler-test"), (1001, "0.0.720"), (1002, "1"),
                                (1004, f"{MARK} (TEST package, installs nothing)"), (1021, "linux"),
                                (1022, "noarch")])


def appx_file(_name):
    manifest = ('<?xml version="1.0" encoding="utf-8"?><Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">'
                '<Identity Name="Constructicon.Sampler.Test" Version="0.0.720.0" Publisher="CN=Constructicon TEST, O=Not Real" '
                'ProcessorArchitecture="neutral"/><Properties><DisplayName>Constructicon Sampler (TEST)</DisplayName>'
                '<PublisherDisplayName>Constructicon TEST</PublisherDisplayName></Properties>'
                '<Dependencies><TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.19041.0"/>'
                '</Dependencies></Package>')
    return _zip([("AppxManifest.xml", manifest), ("readme.txt", f"{MARK}: MSIX/APPX placeholder (TEST)\n")])


def appx_bundle_file(_name):
    manifest = ('<?xml version="1.0" encoding="utf-8"?><Bundle xmlns="http://schemas.microsoft.com/appx/2013/bundle" SchemaVersion="5.0">'
                '<Identity Name="Constructicon.Sampler.Test" Version="0.0.720.0" Publisher="CN=Constructicon TEST, O=Not Real"/>'
                '<Packages><Package Type="application" Version="0.0.720.0" Architecture="x64" FileName="sampler_x64.msix" Offset="0" Size="0"/>'
                '<Package Type="application" Version="0.0.720.0" Architecture="arm64" FileName="sampler_arm64.msix" Offset="0" Size="0"/>'
                '</Packages></Bundle>')
    return _zip([("AppxMetadata/AppxBundleManifest.xml", manifest)])


def macos_app_zip(_name):
    """A zipped .app bundle: Info.plist, a 32-byte arm64 Mach-O header with no code, an .icns icon."""
    plist = plistlib.dumps({
        "CFBundleName": "Constructicon Sampler", "CFBundleDisplayName": "Constructicon Sampler (TEST)",
        "CFBundleIdentifier": "invalid.example.constructicon.sampler", "CFBundleShortVersionString": "0.0.720",
        "CFBundleVersion": "720", "CFBundleExecutable": "Sampler", "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "11.0", "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHumanReadableCopyright": f"{MARK} (TEST, not a real app)",
    }, fmt=plistlib.FMT_XML, sort_keys=True)
    macho = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0)
    icon_png = _png("APP", (128, 128))
    icns = b"icns" + struct.pack(">I", 8 + 8 + len(icon_png)) + b"ic07" + struct.pack(">I", 8 + len(icon_png)) + icon_png
    return _zip([("Sampler.app/Contents/Info.plist", plist), ("Sampler.app/Contents/MacOS/Sampler", macho),
                 ("Sampler.app/Contents/Resources/AppIcon.icns", icns),
                 ("Sampler.app/Contents/PkgInfo", b"APPL????")])


def elf_arm(_name):
    """A 32-bit little-endian ARM ELF header (an embedded target the firmware type accepts) plus a
    string table of text. No program headers: nothing loads or runs it."""
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\x00" * 8
    hdr = ident + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0x8000, 0, 0, 0x05000000, 52, 0, 0, 0, 0, 0)
    return hdr + f"\x00{MARK}: ARM firmware placeholder (TEST)\x00build 720\x00".encode()


def intel_hex(_name):
    """Intel HEX records (checksums computed) carrying a short message, then EOF."""
    data = f"{MARK} HEX #720".encode()
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        rec = bytes([len(chunk), (off >> 8) & 0xFF, off & 0xFF, 0]) + chunk
        lines.append(":" + rec.hex().upper() + f"{(-sum(rec)) & 0xFF:02X}")
    lines.append(":00000001FF")
    return ("\n".join(lines) + "\n").encode("ascii")


# ============================================================================ the manifest

@dataclass
class Sample:
    file: str
    media_type: str
    build: object
    executable: bool = False   # PE / installer / app bundle / ELF / dmg: Linux only, never committed
    thumbnail: object = None   # None = what the registry says; True/False = this file's own answer
    text: object = None        # None = the type's ocr_capable; True/False = this file's own answer
    date: bool = False         # the file carries a date the item's content_date should come from
    disposition: object = None  # None = #701's rule via the registry; "inline"/"attachment" = the intent,
                                # set where the registry's answer is a known bug, so the drop test shows it
    hand_built: bool = False   # no writer in the image: the smallest file the sniffer/parser accept
    shows: str = ""            # what a correct drop test shows, for the README


S = Sample
SAMPLES = [
    # images (UPLOADED_FILE: the file is its own thumbnail; OCR reads the big text)
    S("image-photo-exif.jpg", "image", image_jpeg_exif, date=True, shows="photo; content date 2001-02-03 from EXIF"),
    S("image-photo.jpeg", "image", image_as("JPEG", "JPEG", quality=90), shows="image"),
    S("image-photo.jpe", "image", image_as("JPEG", "JPE", quality=90), shows="image"),
    S("image-photo.jfif", "image", image_as("JPEG", "JFIF", quality=90), shows="image"),
    S("image-screenshot.png", "image", image_as("PNG", "PNG"), shows="image"),
    S("image-bitmap.bmp", "image", image_as("BMP", "BMP"), shows="image"),
    S("image-icon.ico", "image", image_ico, shows="image (256 px icon)"),
    S("image-scan.tif", "image", image_as("TIFF", "TIF", compression="tiff_lzw"), shows="image (downloaded, not inline)"),
    S("image-scan.tiff", "image", image_as("TIFF", "TIFF", compression="tiff_lzw"), shows="image (downloaded, not inline)"),
    S("image-web.webp", "image", image_as("WEBP", "WEBP", lossless=True), shows="image"),
    S("gif-animated.gif", "gif", gif_animated, shows="animated GIF, 3 frames"),
    S("psd-layered.psd", "psd", psd_file, shows="Photoshop composite rendered as the thumbnail"),
    S("svg-drawing.svg", "svg", svg_file, shows="SVG served inline but sandboxed (#610)"),
    S("eps-drawing.eps", "eps", eps_file, hand_built=True, shows="ghostscript-rendered thumbnail"),
    S("ai-artwork.ai", "ai", ai_file, hand_built=True, shows="PDF-compatible .ai, first page rendered"),
    S("pdf-document.pdf", "pdf", pdf_file, hand_built=True, shows="first page rendered; text layer read"),
    # text-like
    S("text-notes.txt", "text", text_file, shows="text shown inline"),
    S("markdown-readme.md", "markdown", markdown_file, shows="rendered Markdown"),
    S("markdown-readme.markdown", "markdown", markdown_file, shows="rendered Markdown"),
    S("data-table.csv", "data", csv_file, shows="wide table preview"),
    S("email-message.eml", "email", email_file, hand_built=True, date=True,
      shows="headers + escaped body, 1 attachment listed; date from the Date: header"),
] + [
    S(f"code-sample{ext}", "code", code_file(ext),
      shows="highlighted source; Open raw" + (" (Rendered/Code toggle; /f/ is a download)" if ext == ".html" else ""))
    for ext in sorted(CODE)
] + [
    # Office: OOXML (each carries docProps/thumbnail.png, so a thumbnail is expected) and legacy OLE
    *[S(f"word-document{e}", "word", word_file(e), thumbnail=True, hand_built=True, date=True,
        shows="paragraph preview, embedded thumbnail") for e in (".docx", ".docm", ".dotx", ".dotm")],
    *[S(f"powerpoint-deck{e}", "powerpoint", powerpoint_file(e), thumbnail=True, hand_built=True, date=True,
        shows="slide outline (3 slides, 1 note), embedded thumbnail") for e in (".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm")],
    *[S(f"spreadsheet-workbook{e}", "spreadsheet", spreadsheet_file(e), thumbnail=True, date=True,
        shows="sheet table preview, embedded thumbnail") for e in (".xlsx", ".xlsm", ".xltx", ".xltm")],
    S("word-legacy.doc", "word", legacy_office("WordDocument", "Word 97-2003"), thumbnail=False, text=False,
      hand_built=True, shows="legacy format recognised; no preview or text (no .doc/.xls/.ppt text reader; OCR reads failed, #724)"),
    S("spreadsheet-legacy.xls", "spreadsheet", legacy_office("Workbook", "Excel 97-2003"), thumbnail=False, text=False,
      hand_built=True, shows="legacy format recognised; no preview or text (no .doc/.xls/.ppt text reader; OCR reads failed, #724)"),
    S("powerpoint-legacy.ppt", "powerpoint", legacy_office("PowerPoint Document", "PowerPoint 97-2003"),
      thumbnail=False, text=False, hand_built=True,
      shows="legacy format recognised; no preview or text (no .doc/.xls/.ppt text reader; OCR reads failed, #724)"),
    # archives
    S("archive-bundle.zip", "archive", archive_zip, shows="member listing (3 files)"),
    S("archive-bundle.7z", "archive", archive_7z, shows="member listing (3 files)"),
    S("archive-bundle.tar", "archive", archive_tar, shows="member listing (3 files)"),
    S("archive-bundle.tar.gz", "archive", archive_targz, shows="member listing (3 files)"),
    S("archive-bundle.tgz", "archive", archive_targz, shows="member listing (3 files)"),
    # 3D, fonts, SketchUp
    S("stl-cube.stl", "stl", stl_file, shows="rendered mesh (12 triangles)"),
    S("font-sampler.ttf", "font", font_ttf, shows="glyph sample (every letter is a box)"),
    S("font-sampler.otf", "font", font_otf, shows="glyph sample (every letter is a box)"),
    S("sketchup-model.skp", "sketchup", sketchup_file, hand_built=True, shows="embedded preview PNG as the thumbnail"),
    # audio / video (ffmpeg)
    S("video-clip.mp4", "video", video_file(".mp4"), date=True, shows="player + frame thumbnail; date from creation_time"),
    S("video-clip.mov", "video", video_file(".mov"), date=True, shows="player + frame thumbnail; date from creation_time"),
    S("audio-tone.mp3", "audio", audio_file(".mp3", ["-c:a", "libmp3lame", "-b:a", "64k", "-id3v2_version", "3"]),
      shows="player; title/artist/album from the tags"),
    S("audio-tone.m4a", "audio", audio_file(".m4a", ["-c:a", "aac", "-b:a", "64k"]), shows="player; tags"),
    S("audio-tone.ogg", "audio", audio_file(".ogg", ["-c:a", "libvorbis", "-q:a", "2"]), shows="player; tags"),
    # #701 means WAV to play inline; Python 3.14 guesses audio/vnd.wave, which the allowlist lacks (#723)
    S("audio-tone.wav", "audio", audio_file(".wav", ["-c:a", "pcm_s16le"]), disposition="inline",
      shows="player; RIFF INFO tags; /f/ inline (fails until #723 is fixed)"),
    # credentials: RESTRICTED, all fake
    S("certkey-cert.crt", "certkey", cert_pem, date=True, shows="RESTRICTED: certificate facts (TEST cert, valid to 2099)"),
    S("certkey-cert.cer", "certkey", cert_der, date=True, shows="RESTRICTED: certificate facts (DER)"),
    S("certkey-cert.der", "certkey", cert_der, date=True, shows="RESTRICTED: certificate facts (DER)"),
    S("certkey-bundle.pem", "certkey", cert_and_key_pem, date=True, shows="RESTRICTED: cert + matching key"),
    S("certkey-private.key", "certkey", key_pem, shows="RESTRICTED: private key (Ed25519, public test seed)"),
    S("certkey-request.csr", "certkey", csr_pem, shows="RESTRICTED: certificate request"),
    S("certkey-bundle.p12", "certkey", pkcs12_file, shows="RESTRICTED: sealed PKCS#12 (password TEST)"),
    S("certkey-bundle.pfx", "certkey", pkcs12_file, shows="RESTRICTED: sealed PKCS#12 (password TEST)"),
    S("vpptoken-test.vpptoken", "vpptoken", vpptoken_file, hand_built=True,
      shows="RESTRICTED: org name + expiry only, never the token"),
    # disk images (hand-built headers)
    S("disk-image-volume.iso", "disk_image", iso_file, hand_built=True, shows="ISO 9660 facts (label CONSTRUCTICON_SAMPLE)"),
    S("disk-image-raw.img", "disk_image", raw_mbr_image, hand_built=True, shows="raw image, MBR with one partition"),
    S("disk-image-master.cdr", "disk_image", raw_mbr_image, hand_built=True, shows="raw image (accepted by extension)"),
    S("disk-image-sparse.sparseimage", "disk_image", sparseimage_file, hand_built=True, shows="sparse image facts"),
    S("disk-image-hyperv.vhdx", "disk_image", vhdx_file, hand_built=True, shows="VHDX facts"),
    S("disk-image-qemu.qcow2", "disk_image", qcow2_file, hand_built=True, shows="qcow2 facts (1 MiB virtual)"),
    S("disk-image-vmware.vmdk", "disk_image", vmdk_file, hand_built=True, shows="VMDK descriptor facts"),
    S("disk-image-virtualpc.vhd", "disk_image", vhd_file, hand_built=True, shows="fixed VHD facts"),
    S("disk-image-mac.dmg", "disk_image", dmg_file, executable=True, hand_built=True, shows="UDIF trailer facts"),
    # firmware
    S("firmware-board.hex", "firmware", intel_hex, hand_built=True, shows="Intel HEX ranges and checksums"),
    S("firmware-eeprom.eep", "firmware", intel_hex, hand_built=True, shows="Intel HEX (AVR EEPROM)"),
    S("firmware-arm.elf", "firmware", elf_arm, executable=True, hand_built=True, shows="ELF facts (ARM, 32-bit)"),
    S("firmware-arm.bin", "firmware", elf_arm, executable=True, hand_built=True, shows="ELF facts (ARM, 32-bit)"),
    # executables / installers / bundles: Linux only
    S("application-tool.exe", "application", pe_application, executable=True, hand_built=True,
      shows='PE facts; queues "installer or standalone app?" in Needs your input'),
    S("installer-setup-nsis.exe", "installer", pe_installer_nsis, executable=True, hand_built=True,
      shows="claimed as an installer (NSIS fingerprint), no question asked"),
    S("installer-package.msi", "installer", msi_file, executable=True, hand_built=True, shows="Windows Installer recognised"),
    S("installer-package.pkg", "installer", pkg_file, executable=True, hand_built=True, shows="macOS package: title + product id"),
    S("installer-meta.mpkg", "installer", mpkg_file, executable=True, hand_built=True, shows="macOS metapackage"),
    S("installer-archive.xip", "installer", xip_file, executable=True, hand_built=True, shows="XIP: 2 components"),
    S("installer-package.deb", "installer", deb_file, executable=True, hand_built=True, shows="Debian control fields"),
    S("installer-package.rpm", "installer", rpm_file, executable=True, hand_built=True, shows="RPM name/version/arch"),
    S("installer-package.msix", "installer", appx_file, executable=True, hand_built=True, shows="MSIX identity"),
    S("installer-package.appx", "installer", appx_file, executable=True, hand_built=True, shows="APPX identity"),
    S("installer-bundle.msixbundle", "installer", appx_bundle_file, executable=True, hand_built=True,
      shows="MSIX bundle: 2 architectures"),
    S("installer-bundle.appxbundle", "installer", appx_bundle_file, executable=True, hand_built=True,
      shows="APPX bundle: 2 architectures"),
    S("macos-app-sampler.zip", "macos_app", macos_app_zip, executable=True, thumbnail=True, hand_built=True,
      shows="app facts from Info.plist; icon (.icns) as the thumbnail"),
]


@dataclass
class Content:
    """An item with no file, made through /api/content."""
    name: str
    media_type: str
    fields: dict = field(default_factory=dict)
    thumbnail: object = None   # True / False / "network"
    text: object = None
    shows: str = ""


CONTENT = [
    Content("youtube-link", "youtube", {"external_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
            thumbnail="network", text="network", shows="embedded player; thumbnail fetched from YouTube (needs network)"),
    Content("web-page-link", "url", {"external_url": "https://example.com/constructicon-type-sampler"},
            thumbnail=False, text=False, shows="link card with the URL as its title (no capture yet, so OCR reads failed, #724)"),
    Content("written-post", "document", {
        "media_type": "document", "content_description": "Constructicon type sampler written post (#720)",
        "type_metadata": json.dumps({"body": f"# {MARK}: written post\n\nA post made by the drop test."})},
        thumbnail=False, text=False, shows="rendered Markdown body"),
]


# ============================================================================ expectations (from the registry)

def _ot():
    from core import object_types
    return object_types


def disposition_for(filename):
    """How /f/<slug> should serve this file (#610/#701): HTML is always a download; SVG inline (sandboxed);
    everything else inline when the browser shows it itself, else a download."""
    ot = _ot()
    ext = ot.file_extension(filename)
    if ext in (".html", ".htm", ".xhtml"):
        return "attachment"
    if ext == ".svg":
        return "inline"
    return "inline" if ot.opens_in_browser(filename) else "attachment"


def expectations(sample):
    ot = _ot()
    spec = ot.OBJECT_TYPES[sample.media_type]
    src = spec.thumbnail_source
    if sample.thumbnail is not None:
        thumb = sample.thumbnail
    elif spec.has_thumbnail_fn is not None:
        raise ValueError(f"{sample.file}: type {spec.key} decides thumbnails per file; set Sample.thumbnail")
    else:
        thumb = src == ot.ThumbnailSource.UPLOADED_FILE or (src == ot.ThumbnailSource.CAPTURE and spec.capture_fn is not None)
    return {
        "file": sample.file,
        "media_type": sample.media_type,
        "type_label": spec.label,
        "restricted": bool(spec.restricted),
        "thumbnail": bool(thumb) and not spec.restricted,
        "text": spec.ocr_capable if sample.text is None else bool(sample.text),
        "date": sample.date,
        "disposition": sample.disposition or disposition_for(sample.file),
        "raw": "text" if spec.raw_text_fn is not None else "download",
        "executable": sample.executable,
        "hand_built": sample.hand_built,
        "shows": sample.shows,
    }


def content_expectations(c):
    spec = _ot().OBJECT_TYPES[c.media_type]
    fields = dict(c.fields)
    fields.setdefault("media_type", c.media_type)
    return {"name": c.name, "media_type": c.media_type, "type_label": spec.label, "restricted": bool(spec.restricted),
            "thumbnail": c.thumbnail, "text": c.text, "fields": fields, "shows": c.shows}


# ============================================================================ README

def _yn(v):
    return {True: "yes", False: "no", "network": "with network"}.get(v, str(v))


def readme(files, content, skipped, varying):
    lines = [
        "# Constructicon type sampler (#720)",
        "",
        "One file of every object type Constructicon knows, generated by `scripts/make_type_samples.py` "
        "(nothing here comes from the archive). Drop the whole folder on a new install, or let "
        "`scripts/drop_test.py --base-url URL --samples THIS_FOLDER` upload it through the API, check each "
        "item and delete what it made.",
        "",
        "**Windows / Defender:** the executable-class samples (PE `.exe` files, installer packages, the "
        "zipped macOS `.app`, ELF firmware, the `.dmg`) are generated only on Linux (in the app "
        "container) and are never committed. Copying this folder to a Windows machine can make Defender "
        "cloud-scan or quarantine them; they are harmless (a PE whose entry point is a bare `ret`, "
        "installer shells that install nothing), but keep the folder on the NAS / in the container.",
        "",
        "**Credentials are fake:** the certificate, key, CSR, PKCS#12 (password `TEST`) and VPP token are "
        "labelled TEST / NOT REAL and protect nothing (the key comes from a fixed public seed).",
        "",
        "Columns: **type** = the media type the upload should land as; **thumb** = a thumbnail at "
        "`/f/<slug>/thumb`; **text** = extracted/OCR text; **/f/** = how the file is served (#701: "
        "`inline` = opens in the browser, `attachment` = a download); **raw** = `/f/<slug>/raw` (#703); "
        "**restricted** = admin only, hidden from viewers. *hand-built* = no writer in the app image, so "
        "the smallest file the type's sniffer and parser accept was written by hand.",
        "",
        "| file | type | thumb | text | /f/ | raw | restricted | a correct drop test shows |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for f in files:
        note = f["shows"] + (" *(hand-built)*" if f["hand_built"] else "") + (" **[executable: Linux only]**" if f["executable"] else "")
        lines.append(f"| `{f['file']}` | {f['media_type']} | {_yn(f['thumbnail'])} | {_yn(f['text'])} | {f['disposition']} "
                     f"| {f['raw']} | {'**yes**' if f['restricted'] else 'no'} | {note} |")
    lines += ["", "Items with no file (`content.json`, also listed in `urls.txt`), created through `/api/content`:", "",
              "| item | type | thumb | text | shows |", "|---|---|---|---|---|"]
    for c in content:
        lines.append(f"| {c['name']} | {c['media_type']} | {_yn(c['thumbnail'])} | {_yn(c['text'])} | {c['shows']} |")
    if skipped:
        lines += ["", f"**Not generated here** (executables are Linux-only): {', '.join(f'`{s}`' for s in skipped)}."]
    lines += ["", f"Byte-identical on every run except: {', '.join(f'`{v}`' for v in varying)}.", ""]
    return "\n".join(lines)


# Not byte-identical between runs: PKCS#12 encryption uses a random salt; py7zr stamps the write time.
VARYING = ["certkey-bundle.p12", "certkey-bundle.pfx", "archive-bundle.7z"]


# ============================================================================ main

def generate(outdir, *, executables=True, quiet=False):
    """Write every sample into `outdir`. Returns (manifest dict, skipped file names)."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    can_exec = executables and CAN_WRITE_EXECUTABLES
    files, skipped = [], []
    for s in SAMPLES:
        exp = expectations(s)
        if s.executable and not can_exec:
            skipped.append(s.file)
            continue
        data = s.build(s.file)
        (outdir / s.file).write_bytes(data)
        exp["bytes"] = len(data)
        exp["sha256"] = hashlib.sha256(data).hexdigest()
        files.append(exp)
        if not quiet:
            print(f"  wrote {s.file:34} {len(data):>8} bytes  -> {s.media_type}")
    content = [content_expectations(c) for c in CONTENT]
    manifest = {"generator": "scripts/make_type_samples.py (#720)", "marker": MARK, "files": files,
                "content": content, "skipped_executables": skipped}
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (outdir / "content.json").write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    (outdir / "urls.txt").write_text(
        "# URL-only types (no file): the media type each should land as, then the link.\n"
        "# scripts/drop_test.py creates these (and the written post in content.json) via /api/content.\n"
        + "".join(f"{c['media_type']}\t{c['fields']['external_url']}\n" for c in content if c["fields"].get("external_url")),
        encoding="utf-8")
    (outdir / "README.md").write_text(readme(files, content, skipped, VARYING), encoding="utf-8")
    return manifest, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("outdir", nargs="?", help="where to write the samples (e.g. samples-out)")
    ap.add_argument("--no-executables", action="store_true", help="skip the executable-class samples even on Linux")
    ap.add_argument("--list", action="store_true", help="print the manifest (file -> type) and write nothing")
    args = ap.parse_args()
    if args.list:
        for s in SAMPLES:
            print(f"{s.file:34} {s.media_type:12}{'  [executable]' if s.executable else ''}")
        for c in CONTENT:
            print(f"{c.name:34} {c.media_type:12}  [no file: /api/content]")
        return
    if not args.outdir:
        ap.error("OUTDIR is required (or --list)")
    if not CAN_WRITE_EXECUTABLES and not args.no_executables:
        print(f"note: {sys.platform} is not Linux, so the executable-class samples are SKIPPED "
              "(owner rule: build them only in the Linux container; Defender cloud-scans synthetic PE files)")
    manifest, skipped = generate(args.outdir, executables=not args.no_executables)
    types = sorted({f["media_type"] for f in manifest["files"]} | {c["media_type"] for c in manifest["content"]})
    print(f"OK: wrote {len(manifest['files'])} sample files + {len(manifest['content'])} content items "
          f"({len(types)} types) to {args.outdir}" + (f"; skipped {len(skipped)} executables" if skipped else ""))


if __name__ == "__main__":
    main()
