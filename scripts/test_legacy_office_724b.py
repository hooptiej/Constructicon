#!/usr/bin/env python3
"""#724 part b: text out of legacy Word (.doc) and PowerPoint (.ppt), olefile only.

Throwaway DB + storage (scripts/_testenv.py), no server, no network:

    python scripts/test_legacy_office_724b.py

Fixtures (scripts/data/legacy_office_724b/, small and synthetic, no personal data): a Word 97 .doc
and a PowerPoint 97 .ppt made by LibreOffice 7.4 headless (the .ppt's two streams repacked into a
small container to drop LibreOffice's 588 KB preview picture), and an Excel 97 .xls from a CSV.
Other cases are built here (a Word 97 piece table with compressed and Unicode pieces and nested
fields, a Word 6 FIB, broken files).

Covers:
  * word.doc_text: every paragraph, cp1252 and beyond-cp1252 text, a hyperlink field's words kept
    and its code dropped, table cells; compressed + Unicode pieces, a Prc before the Pcdt, nested
    fields, control characters
  * powerpoint.ppt_text: the slides in order then the notes; the masters' "Click to edit ..." and
    "... Outline Level" placeholders never appear
  * None (-> 'unsupported') for Word 6/95, an unrecognised WordDocument, a password-protected .doc,
    a deck with no Current User stream, PowerPoint 95, a protected deck
  * raises (-> 'failed') for a truncated file, an OLE header with nothing behind it, a piece table
    or a persist offset pointing outside the file
  * ocr.run_ocr: text lands in extracted_text (searchable), type_metadata.text_source = 'office',
    no ocr_note; Word 6 settles 'unsupported' with the note; a broken file settles 'failed'
  * parse limits (#644): the extraction runs through parse_limits.call (background budget), a real
    parser child returns the same text (Linux), a stream over the size cap is refused, the output cap
  * db.set_extracted_text records / clears text_source, also on a row with empty type_metadata
  * the .xls reader writes whole numbers without ".0"

Exits 1 if any check fails.
"""

import os
import shutil
import sqlite3
import struct
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("legacy-office-724b-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
FIX = os.path.join(HERE, "data", "legacy_office_724b")

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

import olefile  # noqa: E402

import make_type_samples  # noqa: E402  (scripts/: its hand-built OLE writer)
from core import actor, db, ingest, ocr, parse_limits, paths  # noqa: E402
from core.object_types import _office, powerpoint, spreadsheet, word  # noqa: E402
_testenv.assert_isolated()

FAILS = []
sys.stdout.reconfigure(errors="backslashreplace")  # a Windows console can't print every fixture character


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def outcome(fn, *args, **kw):
    """('text', value) / ('none', None) / ('raised', ExceptionName)."""
    try:
        out = fn(*args, **kw)
    except Exception as e:  # noqa: BLE001 - the check reads it
        return "raised", type(e).__name__
    return ("none", None) if out is None else ("text", out)


def write(name, data):
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


DOC = os.path.join(FIX, "synthetic-word97.doc")
PPT = os.path.join(FIX, "synthetic-ppt97.ppt")
XLS = os.path.join(FIX, "synthetic-numbers.xls")
DOC_NOTE = "legacy .doc: older Word format or password-protected, no readable text"


def streams(path):
    with olefile.OleFileIO(path) as o:
        return {"/".join(e): o.openstream("/".join(e)).read() for e in o.listdir()}


# ============================================================================================
# 1. the .doc reader on a real Word 97 file
# ============================================================================================
kind, text = outcome(word.doc_text, DOC)
check("doc: reads text", kind == "text" and len(text) > 200, (kind, text))
text = text or ""
for want in ("Synthetic Constructicon fixture (#724)",
             "First paragraph: plain text about a test rover with six wheels.",
             "“quotes” — a dash and café",      # cp1252 punctuation
             "Ωmega → arrow ✓ done.",                  # beyond cp1252
             "A link: the rover page ends here.",                    # field result kept
             "Cell A1\tCell B1", "Cell A2\tCell B2", "Last paragraph of the fixture."):
    check(f"doc: has {want[:40]!r}", want in text, text)
check("doc: the hyperlink's field code is dropped", "HYPERLINK" not in text and "example.com" not in text, text)
check("doc: no control characters left", not any(ord(c) < 32 and c not in "\n\t" for c in text), repr(text))
check("doc: paragraphs are lines", text.splitlines()[0] == "Synthetic Constructicon fixture (#724)", text[:80])


# A Word 97 document built here: a Prc, then a compressed (cp1252) piece and a Unicode piece with
# nested fields and control characters.
def word97(pieces, flags=0x0200, ident=0xA5EC, nfib=0x00C1, prc=True, clx_extra=0, fc_shift=0):
    wd = bytearray(0x800)
    struct.pack_into("<HH", wd, 0, ident, nfib)
    struct.pack_into("<H", wd, 0x0A, flags)
    cps, pcds, at, cp = [0], b"", 0x400, 0
    for text, compressed in pieces:
        if compressed:
            raw = text.encode("cp1252")
            wd[at:at + len(raw)] = raw
            fc = (at * 2) | 0x40000000
        else:
            raw = text.encode("utf-16-le")
            wd[at:at + len(raw)] = raw
            fc = at + fc_shift
        pcds += struct.pack("<HIH", 0, fc, 0)
        at += len(raw) + 16
        cp += len(text)
        cps.append(cp)
    plc = struct.pack(f"<{len(cps)}I", *cps) + pcds
    clx = (b"\x01\x02\x00\xaa\xbb" if prc else b"") + b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", wd, 0x01A2, 0, len(clx) + clx_extra)
    table = "1Table" if flags & 0x0200 else "0Table"
    return make_type_samples.ole_file({"WordDocument": bytes(wd), table: clx})


PIECES = [("Plain caf\xe9 \x01", True),
          ("\x13 HYPERLINK \"x\" \x13 nested \x14\x15\x14Ω link\x15 end\x07\x07Next\x0bline\x0c", False)]
kind, got = outcome(word.doc_text, write("built97.doc", word97(PIECES)))
check("doc: compressed + Unicode pieces, nested fields, marks", got == "Plain café Ω link end\nNext\nline",
      (kind, got))
check("doc: a 0Table document reads too", outcome(word.doc_text, write("built97-0t.doc", word97(PIECES, flags=0)))[1]
      == "Plain café Ω link end\nNext\nline")
check("doc: an empty document reads as '' (done, not unsupported)",
      outcome(word.doc_text, write("empty97.doc", word97([("\r", True)]))) == ("text", ""))

# ============================================================================================
# 2. formats we don't read -> None; broken files -> raise
# ============================================================================================
word6 = make_type_samples.ole_file({"WordDocument": struct.pack("<HH", 0xA5DC, 0x0065) + b"\x00" * 0x200})
check("doc: Word 6/95 (wIdent 0xA5DC) -> None", outcome(word.doc_text, write("word6.doc", word6)) == ("none", None))
check("doc: Word 95 with the 97 ident but an old nFib -> None",
      outcome(word.doc_text, write("word95.doc", word97(PIECES, nfib=0x0068))) == ("none", None))
check("doc: password-protected (fEncrypted) -> None",
      outcome(word.doc_text, write("enc.doc", word97(PIECES, flags=0x0300))) == ("none", None))
stub = make_type_samples.ole_file({"WordDocument": "not a Word FIB at all".encode("utf-16-le")})
check("doc: an unrecognised WordDocument stream -> None", outcome(word.doc_text, write("stub.doc", stub)) == ("none", None))
check("doc: the #720 sampler's stub -> None (stays 'not compatible')",
      outcome(word.doc_text, write("sampler.doc", make_type_samples.legacy_office("WordDocument", "Word 97-2003")("x")))
      == ("none", None))

with open(DOC, "rb") as f:
    doc_bytes = f.read()
OLE_ONLY = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
for name, data in (("truncated to half", doc_bytes[:len(doc_bytes) // 2]),
                   ("truncated to 1.5 KB", doc_bytes[:1536]),
                   ("an OLE header with nothing behind it", OLE_ONLY)):
    check(f"doc: {name} -> raises", outcome(word.doc_text, write("broken.doc", data))[0] == "raised",
          outcome(word.doc_text, write("broken.doc", data)))
check("doc: a piece table past the end of the table stream -> raises",
      outcome(word.doc_text, write("badclx.doc", word97(PIECES, clx_extra=8000)))[0] == "raised")
check("doc: a text piece past the end of the document -> raises",
      outcome(word.doc_text, write("badfc.doc", word97(PIECES, fc_shift=0x10000)))[0] == "raised")

# ============================================================================================
# 3. the .ppt reader
# ============================================================================================
kind, ptext = outcome(powerpoint.ppt_text, PPT)
check("ppt: reads text", kind == "text" and ptext, (kind, ptext))
ptext = ptext or ""
check("ppt: slides in order, then the notes", ptext == (
    "Rover deck (synthetic #724 fixture)\nSlide one body: wheels and motors\n\n"
    "Second slide — café Ω\nSlide two body text\n\n"
    "Third slide\nSlide three body text\n\n"
    "Speaker note for slide one"), repr(ptext))
raw = streams(PPT)["PowerPoint Document"]
check("ppt: the fixture's masters DO hold placeholder text (so the filter is tested)",
      "Click to edit the title text format".encode("utf-16-le") in raw)
check("ppt: no master placeholder text", "Click to edit" not in ptext and "Outline Level" not in ptext, ptext)

ppt_streams = {k: v for k, v in streams(PPT).items() if k in ("Current User", "PowerPoint Document")}


def ppt_with(cu_patch=None, drop=None):
    s = dict(ppt_streams)
    if cu_patch:
        cu = bytearray(s["Current User"])
        for off, fmt, value in cu_patch:
            struct.pack_into(fmt, cu, off, value)
        s["Current User"] = bytes(cu)
    if drop:
        s.pop(drop)
    return make_type_samples.ole_file(s)


check("ppt: no Current User stream (PowerPoint 95 and older) -> None",
      outcome(powerpoint.ppt_text, write("p95.ppt", ppt_with(drop="Current User"))) == ("none", None))
check("ppt: an older docFileVersion -> None",
      outcome(powerpoint.ppt_text, write("pver.ppt", ppt_with([(22, "<H", 0x03F3)]))) == ("none", None))
check("ppt: password-protected (encrypted header token) -> None",
      outcome(powerpoint.ppt_text, write("penc.ppt", ppt_with([(12, "<I", powerpoint.HEADER_TOKEN_ENCRYPTED)]))) == ("none", None))
check("ppt: the #720 sampler's stub -> None",
      outcome(powerpoint.ppt_text, write("sampler.ppt",
              make_type_samples.legacy_office("PowerPoint Document", "PowerPoint 97-2003")("x"))) == ("none", None))
check("ppt: a current-edit offset outside the stream -> raises",
      outcome(powerpoint.ppt_text, write("pbad.ppt", ppt_with([(16, "<I", 0x7FFFFFF0)])))[0] == "raised")
check("ppt: a current-edit offset at the wrong record -> raises",
      outcome(powerpoint.ppt_text, write("pbad2.ppt", ppt_with([(16, "<I", 0)])))[0] == "raised")
with open(PPT, "rb") as f:
    ppt_bytes = f.read()
check("ppt: truncated -> raises", outcome(powerpoint.ppt_text, write("ptrunc.ppt", ppt_bytes[:len(ppt_bytes) // 3]))[0] == "raised",
      outcome(powerpoint.ppt_text, write("ptrunc.ppt", ppt_bytes[:len(ppt_bytes) // 3])))

# ============================================================================================
# 4. through the OCR pipeline: extracted_text, text_source, statuses, search
# ============================================================================================
db.init_db()
ingest.run_in_thread = lambda fn, *a: None  # nothing runs OCR behind the test's back
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()


def mkfile(slug, src_or_bytes, ext, media_type, type_metadata=None):
    sf = f"{slug}{ext}"
    dest = paths.storage_dir() / sf
    if isinstance(src_or_bytes, bytes):
        dest.write_bytes(src_or_bytes)
    else:
        shutil.copyfile(src_or_bytes, dest)
    db.insert_upload(slug, f"{slug}{ext}", sf, "tester", media_type=media_type, file_size=dest.stat().st_size,
                     ocr_status="pending", type_metadata=type_metadata)
    return slug


def state(slug):
    r = db.get_by_slug(slug)
    tm = r.get("type_metadata") or {}
    return r["ocr_status"], tm.get("ocr_note"), tm.get("text_source"), r["extracted_text"] or ""


calls = []
_real_call = parse_limits.call


def spy(fn, *args, **kw):
    calls.append((getattr(fn, "__module__", ""), getattr(fn, "__name__", ""), kw.get("what"), kw.get("kind")))
    return _real_call(fn, *args, **kw)


parse_limits.call = spy
try:
    S_DOC = mkfile("legacy_doc", DOC, ".doc", "word", type_metadata={"word_stats": {"format": "Word 97–2003 document"}})
    S_PPT = mkfile("legacy_ppt", PPT, ".ppt", "powerpoint")
    S_W6 = mkfile("legacy_word6", word6, ".doc", "word")
    S_BAD = mkfile("legacy_trunc", doc_bytes[:len(doc_bytes) // 2], ".doc", "word")
    S_BADPPT = mkfile("legacy_bad_ppt", ppt_with([(16, "<I", 0)]), ".ppt", "powerpoint")
    for s in (S_DOC, S_PPT, S_W6, S_BAD, S_BADPPT):
        ocr.run_ocr(s)
finally:
    parse_limits.call = _real_call

st = state(S_DOC)
check("ocr: a real .doc settles done with its text", st[0] == "done" and "six wheels" in st[3], st[:3])
check("ocr: .doc text_source = 'office'", st[2] == "office", st[:3])
check("ocr: .doc carries no ocr_note", st[1] is None, st[:3])
check("ocr: .doc keeps its other type_metadata", (db.get_by_slug(S_DOC)["type_metadata"] or {}).get("word_stats"),
      db.get_by_slug(S_DOC)["type_metadata"])
st = state(S_PPT)
check("ocr: a real .ppt settles done with its slides' text", st[0] == "done" and "Slide two body text" in st[3], st[:3])
check("ocr: .ppt text_source = 'office' (type_metadata was empty)", st[2] == "office", st[:3])
check("ocr: .ppt text has no master placeholders", "Click to edit" not in st[3], st[3][:200])
check("ocr: Word 6 settles 'unsupported' with the note", state(S_W6)[:3] == ("unsupported", DOC_NOTE, None), state(S_W6))
check("ocr: a truncated .doc settles 'failed'", state(S_BAD)[0] == "failed", state(S_BAD))
check("ocr: a broken .ppt settles 'failed'", state(S_BADPPT)[0] == "failed", state(S_BADPPT))
hits = [r["slug"] for r in db.search("wheels")]
check("search: a word from the .doc finds it (and the .ppt)", S_DOC in hits and S_PPT in hits, hits)
check("search: a word only in the .ppt's third slide finds it", S_PPT in [r["slug"] for r in db.search("three body")])

check("limits: .doc extraction ran through parse_limits.call (background budget)",
      ("core.object_types.word", "extract_text_for_row", "text extraction", parse_limits.BACKGROUND) in calls, calls)
check("limits: .ppt extraction ran through parse_limits.call (background budget)",
      ("core.object_types.powerpoint", "extract_text_for_row", "text extraction", parse_limits.BACKGROUND) in calls, calls)
if parse_limits.sandbox_available():
    row = db.get_by_slug(S_DOC)
    in_child = _real_call(word.extract_text_for_row, row, kind=parse_limits.BACKGROUND, what="text extraction")
    check("limits: a real parser child returns the same .doc text", in_child == word.extract_text_for_row(row))
    in_child = _real_call(powerpoint.extract_text_for_row, db.get_by_slug(S_PPT), kind=parse_limits.BACKGROUND)
    check("limits: a real parser child returns the same .ppt text", in_child == ptext)
else:
    print(f"SKIP parser-child checks on {sys.platform} (no forkserver: hooks run in-process)")

check("limits: doc_text honours the output cap", len(word.doc_text(DOC, cap=25)) == 25)
check("limits: ppt_text honours the output cap", len(powerpoint.ppt_text(PPT, cap=30)) == 30)
with olefile.OleFileIO(DOC) as o:
    check("limits: read_ole_stream refuses a stream over its cap",
          outcome(_office.read_ole_stream, o, "WordDocument", 100) == ("raised", "ValueError"))
_cap = _office.MAX_PART_BYTES
_office.MAX_PART_BYTES = 1000
try:
    check("limits: a .doc whose WordDocument is over MAX_PART_BYTES -> raises",
          outcome(word.doc_text, DOC) == ("raised", "ValueError"), outcome(word.doc_text, DOC))
    check("limits: a .ppt whose stream is over MAX_PART_BYTES -> raises",
          outcome(powerpoint.ppt_text, PPT) == ("raised", "ValueError"), outcome(powerpoint.ppt_text, PPT))
    S_BIG = mkfile("legacy_big", DOC, ".doc", "word")
    ocr.run_ocr(S_BIG)
    check("limits: ...and OCR settles it 'failed' (a limit, not 'no text')", state(S_BIG)[0] == "failed", state(S_BIG))
finally:
    _office.MAX_PART_BYTES = _cap

# ============================================================================================
# 5. set_extracted_text and text_source
# ============================================================================================
db.set_extracted_text(S_DOC, "replaced by OCR")
check("db: text without a source clears text_source", state(S_DOC)[2] is None and
      (db.get_by_slug(S_DOC)["type_metadata"] or {}).get("word_stats"), state(S_DOC))
S_NULL = mkfile("null_meta", DOC, ".doc", "word")
c = sqlite3.connect(db.DB_PATH)
c.execute("UPDATE capture_events SET type_metadata = '' WHERE slug = ?", (S_NULL,))
c.commit()
c.close()
db.set_extracted_text(S_NULL, "x", source="office")
check("db: a row with empty type_metadata gets text_source", state(S_NULL)[2] == "office", state(S_NULL))
db.set_extracted_text(S_NULL, "")
check("db: emptied text clears it", state(S_NULL)[2] is None, state(S_NULL))

# ============================================================================================
# 6. .xls whole numbers
# ============================================================================================
S_XLS = mkfile("legacy_xls", XLS, ".xls", "spreadsheet")
try:
    import xlrd  # noqa: F401  (in the app image; not every dev machine has it)
except ImportError:
    xlrd = None
    print("SKIP xls fixture checks (no xlrd here; the app image has it)")
if xlrd:
    xtext = spreadsheet.extract_text_for_row(db.get_by_slug(S_XLS))
    check("xls: whole numbers have no '.0'",
          "Wheel\t6\t2.5" in xtext and "Motor\t2\t12" in xtext and "Year\t1999\t0.25" in xtext, repr(xtext))
    check("xls: no '.0' anywhere", ".0\t" not in xtext and not xtext.rstrip().endswith(".0") and ".0\n" not in xtext,
          repr(xtext))
check("xls: _search_text keeps real decimals and text", (spreadsheet._search_text(12.0), spreadsheet._search_text(2.5),
      spreadsheet._search_text("12.0"), spreadsheet._search_text(-3.0)) == ("12", "2.5", "12.0", "-3"))

ctx.__exit__(None, None, None)
print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
