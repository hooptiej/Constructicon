#!/usr/bin/env python3
"""Batch 10 "Type Casting": #737 (.opus / .m4v accepted), #790 (legacy .xls grey like .doc/.ppt) and
the object-type part of #556 (document preview, captions for PDF / Illustrator only, Word /
PowerPoint / spreadsheets excused by the Curator's caption check, the url type's OCR lamp).
Throwaway DB + storage (scripts/_testenv.py), no server, no network:

    python scripts/test_type_casting_737_790_556.py

The .opus / .m4v sample files themselves (ffmpeg-made, ffprobe'd, thumbnailed) are checked in
scripts/test_type_samples_720.py; here they are uploaded through the real upload route. Covers:
  1. #737: an upload of .opus and .m4v is accepted (200), lands as audio / video, and /f/ serves
     them inline as audio/ogg / video/mp4; a still-unclaimed sibling (.flac) is refused, saying so;
  2. #790: through ocr.run_ocr, an .xls holding no workbook settles 'unsupported' (grey) with a plain
     reason, the same state as the .doc / .ppt stubs; one that starts like a workbook and breaks is
     still 'failed' saying it's corrupt;
  3. #556 document: a written post with no body previews "No write-up yet", never the uploader's note;
  4. #556 captions: PDF and AI are caption-capable, Word / PowerPoint / spreadsheets are not and each
     carries a plain caption_excuse; the contract refuses a type that is both; the Curator's caption
     check lists the excused files with their reason (never in `unmet`, never silent), counts only
     captionable files, and the nudge summary says so; explain_card carries it; a PDF with no
     picture to caption fails with a reason (not a bare "failed");
  5. #556 url: a web link never settles OCR 'failed' when there is nothing it could read (fetching
     off on this install, no address): grey 'unsupported' with the reason (#754's reader).
Prints PASS/FAIL per check and ends with ALL PASS or FAILED: <names>.
"""

import os
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("type-casting-737-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows); the svg type imports it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

import make_type_samples  # noqa: E402  (scripts/: its hand-built OLE writer)
from core import actor, captions, cards, curator, curator_needs, db, membership, object_types, ocr, paths  # noqa: E402
from core.object_types import ObjectTypeContractError, ObjectTypeSpec, PreviewContext, ThumbnailSource  # noqa: E402
from core.object_types import document, spreadsheet  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
_n = [0]


def mkfile(ext, media_type, data=b"x", **kw):
    _n[0] += 1
    slug = f"tc-{_n[0]}"
    sf = f"{slug}{ext}"
    (paths.storage_dir() / sf).write_bytes(data)
    db.insert_upload(slug, sf, sf, "tester", media_type=media_type, file_size=len(data), **kw)
    return slug


def ocr_state(slug):
    r = db.get_by_slug(slug)
    return r["ocr_status"], (r.get("type_metadata") or {}).get("ocr_note")


# ============================================================================================
print("--- 1. #737: .opus and .m4v uploads are accepted ---")
from web.app import app  # noqa: E402
client = _testenv.client(app)
OGG = b"OggS" + b"\x00" * 60
M4V = b"\x00\x00\x00\x18ftypM4V \x00\x00\x02\x00M4V isom" + b"\x00" * 40
for name, data, want_type, want_ct in (("tone.opus", OGG, "audio", "audio/ogg"), ("clip.m4v", M4V, "video", "video/mp4")):
    r = client.post("/api/upload", files={"file": (name, data, "application/octet-stream")}, data={"tech": "tester"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    slug = body.get("slug") or (body.get("item") or {}).get("slug")
    check(f"{name}: upload accepted (200)", r.status_code == 200 and bool(slug), (r.status_code, r.text[:300]))
    row = db.get_by_slug(slug) if slug else None
    check(f"{name}: lands as {want_type}", bool(row) and row["media_type"] == want_type, row and row["media_type"])
    if slug:
        f = client.get(f"/f/{slug}")
        check(f"{name}: /f/ serves it inline as {want_ct}",
              f.status_code == 200 and f.headers.get("content-type", "").startswith(want_ct)
              and f.headers.get("content-disposition", "").startswith("inline"),
              (f.status_code, f.headers.get("content-type"), f.headers.get("content-disposition")))
r = client.post("/api/upload", files={"file": ("tone.flac", b"fLaC" + b"\x00" * 40, "audio/flac")}, data={"tech": "tester"})
check("an unclaimed sibling (.flac) is still refused, saying which extension",
      r.status_code >= 400 and ".flac" in r.text, (r.status_code, r.text[:200]))

# ============================================================================================
print("--- 2. #790: legacy .xls lands grey like .doc / .ppt ---")
with actor.acting_as(actor.ACTOR_SYSTEM):
    stubs = {ext: mkfile(ext, mt, make_type_samples.legacy_office(stream, label)("x"), ocr_status="pending")
             for ext, mt, stream, label in ((".xls", "spreadsheet", "Workbook", "Excel 97-2003"),
                                            (".doc", "word", "WordDocument", "Word 97-2003"),
                                            (".ppt", "powerpoint", "PowerPoint Document", "PowerPoint 97-2003"))}
    for s in stubs.values():
        ocr.run_ocr(s)
    states = {ext: ocr_state(s) for ext, s in stubs.items()}
    check("the .xls, .doc and .ppt stubs all settle 'unsupported' (grey)",
          {st for st, _ in states.values()} == {"unsupported"}, states)
    check(".xls reason is plain words, like the others",
          states[".xls"][1] == spreadsheet.UNRECOGNISED_NOTE and states[".doc"][1].startswith("legacy .doc:"), states)
    no_stream = mkfile(".xls", "spreadsheet", make_type_samples.ole_file({"SamplerNote": b"nothing"}), ocr_status="pending")
    ocr.run_ocr(no_stream)
    check(".xls with no Workbook stream at all: grey with the same reason",
          ocr_state(no_stream) == ("unsupported", spreadsheet.UNRECOGNISED_NOTE), ocr_state(no_stream))
    broken = mkfile(".xls", "spreadsheet",
                    make_type_samples.ole_file({"Workbook": b"\x09\x08\x10\x00\x00\x06\x05\x00" + b"\xff" * 4}),
                    ocr_status="pending")
    ocr.run_ocr(broken)
    st = ocr_state(broken)
    check("an .xls that starts like a workbook and breaks: 'failed', saying it's corrupt",
          st[0] == "failed" and (st[1] or "").startswith("corrupt or unreadable .xls: "), st)

# ============================================================================================
print("--- 3. #556: a written post with no body says 'No write-up yet' ---")
html = str(document.preview(PreviewContext(item={"type_metadata": {}, "description": "UPLOADER NOTE",
                                                 "content_description": "Some title"},
                                           media_url=None, thumb_url=None, page_url=None, mode="live")))
check("no body: 'No write-up yet'", "No write-up yet" in html, html)
check("no body: the uploader's note is not shown as the post", "UPLOADER NOTE" not in html and "Some title" not in html, html)
html = str(document.preview(PreviewContext(item={"type_metadata": {"body": "# Hello"}}, media_url=None,
                                           thumb_url=None, page_url=None, mode="live")))
check("with a body: the body renders", "Hello" in html and "No write-up yet" not in html, html)

# ============================================================================================
print("--- 4. #556: captions for PDF / Illustrator only; Word / PowerPoint / spreadsheets excused ---")
T = object_types.OBJECT_TYPES
check("pdf and ai are caption-capable", T["pdf"].caption_capable and T["ai"].caption_capable)
for k in ("word", "powerpoint", "spreadsheet"):
    check(f"{k}: not caption-capable, with its own plain excuse",
          not T[k].caption_capable and T[k].caption_excuse
          and object_types.caption_excuse(T[k]) == T[k].caption_excuse, T[k].caption_excuse)
check("word's excuse reads as the owner asked",
      T["word"].caption_excuse == "Word documents aren't captioned; their text is extracted instead")
check("a caption-capable type has no excuse", object_types.caption_excuse(T["pdf"]) == "")
check("a type with no excuse of its own gets the generic one",
      object_types.caption_excuse(T["stl"]) == f"{T['stl'].label}s aren't captioned", object_types.caption_excuse(T["stl"]))
check("pdf and ai are in captions.caption_capable_types()",
      {"pdf", "ai"} <= set(captions.caption_capable_types()) and "word" not in captions.caption_capable_types())
try:
    object_types.register(ObjectTypeSpec(key="tc-bad", label="Bad", thumbnail_source=ThumbnailSource.NONE,
                                          ocr_capable=False, caption_capable=True, caption_excuse="nope",
                                          preview_fn=document.preview, properties_fn=document.get_properties))
    refused = False
except ObjectTypeContractError as e:
    refused = "caption_excuse" in str(e)
object_types.OBJECT_TYPES.pop("tc-bad", None)
check("the contract refuses a type that is both caption_capable and excused", refused)


def caption_row(card_row):
    return next(c for c in curator.score_project(card_row)["checklist"] if c["item"] == "captions")


def card_named(title):
    return next(p for p in db.list_projects() if p["title"] == title)


with actor.acting_as(actor.ACTOR_UI):
    cards.create("Mixed Card")
    cards.create("Office Card")
    mixed, office = card_named("Mixed Card"), card_named("Office Card")
    imgs = [mkfile(".png", "image") for _ in range(2)]
    pdf = mkfile(".pdf", "pdf", description="a captioned pdf")
    words = [mkfile(".docx", "word") for _ in range(2)]
    deck = mkfile(".pptx", "powerpoint")
    membership.add_files(mixed["id"], imgs + [pdf] + words + [deck], link_tag=True, auto_cover=False)
    membership.add_files(office["id"], [mkfile(".docx", "word"), mkfile(".xlsx", "spreadsheet"),
                                        mkfile(".pptx", "powerpoint")], link_tag=True, auto_cover=False)
    cards.set_status(mixed["id"], "done")
    cards.set_status(office["id"], "done")
mixed, office = card_named("Mixed Card"), card_named("Office Card")

row = caption_row(mixed)
check("mixed: only the 3 captionable files count (1 of 3 captioned: fails)",
      row["passed"] is False and not row["excused"] and "1 of 3 captionable files" in row["reason"], row)
ex = {e["type"]: e for e in row["excused_files"]}
check("mixed: Word and PowerPoint listed as excused, with counts and the plain reason",
      ex.get("word", {}).get("count") == 2 and ex.get("powerpoint", {}).get("count") == 1
      and ex["word"]["reason"] == T["word"].caption_excuse, row["excused_files"])
check("mixed: the reason names what isn't counted and why",
      "Not counted: 2 Word documents (Word documents aren't captioned" in row["reason"], row["reason"])
nudge = next((n for n in curator_needs.project_nudges(mixed) if n["kind"] == "thin_captions"), None)
check("mixed: the thin-captions nudge's summary is that same reason", nudge and nudge["summary"] == row["reason"], nudge)

row = caption_row(office)
check("office-only card: the caption check is EXCUSED, not failed",
      row["excused"] is True and {"word", "spreadsheet", "powerpoint"} == {e["type"] for e in row["excused_files"]}, row)
check("office-only card: the reason says so in plain words",
      row["reason"].startswith("Excused: nothing here is captioned.") and "Spreadsheets aren't captioned" in row["reason"],
      row["reason"])
score = curator.score_project(office)
check("office-only card: captions is never in `unmet`",
      not any(u["item"] == "captions" for u in score["unmet"]), score["unmet"])
check("office-only card: no thin-captions nudge",
      not any(n["kind"] == "thin_captions" for n in curator_needs.project_nudges(office)))
exp = cards.explain_card(office["id"])
check("explain_card (MCP) carries the caption check with its excuses",
      exp.get("caption_check", {}).get("excused") is True and len(exp["caption_check"]["excused_files"]) == 3,
      exp.get("caption_check"))
page = client.get(f"/project/{office['slug']}")
check("the card page's health badge says why captions are excused",
      page.status_code == 200 and "Captions: Excused: nothing here is captioned." in page.text.replace("&#39;", "'"),
      page.status_code)

# A PDF whose first page can't be rendered: a red caption with the reason, never a bare "failed".
bad_pdf = mkfile(".pdf", "pdf", b"%PDF-1.4 not really")
real_disabled, real_ensure = captions.DISABLED, captions.thumbnails.ensure_thumbnail
captions.DISABLED = False
captions.thumbnails.ensure_thumbnail = lambda row: None
try:
    with actor.acting_as(actor.ACTOR_SYSTEM):
        captions._run_caption_now(bad_pdf)
finally:
    captions.DISABLED, captions.thumbnails.ensure_thumbnail = real_disabled, real_ensure
tm = db.get_by_slug(bad_pdf)["type_metadata"] or {}
check("a PDF with no picture: caption 'failed' WITH the plain reason",
      tm.get("auto_caption_status") == "failed" and "no picture to caption" in (tm.get("auto_caption_failed_reason") or ""),
      tm)

# ============================================================================================
print("--- 5. #556: a web link never shows an OCR failure it can't have (#754) ---")
check("this test runs with page fetching off", os.environ.get("CONSTRUCTICON_LINK_FETCH") == "off")
with actor.acting_as(actor.ACTOR_SYSTEM):
    link = "tc-link"
    db.insert_content(link, "tester", "url", external_url="https://example.com/")
    ocr.run_ocr(link)
    st = ocr_state(link)
    check("fetching off: grey 'unsupported' with the reason, not 'failed'",
          st[0] == "unsupported" and "page fetching is turned off" in (st[1] or ""), st)
    bare = "tc-link-bare"
    db.insert_content(bare, "tester", "url", external_url=None)
    ocr.run_ocr(bare)
    st = ocr_state(bare)
    check("no address: grey 'unsupported', not 'failed'", st[0] in ("unsupported", None) and st[0] != "failed", st)

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
