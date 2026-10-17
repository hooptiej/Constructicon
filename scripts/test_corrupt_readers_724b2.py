#!/usr/bin/env python3
"""#724 (b2): every text reader reports a corrupt or unreadable file instead of settling "done, no text".

Owner decision 2026-10-17: "We should say they are corrupt along the 'never fail silently' rule".

Throwaway DB + storage (scripts/_testenv.py), no server:

    python scripts/test_corrupt_readers_724b2.py

The fixtures are tiny synthetic files built here, in memory (nothing binary is committed): a truncated
zip, a garbage tar and a tar.gz cut off partway, a garbage .7z, a broken and an encrypted PDF, a
malformed SVG, an oversize .eml, a truncated .xlsx, a .docx whose text is broken but whose thumbnail is
fine, and a disk read error (stood in) for each plain-text reader.

Covers:
  * each reader raises UnreadableFile ("corrupt or unreadable .<ext>: <short error>", the server path cut
    to the file's name) or NotCompatibleFile (encrypted PDF -> grey), never "" for a broken file
  * ocr.run_ocr settles each one 'failed' with that reason in type_metadata.ocr_note (or 'unsupported')
  * valid but empty files still settle 'done' with no text and no note (empty zip, blank PDF, SVG with no
    <text>), and malformed-but-readable text (bad JSON, ragged CSV) is still text, 'done'
  * oversize stays "too large to read .<ext>: ..."
  * the picture + bad text case: a .docx whose document.xml is broken but whose thumbnail is fine settles
    'done' with the OCR text AND the reader's reason in ocr_note ("...; OCR read the picture instead")
  * a tar.gz that breaks partway keeps the names read before the error, with the warning as the note
  * reader_note() for a parse limit and for a reader bug; db.set_ocr_status keeps a note on 'done'
  * the drawer, both lamps and the object page show a 'done' row's warning

Exits 1 if any check fails.
"""

import gzip
import io
import json
import os
import random
import re
import sqlite3
import sys
import tarfile
import types
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("corrupt-readers-724b2-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
    HAVE_CAIRO = True
except Exception:  # no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")
    HAVE_CAIRO = False

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from core import actor, db, ingest, object_types, ocr, parse_limits, paths  # noqa: E402
from core.object_types import archive, code, data, emailmsg, pdf, spreadsheet, svg, text, word  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def state(slug):
    r = db.get_by_slug(slug)
    return r["ocr_status"], (r.get("type_metadata") or {}).get("ocr_note"), r["extracted_text"]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


try:
    import pytesseract
    pytesseract.get_tesseract_version()
    HAVE_TESSERACT = True
except Exception as e:  # silent-ok: a test-environment probe, reported as SKIP lines below
    print(f"SKIP OCR-of-a-picture checks (no tesseract here: {e!r})")
    HAVE_TESSERACT = False

try:
    from web.routes import items as web_items
except Exception as e:  # web stack not importable here: the box run covers it
    web_items = None
    print(f"SKIP drawer checks (web import failed: {e!r})")

db.init_db()
ingest.run_in_thread = lambda fn, *a: None  # nothing runs OCR behind the test's back
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()


def mkfile(slug, payload, ext, media_type, type_metadata=None):
    """A row whose stored file holds `payload`."""
    sf = f"{slug}{ext}"
    (paths.storage_dir() / sf).write_bytes(payload)
    db.insert_upload(slug, f"{slug}{ext}", sf, "tester", media_type=media_type, file_size=len(payload),
                     ocr_status="pending", type_metadata=type_metadata)
    return slug


def raised(fn, *args):
    """(exception class name, message) of what fn raised, or ("returned", value)."""
    try:
        return "returned", fn(*args)
    except Exception as e:
        return type(e).__name__, str(e)


def row(slug):
    return db.get_by_slug(slug)


CORRUPT = "corrupt or unreadable ."


# ============================================================================================
# fixtures
# ============================================================================================
def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


GOOD_ZIP = zip_bytes({"rover/readme.txt": "wheels", "rover/motor.c": "int main(){}"})
TRUNC_ZIP = GOOD_ZIP[:len(GOOD_ZIP) // 2]   # the central directory is gone
EMPTY_ZIP = zip_bytes({})                    # 22 bytes: a valid archive with nothing in it


def tar_gz_cut():
    """A .tar.gz with a small member and a big incompressible one, cut off inside the big one's data:
    both headers are readable, then the stream ends early."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, content in (("notes.txt", b"rover notes"),
                              ("blob.bin", random.Random(724).randbytes(200_000))):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    gz = gzip.compress(raw.getvalue(), mtime=0)
    return gz[:int(len(gz) * 0.6)]


def pdf_bytes(text_on_page=None, encrypt=False):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    if text_on_page:
        page.insert_text((72, 72), text_on_page)
    if encrypt:
        out = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner-724", user_pw="user-724")
    else:
        out = doc.tobytes()
    doc.close()
    return out


def picture_png(words):
    img = Image.new("RGB", (900, 260), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=96)
    except TypeError:  # silent-ok: an older Pillow has no sized default font; the bitmap one is fine here
        font = ImageFont.load_default()
    d.text((30, 70), words, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def docx_with_thumbnail(document_xml, thumb_png):
    return zip_bytes({
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "word/document.xml": document_xml,
        "docProps/thumbnail.png": thumb_png,
    })


BROKEN_DOCUMENT_XML = '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p>'
EML = (b"From: rover@example.com\r\nTo: owner@example.com\r\nSubject: Wheel order\r\n\r\n"
       b"Six wheels, please.\r\n")


# ============================================================================================
# 1. each reader, called directly: raises with the reason, never "" for a broken file
# ============================================================================================
plain_cases = [("text", ".txt"), ("code", ".py"), ("data", ".csv"), ("markdown", ".md")]
# A plain-text reader replaces odd bytes (they are still text), so the only way it fails is the disk
# not giving the bytes back. Stand that in with an I/O error from the one function every plain-text
# reader reads through, in-process (CONSTRUCTICON_PARSE_SANDBOX=off: no parser child to miss the patch).
from core.object_types import _textstats  # noqa: E402
_real_read_text = _textstats.read_text


def _eio(path, max_chars):
    raise OSError(5, "Input/output error", str(path))


class disk_error:
    def __enter__(self):
        os.environ["CONSTRUCTICON_PARSE_SANDBOX"] = "off"
        _textstats.read_text = _eio

    def __exit__(self, *exc):
        _textstats.read_text = _real_read_text
        os.environ.pop("CONSTRUCTICON_PARSE_SANDBOX", None)


for media_type, ext in plain_cases:
    s = mkfile(f"eio_{media_type}", b"wheels and motors\n", ext, media_type)
    spec = object_types.get_object_type(media_type)
    with disk_error():
        kind, msg = raised(spec.text_extract_fn, row(s))
    check(f"{media_type} reader: a file it can't read raises UnreadableFile, not \"\"",
          kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}{ext[1:]}: ") and "Input/output error" in msg,
          (kind, msg))
    check(f"{media_type} reader: the reason names the file, not the server path",
          str(paths.storage_dir()) not in msg and f"eio_{media_type}{ext}" in msg, msg)
    check(f"{media_type} reader: the same file reads fine when the disk does",
          raised(spec.text_extract_fn, row(s)) == ("returned", "wheels and motors"))

kind, msg = raised(archive.extract_text_for_row, row(mkfile("u_trunc_zip", TRUNC_ZIP, ".zip", "archive")))
check("archive reader: a truncated zip raises 'corrupt or unreadable .zip: ...'",
      kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}zip: "), (kind, msg))
kind, msg = raised(archive.extract_text_for_row, row(mkfile("u_bad_tar", b"not a tar archive at all" * 40, ".tar", "archive")))
check("archive reader: a .tar that isn't one raises 'corrupt or unreadable .tar: ...'",
      kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}tar: "), (kind, msg))
try:
    exc = None
    archive.extract_text_for_row(row(mkfile("u_cut_tgz", tar_gz_cut(), ".tar.gz", "archive")))
except object_types.UnreadableFile as e:
    exc = e
check("archive reader: a .tar.gz cut off partway raises, naming .tar.gz",
      exc is not None and str(exc).startswith(f"{CORRUPT}tar.gz: "), exc)
check("archive reader: ...and keeps the names read before the break as partial_text",
      exc is not None and (exc.partial_text or "").split("\n")[:1] == ["notes.txt"], getattr(exc, "partial_text", None))
kind, msg = raised(archive.extract_text_for_row, row(mkfile("u_bad_7z", b"7z? no, just bytes" * 20, ".7z", "archive")))
if archive.py7zr is None:
    check("archive reader: no 7z reader here -> NotCompatibleFile, saying so",
          kind == "NotCompatibleFile" and "7z reader" in msg, (kind, msg))
else:
    check("archive reader: a garbage .7z raises 'corrupt or unreadable .7z: ...'",
          kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}7z: "), (kind, msg))
check("archive reader: an empty zip is read, and empty (\"\", not an error)",
      raised(archive.extract_text_for_row, row(mkfile("u_empty_zip", EMPTY_ZIP, ".zip", "archive"))) == ("returned", ""))

HAVE_FITZ = True
try:
    import fitz  # noqa: F401
except Exception as e:  # silent-ok: a test-environment probe, reported as a SKIP line
    HAVE_FITZ = False
    print(f"SKIP PDF checks (no PyMuPDF here: {e!r})")
if HAVE_FITZ:
    kind, msg = raised(pdf.extract_text_for_row, row(mkfile("u_bad_pdf", b"%PDF-1.4\n" + b"\x00garbage" * 50, ".pdf", "pdf")))
    check("pdf reader: a broken PDF raises 'corrupt or unreadable .pdf: ...'",
          kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}pdf: "), (kind, msg))
    kind, msg = raised(pdf.extract_text_for_row, row(mkfile("u_enc_pdf", pdf_bytes("secret", encrypt=True), ".pdf", "pdf")))
    check("pdf reader: an encrypted PDF is NotCompatibleFile with the owner's wording",
          kind == "NotCompatibleFile" and msg == object_types.ENCRYPTED_NOTE, (kind, msg))
    check("pdf reader: a valid PDF with text returns it",
          raised(pdf.extract_text_for_row, row(mkfile("u_ok_pdf", pdf_bytes("Rover manual"), ".pdf", "pdf")))
          == ("returned", "Rover manual"))
    check("pdf reader: a blank page is read, and empty (\"\": OCR's turn)",
          raised(pdf.extract_text_for_row, row(mkfile("u_blank_pdf", pdf_bytes(), ".pdf", "pdf"))) == ("returned", ""))
    check("pdf.extract_text (best-effort helper) still answers \"\" for a broken PDF",
          pdf.extract_text(paths.storage_dir() / "u_bad_pdf.pdf") == "")
    kind, msg = raised(object_types.get_object_type("ai").text_extract_fn,
                       row(mkfile("u_bad_ai", b"%PDF-1.5\n" + b"\x00broken" * 40, ".ai", "ai")))
    check("ai reader: a PDF-compatible .ai that won't open raises 'corrupt or unreadable .ai: ...'",
          kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}ai: "), (kind, msg))
    check("ai reader: a legacy PostScript .ai has no text layer (\"\", OCR of the render decides)",
          raised(object_types.get_object_type("ai").text_extract_fn,
                 row(mkfile("u_ps_ai", b"%!PS-Adobe-3.0\n%%EOF\n", ".ai", "ai"))) == ("returned", ""))

kind, msg = raised(svg.extract_text_for_row, row(mkfile("u_bad_svg", b'<svg xmlns="http://www.w3.org/2000/svg"><text>hi</svg', ".svg", "svg")))
check("svg reader: a malformed SVG raises 'corrupt or unreadable .svg: <the XML error>'",
      kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}svg: ") and "line 1" in msg, (kind, msg))
check("svg reader: a valid SVG with no <text> is read, and empty",
      raised(svg.extract_text_for_row, row(mkfile("u_plain_svg", b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="5" height="5"/></svg>', ".svg", "svg")))
      == ("returned", ""))

S_EML_BIG = mkfile("u_big_eml", EML, ".eml", "email")
_cap = emailmsg.MAX_FILE_BYTES
emailmsg.MAX_FILE_BYTES = 10
try:
    kind, msg = raised(emailmsg.extract_text_for_row, row(S_EML_BIG))
finally:
    emailmsg.MAX_FILE_BYTES = _cap
check("email reader: an oversize message raises 'too large to read .eml: ...'",
      kind == "UnreadableFile" and msg.startswith("too large to read .eml: "), (kind, msg))
check("email reader: a normal message still reads",
      "Six wheels" in (raised(emailmsg.extract_text_for_row, row(mkfile("u_ok_eml", EML, ".eml", "email")))[1] or ""))

try:
    import openpyxl  # noqa: F401
    HAVE_OPENPYXL = True
except Exception as e:  # silent-ok: a test-environment probe, reported as a SKIP line
    HAVE_OPENPYXL = False
    print(f"SKIP .xlsx check (no openpyxl here: {e!r})")
if HAVE_OPENPYXL:
    kind, msg = raised(spreadsheet.extract_text_for_row,
                       row(mkfile("u_trunc_xlsx", zip_bytes({"xl/workbook.xml": "<workbook/>" * 50})[:120], ".xlsx", "spreadsheet")))
    check("spreadsheet reader: a truncated .xlsx raises 'corrupt or unreadable .xlsx: ...'",
          kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}xlsx: "), (kind, msg))

kind, msg = raised(word.extract_text_for_row,
                   row(mkfile("u_bad_docx", docx_with_thumbnail(BROKEN_DOCUMENT_XML, picture_png("ROVER")), ".docx", "word")))
check("word reader: a .docx whose document.xml is broken raises (it never reads as \"no text\")",
      kind == "UnreadableFile" and msg.startswith(f"{CORRUPT}docx: "), (kind, msg))

# the shared helpers
check("file_ext: a double extension is kept whole", object_types.file_ext("/x/a.TAR.GZ") == "tar.gz")
check("file_ext: no extension reads 'file'", object_types.file_ext("/x/README") == "file")
long_err = ValueError("x" * 500)
check("short_error: cut to a human size", len(object_types.short_error(long_err)) == object_types.MAX_REASON_DETAIL_CHARS)
check("unreadable: TooLarge says 'too large to read'",
      str(object_types.unreadable("/s/a.eml", object_types.TooLarge("big"))) == "too large to read .eml: big")
pickled = __import__("pickle").loads(__import__("pickle").dumps(object_types.UnreadableFile("why", partial_text="kept")))
check("UnreadableFile keeps partial_text across a pickle (the parser child's way back)",
      str(pickled) == "why" and pickled.partial_text == "kept", vars(pickled))

# ============================================================================================
# 2. ocr.run_ocr: the status and the reason each one settles with
# ============================================================================================
for media_type, ext in plain_cases:
    s = mkfile(f"run_eio_{media_type}", b"wheels and motors\n", ext, media_type)
    with disk_error():
        ocr.run_ocr(s)
    st = state(s)
    check(f"run_ocr {s}: a read error is 'failed', saying why (was 'done', no text)",
          st[0] == "failed" and (st[1] or "").startswith(f"{CORRUPT}{ext[1:]}: ") and not st[2], st)

FAILED_CASES = {}  # slug -> expected reason prefix
FAILED_CASES[mkfile("run_trunc_zip", TRUNC_ZIP, ".zip", "archive")] = f"{CORRUPT}zip: "
FAILED_CASES[mkfile("run_bad_tar", b"not a tar archive at all" * 40, ".tar", "archive")] = f"{CORRUPT}tar: "
if archive.py7zr is not None:
    FAILED_CASES[mkfile("run_bad_7z", b"7z? no, just bytes" * 20, ".7z", "archive")] = f"{CORRUPT}7z: "
if HAVE_FITZ:
    FAILED_CASES[mkfile("run_bad_pdf", b"%PDF-1.4\n" + b"\x00garbage" * 50, ".pdf", "pdf")] = f"{CORRUPT}pdf: "
FAILED_CASES[mkfile("run_bad_svg", b'<svg xmlns="http://www.w3.org/2000/svg"><text>hi</svg', ".svg", "svg")] = f"{CORRUPT}svg: "
if HAVE_OPENPYXL:
    FAILED_CASES[mkfile("run_trunc_xlsx", zip_bytes({"xl/workbook.xml": "<workbook/>" * 50})[:120], ".xlsx", "spreadsheet")] = f"{CORRUPT}xlsx: "
for s in FAILED_CASES:
    ocr.run_ocr(s)
for s, prefix in FAILED_CASES.items():
    st = state(s)
    check(f"run_ocr {s}: 'failed', saying why ({prefix}...)",
          st[0] == "failed" and (st[1] or "").startswith(prefix), st)

if HAVE_FITZ:
    S_ENC = mkfile("run_enc_pdf", pdf_bytes("secret", encrypt=True), ".pdf", "pdf")
    ocr.run_ocr(S_ENC)
    check("run_ocr: an encrypted PDF is grey 'unsupported', \"encrypted: ...\" (not red)",
          state(S_ENC)[:2] == ("unsupported", object_types.ENCRYPTED_NOTE), state(S_ENC))

# oversize, through the pipeline (in-process: the lowered cap lives in this process only)
S_EML_RUN = mkfile("run_big_eml", EML, ".eml", "email")
os.environ["CONSTRUCTICON_PARSE_SANDBOX"] = "off"
emailmsg.MAX_FILE_BYTES = 10
try:
    ocr.run_ocr(S_EML_RUN)
finally:
    emailmsg.MAX_FILE_BYTES = _cap
    os.environ.pop("CONSTRUCTICON_PARSE_SANDBOX", None)
check("run_ocr: an oversize .eml settles 'failed', \"too large to read .eml: ...\"",
      state(S_EML_RUN)[0] == "failed" and (state(S_EML_RUN)[1] or "").startswith("too large to read .eml: "), state(S_EML_RUN))

# valid but empty, and malformed-but-readable text: done, no note
DONE_EMPTY = [mkfile("run_empty_zip", EMPTY_ZIP, ".zip", "archive"),
              mkfile("run_blank_txt", b"   \n", ".txt", "text")]
DONE_TEXT = {mkfile("run_bad_json", b'{"wheels": 6, "motors": ', ".json", "code"): '"wheels": 6',
             mkfile("run_ragged_csv", b'a,b,c\n1,"unclosed\n2,3\n', ".csv", "data"): "unclosed"}
for s in DONE_EMPTY + list(DONE_TEXT):
    ocr.run_ocr(s)
for s in DONE_EMPTY:
    check(f"run_ocr {s}: valid and empty stays 'done', no text, no note", state(s) == ("done", None, ""), state(s))
for s, needle in DONE_TEXT.items():
    check(f"run_ocr {s}: malformed but readable text is still text ('done', no note)",
          state(s)[:2] == ("done", None) and needle in (state(s)[2] or ""), state(s))

# a tar.gz that broke partway: the names read so far are its text, and the warning stays
S_CUT = mkfile("run_cut_tgz", tar_gz_cut(), ".tar.gz", "archive")
ocr.run_ocr(S_CUT)
st = state(S_CUT)
check("run_ocr: a tar.gz cut partway settles 'done' with the names read before the break",
      st[0] == "done" and "notes.txt" in (st[2] or ""), st)
check("run_ocr: ...and says it broke (ocr_note keeps the reason)",
      (st[1] or "").startswith(f"{CORRUPT}tar.gz: ") and st[1].endswith("showing the text read before the error"), st)

# the picture + bad text case: OCR's text is kept, and so is the warning
if HAVE_TESSERACT:
    S_PIC = mkfile("run_bad_docx_pic", docx_with_thumbnail(BROKEN_DOCUMENT_XML, picture_png("ROVER")), ".docx", "word",
                   type_metadata={word.STATS_KEY: {"has_thumbnail": True}})
    ocr.run_ocr(S_PIC)
    st = state(S_PIC)
    check("picture + bad text: settles 'done' (OCR read the thumbnail)", st[0] == "done", st)
    check("picture + bad text: the OCR text is kept", "ROVER" in (st[2] or "").upper(), st)
    check("picture + bad text: the reader's reason is kept in ocr_note, saying OCR stood in",
          (st[1] or "").startswith(f"{CORRUPT}docx: ") and st[1].endswith("OCR read the picture instead"), st)
    check("picture + bad text: no text_source (the text came from OCR, not the document)",
          "text_source" not in (row(S_PIC).get("type_metadata") or {}), row(S_PIC).get("type_metadata"))
    # a later clean run clears the warning
    db.set_ocr_status(S_PIC, "pending")
    check("retry: going back to pending clears the warning", state(S_PIC)[1] is None, state(S_PIC))
else:
    print("SKIP picture + bad text checks (no tesseract)")

# ============================================================================================
# 3. reader_note, set_ocr_status
# ============================================================================================
r = {"stored_filename": "abc.pdf"}
check("reader_note: an UnreadableFile is its own reason",
      ocr.reader_note(object_types.UnreadableFile("corrupt or unreadable .pdf: x"), r) == "corrupt or unreadable .pdf: x")
check("reader_note: a time limit says which",
      ocr.reader_note(parse_limits.LimitExceeded("time", "text extraction", "took longer than 30 s, so it was stopped"), r)
      == "couldn't read .pdf: took longer than 30 s, so it was stopped")
check("reader_note: 'busy' says try again (it isn't retried by itself)",
      "try OCR again" in ocr.reader_note(parse_limits.LimitExceeded("busy", "text extraction", "busy"), r))
check("reader_note: a reader bug says what happened without calling the file corrupt",
      ocr.reader_note(KeyError("w:body"), r) == "couldn't read .pdf: 'w:body'")

S_N = mkfile("note_row", b"hello", ".txt", "text")
db.set_ocr_status(S_N, "done", note="corrupt or unreadable .docx: x; OCR read the picture instead")
check("db: 'done' keeps a warning note", state(S_N)[:2] == ("done", "corrupt or unreadable .docx: x; OCR read the picture instead"))
db.set_ocr_status(S_N, "done")
check("db: 'done' with no note clears it", state(S_N)[1] is None)

# ============================================================================================
# 4. where the warning shows: drawer, both lamps, the object page
# ============================================================================================
if web_items is not None:
    db.set_ocr_status(S_N, "done", note="corrupt or unreadable .docx: x; OCR read the picture instead")
    st = web_items._derive_processing_status(db.get_processing_rows_by_slugs([S_N])[0])
    ocr_stage = {s["stage"]: s for s in st["stages"]}["OCR"]
    check("drawer: a done row with a warning stays 'done' and carries the note",
          ocr_stage["state"] == "done" and ocr_stage.get("note", "").startswith("corrupt or unreadable .docx"), ocr_stage)
    check("drawer: still settled (not in flight)", not st["in_flight"], st)
drawer = read("web/templates/_processing_drawer.html")
check("drawer template: each state words its note its own way (failed is not 'Not compatible')",
      "Not compatible with OCR: " in drawer and "OCR failed: " in drawer and "Read with a warning: " in drawer)
cards_js = read("web/static/js/cards.js")
check("cards.js: a done lamp with a note shows the warning in its tooltip",
      re.search(r"ocr_status === 'done' && note", cards_js) is not None and "Read with a warning" in cards_js)
detail = read("web/templates/object_detail.html")
check("object page: a done lamp with a note says 'with a warning'",
      "ocrStatus === 'done' && ocrNote" in detail and "with a warning" in detail)

# ============================================================================================
ctx.__exit__(None, None, None)
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
