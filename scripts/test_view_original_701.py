#!/usr/bin/env python3
"""Self-contained check for #701 ("look, don't take"): "View original" opens what the browser can show.

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient, fixtures
generated here at run time. Run anywhere the requirements are installed:

    python scripts/test_view_original_701.py

Covers:
- the disposition table per type through `content_security.serve_file`: PDF, JPEG, PNG, GIF, WebP, MP4,
  MP3, TXT inline; STL, ZIP, PSD, EXE, TTF attachment; HTML/XHTML attachment + sandbox CSP; SVG inline +
  CSP; XML CSP; an HTML file named .pdf / .jpg treated as HTML; nosniff on every one;
- #723: WAV/M4A/FLAC/OGG/AAC inline, MOV a download; every media type alias (`BROWSER_TYPE_ALIASES`)
  folds into the allowlist; `.wav` guessed as any of its names is served inline as `audio/wav`;
- the original filename kept (plain, with spaces, with quotes, with unicode);
- the link label and target per type (`object_types.original_link`, the shared preview helper);
- real `/f/<slug>` responses and object pages for uploaded PDF, PNG, TXT, STL and ZIP files.
#610's own rules are checked by scripts/test_f_sandbox_610.py, which must keep passing.
Exits 1 if any check fails.
"""
import io
import os
import sys
import tempfile
import types
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("view701-")
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine: the SVG thumbnail can't be drawn, everything else runs
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from pathlib import Path  # noqa: E402
from urllib.parse import quote  # noqa: E402
from core import db, object_types  # noqa: E402
from core.object_types import _preview  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from web import app as webapp, content_security  # noqa: E402

client = _testenv.client(webapp.app, raise_server_exceptions=False)  # the install token (admin)
anon = TestClient(webapp.app, raise_server_exceptions=False)
with client:
    pass
CSP = content_security.RENDERED_HTML_CSP


def img_bytes(fmt):
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (120, 140, 40)).save(b, fmt)
    return b.getvalue()


HTML = b"<!DOCTYPE html><html><body><script>fetch('/api/settings')</script>zq701</body></html>"
PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
STL = (b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\n"
       b"endsolid t\n")
zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w") as z:
    z.writestr("readme.txt", "hello")
ZIP = zbuf.getvalue()

# --- 1. the disposition table, straight through serve_file ------------------------------------------
# (name, bytes, want disposition, want CSP)
TABLE = [
    ("doc.pdf", PDF, "inline", False),
    ("photo.jpg", img_bytes("JPEG"), "inline", False),
    ("pic.png", img_bytes("PNG"), "inline", False),
    ("anim.gif", img_bytes("GIF"), "inline", False),
    ("pic.webp", img_bytes("WEBP"), "inline", False),
    ("clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom", "inline", False),
    ("song.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x00", "inline", False),
    ("notes.txt", b"just some notes\n", "inline", False),
    # #723: WAV plays inline whatever name Python's mimetypes gives it (3.14: audio/vnd.wave)
    ("tone.wav", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24, "inline", False),
    ("tone.m4a", b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom", "inline", False),
    ("tone.flac", b"fLaC\x00\x00\x00\x22" + b"\x00" * 20, "inline", False),
    ("tone.ogg", b"OggS\x00\x02" + b"\x00" * 20, "inline", False),
    ("tone.aac", b"\xff\xf1\x50\x80\x00\x1f\xfc", "inline", False),
    ("clip.mov", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ", "attachment", False),  # #701: a download
    ("part.stl", STL, "attachment", False),
    ("bundle.zip", ZIP, "attachment", False),
    ("art.psd", b"8BPS\x00\x01" + b"\x00" * 20, "attachment", False),
    ("setup.exe", b"MZ\x90\x00" + b"\x00" * 60, "attachment", False),
    ("face.ttf", b"\x00\x01\x00\x00\x00\x0c" + b"\x00" * 20, "attachment", False),
    ("evil.html", HTML, "attachment", True),
    ("evil.xhtml", b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><script>x</script></html>',
     "attachment", True),
    ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>', "inline", True),
    ("feed.xml", b'<?xml version="1.0"?><root>zq</root>', "inline", True),
    # #701's own trap: markup with a viewable name must not ride the inline rule
    ("not-really.pdf", HTML, "attachment", True),
    ("not-really.jpg", HTML, "attachment", True),
    ("not-really.txt", HTML, "attachment", True),
]
d = Path(tempfile.mkdtemp(dir=TMP))
print("--- serve_file: disposition per type ---")
for i, (name, data, want, want_csp) in enumerate(TABLE):
    p = d / f"stored{i}{Path(name).suffix}"
    p.write_bytes(data)
    r = content_security.serve_file(p, filename=name)
    disp = r.headers.get("content-disposition", "")
    check(f"{name}: {want}", disp.split(";")[0] == want, disp)
    check(f"{name}: keeps its name", f'filename="{name}"' in disp, disp)
    check(f"{name}: nosniff", r.headers.get("x-content-type-options") == "nosniff")
    if want_csp:
        check(f"{name}: sandbox CSP", r.headers.get("content-security-policy") == CSP, r.headers.get("content-security-policy"))
    else:
        check(f"{name}: no CSP", "content-security-policy" not in r.headers, r.headers.get("content-security-policy"))
    # what the page link says must agree with what the server does (except sniffed markup: always a download)
    label = object_types.original_link({"filename": name, "type_label": "X"})
    if not name.startswith("not-really") and not want_csp:
        check(f"{name}: link agrees ({label['label']})", label["new_tab"] == (want == "inline"), label)

print("\n--- serve_file: filenames that need encoding ---")
for name, want in (("my scan 01.pdf", "inline; filename*=utf-8''" + quote("my scan 01.pdf")),
                   ('say "hi".txt', "inline; filename*=utf-8''" + quote('say "hi".txt')),
                   ("Ünïcødé ☃ part.stl", "attachment; filename*=utf-8''" + quote("Ünïcødé ☃ part.stl")),
                   ("über.html", "attachment; filename*=utf-8''" + quote("über.html"))):
    p = d / f"enc{abs(hash(name))}{Path(name).suffix}"
    p.write_bytes(HTML if name.endswith(".html") else PDF if name.endswith(".pdf") else STL if name.endswith(".stl") else b"hi")
    disp = content_security.serve_file(p, filename=name).headers.get("content-disposition", "")
    check(f"{name!r}: {want}", disp == want, disp)
check("content_disposition: no name", content_security.content_disposition("inline", None) == "inline")

print("\n--- serve_file: thumbnail path (no filename) unchanged ---")
p = d / "thumb.jpg"
p.write_bytes(img_bytes("JPEG"))
r = content_security.serve_file(p)
check("generated thumbnail: no Content-Disposition, nosniff", "content-disposition" not in r.headers
      and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
p = d / "fallback.html"
p.write_bytes(HTML)
r = content_security.serve_file(p, type_name="orig.html")
check("thumb fallback to an HTML original: attachment + CSP", r.headers.get("content-disposition", "").startswith("attachment")
      and r.headers.get("content-security-policy") == CSP, dict(r.headers))

print("\n--- #723: media type aliases (every name Python or a host's mime.types may give) ---")
import mimetypes  # noqa: E402
ALIASES = {
    "audio/wav": "audio/wav", "audio/x-wav": "audio/wav", "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav", "audio/x-pn-wav": "audio/wav",
    "audio/mp4": "audio/mp4", "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4",
    "audio/flac": "audio/flac", "audio/x-flac": "audio/flac",
    "audio/aac": "audio/aac", "audio/x-aac": "audio/aac",
    "audio/mpeg": "audio/mpeg", "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg",
}
for alias, canon in ALIASES.items():
    check(f"{alias}: folds to {canon}", object_types.browser_media_type(alias) == canon,
          object_types.browser_media_type(alias))
    check(f"{alias}: opens in browser", object_types.opens_in_browser(media_type=alias))
    check(f"{alias}; with params, upper case: opens in browser",
          object_types.opens_in_browser(media_type=alias.upper() + "; codecs=1"))
    check(f"{alias}: disposition inline", content_security.disposition_for(None, media_type=alias) == "inline")
check("allowlist holds canonical names only", not (set(object_types.BROWSER_TYPE_ALIASES) & object_types.BROWSER_VIEWABLE_TYPES),
      set(object_types.BROWSER_TYPE_ALIASES) & object_types.BROWSER_VIEWABLE_TYPES)
check("every alias folds into the allowlist", set(object_types.BROWSER_TYPE_ALIASES.values()) <= object_types.BROWSER_VIEWABLE_TYPES)
for mt in ("video/quicktime", "image/tiff", "image/vnd.adobe.photoshop", "audio/x-ms-wma", ""):
    check(f"{mt or '(none)'}: still not viewable", not object_types.opens_in_browser(media_type=mt))

print("\n--- #723: serve_file with each name Python might guess for .wav ---")
p = d / "storedwav.wav"
p.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24)
print(f"(this Python guesses .wav as {mimetypes.guess_type('a.wav')[0]})")
_real_guess = mimetypes.guess_type
for alias in ("audio/vnd.wave", "audio/x-wav", "audio/wave", "audio/wav"):
    def _fake(url, strict=True, _alias=alias):
        return (_alias, None) if str(url).lower().endswith(".wav") else _real_guess(url, strict)
    mimetypes.guess_type = _fake
    try:
        r = content_security.serve_file(p, filename="Tone 1.wav")
        disp = r.headers.get("content-disposition", "")
        check(f".wav guessed {alias}: inline", disp.startswith("inline;"), disp)
        check(f".wav guessed {alias}: content-type audio/wav", r.headers.get("content-type") == "audio/wav",
              r.headers.get("content-type"))
        check(f".wav guessed {alias}: link says View", object_types.original_link({"filename": "a.wav", "type_label": "Audio"})
              == {"label": "View original Audio", "new_tab": True})
    finally:
        mimetypes.guess_type = _real_guess
for name, want_ct in (("x.m4a", "audio/mp4"), ("x.flac", "audio/flac"), ("x.mp3", "audio/mpeg"),
                      ("x.mov", "video/quicktime"), ("x.pdf", "application/pdf")):
    q = d / f"ct{name}"
    q.write_bytes(PDF if name.endswith(".pdf") else b"\x00" * 32)
    ct = content_security.serve_file(q, filename=name).headers.get("content-type", "")
    guess = _real_guess(name)[0]
    # an un-aliased guess goes through untouched (Starlette's own guess); an alias is folded
    check(f"{name}: content-type {object_types.browser_media_type(guess) or want_ct}",
          ct.split(";")[0] == (object_types.browser_media_type(guess) or want_ct), ct)
check(".mov stays a download", content_security.serve_file(d / "ctx.mov", filename="x.mov")
      .headers.get("content-disposition", "").startswith("attachment;"))

print("\n--- the link: label and target per type ---")
for fname, tl, want_label, want_tab in (("a.pdf", "PDF", "View original PDF", True),
                                        ("a.jpg", "Image", "View original Image", True),
                                        ("a.mp4", "Video", "View original Video", True),
                                        ("a.mp3", "Audio", "View original Audio", True),
                                        ("a.txt", "Text", "View original Text", True),
                                        ("a.stl", "STL", "Download original STL", False),
                                        ("a.psd", "PSD", "Download original PSD", False),
                                        ("a.zip", "Archive", "Download original Archive", False),
                                        ("a.ttf", "Font", "Download original Font", False),
                                        ("a.svg", "SVG", "Download original SVG", False),
                                        (None, None, "Download original file", False)):
    link = object_types.original_link({"filename": fname, "type_label": tl})
    check(f"{fname}: {want_label!r}, new tab={want_tab}", link == {"label": want_label, "new_tab": want_tab}, link)
ctx = object_types.PreviewContext(item={"filename": "a.pdf", "type_label": "PDF"}, media_url="/f/abc",
                                  thumb_url="/f/abc/thumb", page_url=None, mode="live")
html = str(_preview.thumb_with_original_link(ctx))
check("preview helper: PDF link opens a new tab", 'href="/f/abc" class="external-link-btn" target="_blank" rel="noopener">View original PDF</a>' in html, html)
ctx = object_types.PreviewContext(item={"filename": "a.stl", "type_label": "STL"}, media_url="/f/abc",
                                  thumb_url="/f/abc/thumb", page_url=None, mode="live")
html = str(_preview.thumb_with_original_link(ctx))
check("preview helper: STL link downloads in place", 'class="external-link-btn">Download original STL</a>' in html
      and "target=" not in html, html)
ctx = object_types.PreviewContext(item={"filename": "<b>x</b>.stl", "type_label": "<i>T</i>"}, media_url='/f/"x',
                                  thumb_url="/f/x/thumb", page_url=None, mode="live")
html = str(_preview.thumb_with_original_link(ctx))
check("preview helper: label and url escaped", "<i>" not in html and 'href="/f/&#34;x"' in html, html)


print("\n--- real /f/<slug> and object pages ---")


def upload(name, data, ctype="application/octet-stream"):
    r = client.post("/api/upload", files={"file": (name, data, ctype)})
    assert r.status_code == 200, (name, r.status_code, r.text[:200])
    return r.json().get("slug") or r.json().get("object", {}).get("slug")


S = {
    "pdf": upload("Field Notes 2019.pdf", PDF, "application/pdf"),
    "png": upload("pic.png", img_bytes("PNG"), "image/png"),
    "txt": upload("notes.txt", b"plain words only\n", "text/plain"),
    "stl": upload("Clod bracket v2.stl", STL, "model/stl"),
    "zip": upload("bundle.zip", ZIP, "application/zip"),
}
WANT = {"pdf": "inline", "png": "inline", "txt": "inline", "stl": "attachment", "zip": "attachment"}
for key, slug in S.items():
    for who, c in (("token", client), ("anon", anon)):
        r = c.get(f"/f/{slug}")
        disp = r.headers.get("content-disposition", "")
        check(f"/f {key}/{who}: 200 {WANT[key]}", r.status_code == 200 and disp.startswith(WANT[key] + ";"), (r.status_code, disp))
        check(f"/f {key}/{who}: nosniff, no CSP", r.headers.get("x-content-type-options") == "nosniff"
              and "content-security-policy" not in r.headers, dict(r.headers))
check("/f pdf: content-type application/pdf", client.get(f"/f/{S['pdf']}").headers.get("content-type") == "application/pdf")
check("/f pdf: name kept", quote("Field Notes 2019.pdf") in client.get(f"/f/{S['pdf']}").headers.get("content-disposition", ""))

for key, want_new_tab in (("pdf", True), ("png", True), ("txt", True), ("stl", False), ("zip", False)):
    page = client.get(f"/object/{S[key]}").text
    i = page.find('id="open-full-size"')
    tag = page[page.rfind("<a", 0, i):page.find("</a>", i) + 4] if i >= 0 else ""
    if want_new_tab:
        check(f"object page {key}: 'Open full size' in a new tab", 'target="_blank"' in tag and "Open full size" in tag, tag)
    else:
        check(f"object page {key}: 'Download original', no new tab", "target=" not in tag and "Download original" in tag, tag)
    # whichever "... original <Type>" button the preview shows must agree too
    for marker in ("View original", "Download original"):
        j = page.find(marker, page.find('class="detail-preview"'))
        if j >= 0 and j < i:
            a = page[page.rfind("<a", 0, j):j]
            check(f"object page {key}: preview '{marker}' button target", ('target="_blank"' in a) == (marker == "View original"), a)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("all passed")
