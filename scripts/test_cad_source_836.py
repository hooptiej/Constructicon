#!/usr/bin/env python3
"""#836: the "CAD source" drop-in type (core/object_types/cad_source.py): .step / .stp / .f3d / .3mf /
.obj / .igs (+ .iges) are stored, read for their facts and findable.

Throwaway DB + storage (scripts/_testenv.py), the real app through TestClient, no server:

    python scripts/test_cad_source_836.py

Covers:
  * the registry: the type is registered, claims exactly the owner's extensions (+ .iges), every one
    resolves to it (detect_media_type), and the upload picker's accepted list carries them
  * the STEP header reader: file name, author, organization, program, exporter, time stamp, description,
    schema (+ application protocol); string escapes (\\X2\\ UTF-16, '' quote), comments, a missing HEADER
    reported as such, a time stamp turned into the content date (export-prone)
  * the sniffers refuse what is not the thing (text renamed .step, a compiled .obj with NUL bytes, a zip
    with no model part renamed .3mf) with the type's own words, and accept .f3d / .igs by extension
  * 3MF: the metadata (Title, Designer, Application, CreationDate, unit) and the package's own picture as
    the thumbnail (found through the thumbnail relationship); an oversized picture is not used
  * OBJ: counts, size, malformed lines counted not hidden, the program guess labelled as a guess
  * F3D: only the zip container is read (member count, a preview image when a member is named like one),
    a non-zip .f3d is stored with a note; IGES Start section
  * through the real upload route: type, properties, content date, search by program / author, the
    object page, hostile text escaped, the refusal as a 400 with the sniffer's message
  * the sampler lists a sample for every extension (scripts/check_type_samples.py covers the rest)

Exits 1 if any check fails.
"""

import io
import os
import struct
import sys
import types
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("cad-source-836-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, db, ingest, object_types, ocr, paths, storage, thumbnails  # noqa: E402
from core.object_types import cad_source  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


try:
    from web import app as webapp  # noqa: E402
except Exception as e:  # web stack not importable here: the box run covers it
    webapp = None
    print(f"SKIP web checks (web import failed: {e!r})")

client = None
if webapp is not None:
    client = _testenv.client(webapp.app, raise_server_exceptions=False)
    with client:  # real startup (init_db + migrations)
        pass
else:
    db.init_db()

ingest.run_in_thread = lambda fn, *a: None  # nothing runs OCR behind the test's back
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()


def png(color="teal", size=(48, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# ---- fixtures, built in memory -------------------------------------------------------------------
STEP = (b"ISO-10303-21;\nHEADER;\n/* made by a test */\n"
        b"FILE_DESCRIPTION(('Ubiquiti Mini Rack','second line'),'2;1');\n"
        b"FILE_NAME('Ubiquiti-Mini-Rack.step','2022-03-14T09:26:53',('Jane M\\X2\\00E9\\X0\\ker'),('Maker''s Org'),\n"
        b"  'Autodesk Translation Framework v12.1','Autodesk Fusion 360','Unknown');\n"
        b"FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        b"ENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\nEND-ISO-10303-21;\n")
STEP_HOSTILE = STEP.replace(b"Jane M\\X2\\00E9\\X0\\ker", b"<script>alert(1)</script>")
STEP_NO_HEADER = b"ISO-10303-21;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\nEND-ISO-10303-21;\n"

OBJ = (b"# Blender v3.6.0 OBJ File: 'bracket.blend'\n# www.blender.org\nmtllib bracket.mtl\no Bracket\n"
       b"v 0.0 0.0 0.0\nv 40.0 0.0 0.0\nv 40.0 20.0 0.0\nv 0.0 20.0 10.0\nv oops 1 2\nvn 0 0 1\nvt 0 0\n"
       b"f 1 2 3\nf 1 3 4\n")
COFF_LIKE_OBJ = b"\x4c\x01\x03\x00" + b"\x00" * 60 + b".text\x00\x00\x00"

IGES = (b"Test IGES start section line one".ljust(72) + b"S      1\n" +
        b"1H,,1H;,7Hbracket;".ljust(72) + b"G      1\n" +
        b"                                                                        T      1\n")


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


MODEL = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
         'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
         ' <metadata name="Title">Hook &amp; Loop Bracket</metadata>\n <metadata name="Designer">Jane Maker</metadata>\n'
         ' <metadata name="Application">PrusaSlicer-2.7.1</metadata>\n'
         ' <metadata name="CreationDate">2023-08-01</metadata>\n <resources/><build/>\n</model>\n').encode()
RELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        '<Relationship Target="/Metadata/plate_9.png" Id="rel1" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>'
        '</Relationships>').encode()
THREEMF = zip_bytes([("[Content_Types].xml", b"<Types/>"), ("_rels/.rels", RELS), ("3D/3dmodel.model", MODEL),
                     ("Metadata/plate_9.png", png("orange"))])
THREEMF_NO_MODEL = zip_bytes([("readme.txt", b"hello")])
THREEMF_HUGE_PIC = zip_bytes([("_rels/.rels", RELS), ("3D/3dmodel.model", MODEL),
                              ("Metadata/plate_9.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024))])
F3D = zip_bytes([("Manifest.dat", b"x"), ("Properties.dat", b"y"), ("FusionAssetName", b"z"),
                 ("Design1/Preview/thumbnail.png", png("purple"))])
F3D_NOT_ZIP = b"this is not a zip container at all"

# ============================================================================================
# 1. the registry
# ============================================================================================
spec = object_types.OBJECT_TYPES.get("cad_source")
check("registry: the cad_source type is registered as 'CAD source'", spec is not None and spec.label == "CAD source")
WANT = {".step", ".stp", ".f3d", ".3mf", ".obj", ".igs", ".iges"}
check("registry: it claims .step .stp .f3d .3mf .obj .igs (+ .iges)", spec is not None and set(spec.extensions) == WANT,
      sorted(spec.extensions) if spec else None)
check("registry: the upload picker's accepted list carries all of them", WANT <= set(object_types.accepted_extensions()))
check("registry: no other type claims any of them",
      not [k for k, s in object_types.OBJECT_TYPES.items() if k != "cad_source" and set(s.extensions) & WANT])
check("registry: type contract hooks are module-level (run in a parser child)",
      all(getattr(spec, h).__module__ == "core.object_types.cad_source"
          for h in ("capture_fn", "properties_fn", "embedded_metadata_fn", "preview_fn", "sniff_fn", "text_extract_fn")))
check("registry: a name alone resolves too (an archive member is typed from its name, #829's unpack)",
      all(object_types.detect_media_type(f"parts/model{e}") == "cad_source" for e in sorted(WANT)))
check("registry: not captioned, with the plain reason", not spec.caption_capable and "aren't captioned" in spec.caption_excuse)

# ============================================================================================
# 2. the STEP header reader
# ============================================================================================
def tmp_write(name, data):
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


info, stamp = cad_source._scan_step(tmp_write("a.step", STEP))
check("STEP: name in file", info.get("name_in_file") == "Ubiquiti-Mini-Rack.step", info)
check("STEP: author, with the \\X2\\ escape decoded", info.get("author") == "Jane Méker", info.get("author"))
check("STEP: organization, with the '' quote", info.get("organization") == "Maker's Org", info.get("organization"))
check("STEP: program (originating system) and exporter", info.get("program") == "Autodesk Fusion 360"
      and info.get("preprocessor") == "Autodesk Translation Framework v12.1", info)
check("STEP: time stamp and description", info.get("time_stamp") == "2022-03-14T09:26:53"
      and info.get("description") == "Ubiquiti Mini Rack; second line", info)
check("STEP: schema and application protocol", info.get("schema") == "AUTOMOTIVE_DESIGN"
      and info.get("application_protocol") == "AP214", info)
check("STEP: the header text is kept for the preview (comments gone)", info["head"][0] == "HEADER;"
      and not any("made by a test" in ln for ln in info["head"]), info["head"][:3])
check("STEP: time stamp parsed", stamp is not None and (stamp.year, stamp.month, stamp.day) == (2022, 3, 14), stamp)
STEP_DAMAGED = STEP.replace(b"FILE_SCHEMA((", b"FILE_SCHEMA(('unterminated")
info3, _s3 = cad_source._scan_step(tmp_write("dmg.step", STEP_DAMAGED))
check("STEP: a damaged header keeps what read before the damage and SAYS where it stopped",
      info3.get("author") and "FILE_SCHEMA" in info3.get("header_problem", "") and "schema" not in info3, info3)
info2, stamp2 = cad_source._scan_step(tmp_write("b.step", STEP_NO_HEADER))
check("STEP: no HEADER section is said, not guessed", "header_problem" in info2 and stamp2 is None, info2)

emb = cad_source.get_embedded_metadata(tmp_write("c.step", STEP))
check("STEP: embedded metadata carries the facts and an export-prone content date",
      emb.get("type_metadata", {}).get("cad_stats", {}).get("program") == "Autodesk Fusion 360"
      and emb.get("content_date") and emb.get("content_date_export_prone") is True
      and emb.get("content_date_tag") == "cad_file_time_stamp", emb)
check("STEP: it never offers a title (a STEP name is often a path)", "content_description" not in emb)

# ============================================================================================
# 3. sniffers
# ============================================================================================
def sniff(name, data):
    return cad_source.sniff(tmp_write(name, data), name)


check("sniff: a real STEP is accepted", sniff("ok.step", STEP) and sniff("ok.stp", STEP))
check("sniff: text renamed .step is refused", not sniff("fake.step", b"just some notes\n"))
check("sniff: a real OBJ is accepted", sniff("ok.obj", OBJ))
check("sniff: a compiled .obj (NUL bytes, no OBJ statement) is refused", not sniff("win.obj", COFF_LIKE_OBJ))
check("sniff: a 3MF with a model part is accepted; a zip without one is refused",
      sniff("ok.3mf", THREEMF) and not sniff("no.3mf", THREEMF_NO_MODEL) and not sniff("txt.3mf", b"plain"))
check("sniff: .f3d and .igs are accepted by extension (even a non-zip .f3d is the owner's file)",
      sniff("any.f3d", F3D_NOT_ZIP) and sniff("any.igs", IGES) and sniff("any.iges", IGES))
msg = object_types.mismatch_reason("fake.step")
check("refusal wording: 'This .step file isn't a valid STEP (ISO 10303-21) file'",
      msg.startswith("This .step file isn't a valid STEP (ISO 10303-21) file"), msg)
check("refusal wording: .obj", object_types.mismatch_reason("x.obj").startswith("This .obj file isn't a valid Wavefront OBJ model"))
check("detect_media_type: a real STEP resolves to cad_source, text renamed .step does not",
      object_types.detect_media_type("ok.step", tmp_write("ok2.step", STEP)) == "cad_source"
      and object_types.detect_media_type("fake.step", tmp_write("fake2.step", b"nope")) is None)

# ============================================================================================
# 4. 3MF, OBJ, F3D, IGES facts
# ============================================================================================
tm = cad_source._scan(tmp_write("m.3mf", THREEMF))[0]
check("3MF: title / designer / program / created / unit", tm.get("title") == "Hook & Loop Bracket"
      and tm.get("designer") == "Jane Maker" and tm.get("program") == "PrusaSlicer-2.7.1"
      and tm.get("created") == "2023-08-01" and tm.get("unit") == "millimeter", tm)
check("3MF: the picture is found through the thumbnail relationship (not just the usual name)",
      tm.get("thumbnail") == "Metadata/plate_9.png", tm.get("thumbnail"))
check("3MF: package contents listed for the preview", tm["members"] == 4 and tm["head"][0].startswith("4 files"), tm["head"][:2])
big = cad_source._scan(tmp_write("big.3mf", THREEMF_HUGE_PIC))[0]
check("3MF: a picture over the size cap is not used (and nothing blows up)", "thumbnail" not in big, big.get("thumbnail"))

ob = cad_source._scan(tmp_write("o.obj", OBJ))[0]
check("OBJ: counts", (ob["vertices"], ob["faces"], ob["normals"], ob["texture_coords"]) == (5, 2, 1, 1), ob)
check("OBJ: the bad vertex line is counted, not hidden", ob.get("malformed_lines") == 1, ob)
check("OBJ: size from the good vertices, in model units", ob.get("size") == [40.0, 20.0, 10.0], ob.get("size"))
check("OBJ: material library, object count, header comments, labelled program guess",
      ob.get("material_libraries") == ["bracket.mtl"] and ob.get("objects") == 1
      and ob.get("program_guess", "").startswith("Blender") and ob.get("header_comments"), ob)

fd = cad_source._scan(tmp_write("d.f3d", F3D))[0]
check("F3D: a zip container is read for its file count and a preview-named image only",
      fd.get("container") == "zip" and fd.get("members") == 4 and fd.get("thumbnail") == "Design1/Preview/thumbnail.png", fd)
fn = cad_source._scan(tmp_write("n.f3d", F3D_NOT_ZIP))[0]
check("F3D: a non-zip file is stored with a note, not refused", "not a zip" in fn.get("note", ""), fn)

ig = cad_source._scan(tmp_write("i.igs", IGES))[0]
check("IGES: the Start section is read", ig.get("start_section", "").startswith("Test IGES start section"), ig)

bad = cad_source.get_embedded_metadata(tmp_write("broken.3mf", b"PK\x03\x04 truncated"))
check("a broken package is described, not fatal (read_problem is recorded)",
      "read_problem" in (bad.get("type_metadata", {}).get("cad_stats") or {}), bad)

# ============================================================================================
# 5. through the real upload route
# ============================================================================================
def upload(name, data, ctype="application/octet-stream"):
    return client.post("/api/upload", files={"file": (name, data, ctype)})


UP = {}
if client is not None:
    for name, data in (("rack.step", STEP), ("rack.stp", STEP.replace(b"Mini Rack", b"Mini Rack Two")),
                       ("bracket.3mf", THREEMF), ("bracket.obj", OBJ), ("design.f3d", F3D),
                       ("pipe.igs", IGES), ("hostile.step", STEP_HOSTILE)):
        r = upload(name, data)
        ok = r.status_code == 200 and r.json().get("media_type") == "cad_source"
        check(f"upload {name}: stored as a CAD source", ok, (r.status_code, r.text[:200]))
        if ok:
            UP[name] = r.json()["slug"]
            row = db.get_by_slug(UP[name])
            thumbnails.ensure_thumbnail(row)

    row = db.get_by_slug(UP["rack.step"])
    tm_ = row["type_metadata"]["cad_stats"]
    check("upload: the STEP's facts are on the item", tm_["program"] == "Autodesk Fusion 360", tm_)
    check("upload: the content date came from the STEP time stamp (2022-03-14)",
          row["content_date"] is not None and 1647000000 < row["content_date"] < 1648000000, row["content_date"])
    check("upload: the title was NOT taken from the file", not row.get("content_description") and not row.get("display_name"),
          (row.get("content_description"), row.get("display_name")))
    props = object_types.get_object_type("cad_source").properties_fn(row)
    check("properties: Name in file / Author / Program / Date written / Schema / Open with",
          props.get("Name in file") == "Ubiquiti-Mini-Rack.step" and props.get("Author") == "Jane Méker"
          and props.get("Program") == "Autodesk Fusion 360" and props.get("Date written") == "2022-03-14T09:26:53"
          and props.get("Format") == "STEP (ISO 10303-21), AP214" and "Open with" in props, props)

    for name in ("bracket.3mf", "design.f3d"):
        check(f"{name}: has a thumbnail file", storage.thumb_path_for(UP[name]).exists())
    for name in ("rack.step", "bracket.obj", "pipe.igs"):
        check(f"{name}: no thumbnail (nothing to draw it from)", not storage.thumb_path_for(UP[name]).exists())
    t = client.get(f"/f/{UP['bracket.3mf']}/thumb")
    check("3MF thumbnail serves as an image", t.status_code == 200 and t.headers["content-type"].startswith("image/"),
          (t.status_code, t.headers.get("content-type")))

    for name in ("rack.step", "bracket.3mf", "bracket.obj", "design.f3d", "pipe.igs"):
        ocr.run_ocr(UP[name])  # the file's facts become its searchable text; a 3MF's picture is not OCR'd
        got = db.get_by_slug(UP[name])
        check(f"{name}: text settles 'done' with the file's facts in extracted_text", got["ocr_status"] == "done"
              and "CAD source" in (got["extracted_text"] or ""), (got["ocr_status"], (got["extracted_text"] or "")[:120]))

    found = db.search(query="Fusion")
    check("search: the program name inside the STEP header finds the item",
          UP["rack.step"] in {r["slug"] for r in found}, [r["slug"] for r in found])
    found = db.search(query="Jane Maker")
    check("search: the designer inside the 3MF finds it", UP["bracket.3mf"] in {r["slug"] for r in found})

    page = client.get(f"/object/{UP['rack.step']}")
    check("object page (STEP): renders with the header text and the facts",
          page.status_code == 200 and "FILE_NAME" in page.text and "Autodesk Fusion 360" in page.text, page.status_code)
    page = client.get(f"/object/{UP['hostile.step']}")
    check("object page: hostile header text is escaped, never live markup",
          page.status_code == 200 and "<script>alert(1)</script>" not in page.text and "&lt;script&gt;alert(1)" in page.text,
          page.status_code)
    page = client.get(f"/object/{UP['bracket.3mf']}")
    check("object page (3MF): shows its own picture", page.status_code == 200 and 'id="preview-img"' in page.text, page.status_code)
    page = client.get(f"/object/{UP['bracket.obj']}")
    check("object page (OBJ): renders", page.status_code == 200 and "Wavefront OBJ" in page.text, page.status_code)

    r = upload("fake.step", b"this is a text file renamed to .step")
    err = (r.json().get("detail") or r.json().get("error", {}).get("message") or "") if r.status_code >= 400 else ""
    check("upload: text renamed .step is refused with the sniffer's own words (400)",
          r.status_code == 400 and "isn't a valid STEP (ISO 10303-21) file" in str(r.json()), (r.status_code, r.text[:200]))
    r = upload("win.obj", COFF_LIKE_OBJ)
    check("upload: a compiled .obj is refused (400, 'Wavefront OBJ model')", r.status_code == 400
          and "Wavefront OBJ model" in r.text, (r.status_code, r.text[:200]))

# ============================================================================================
# 6. preview, export mode
# ============================================================================================
if client is not None:
    row = db.get_by_slug(UP["hostile.step"])
    pctx = object_types.PreviewContext(item=dict(row), media_url="x/y.step", thumb_url=None, page_url=None, mode="export",
                                       file_path=None)
    html = str(object_types.render_preview(object_types.get_object_type("cad_source"), pctx) or "")
    check("export preview: escaped header text, no raw <script>", "<script>" not in html and "&lt;script&gt;" in html, html[:200])

# ============================================================================================
# 7. the sampler
# ============================================================================================
src = open(os.path.join(ROOT, "scripts", "make_type_samples.py"), encoding="utf-8").read()
check("sampler: a sample for every extension the type claims",
      all(f'"cad-source{e}"' in src or f"cad-source{e}" in src for e in sorted(WANT)))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("ALL PASS")
