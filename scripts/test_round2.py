#!/usr/bin/env python3
"""Round 2 of the work-install shakedown: #583, #587 items 1-4, #581.

Throwaway DB + storage (scripts/_testenv.py), the real FastAPI app through TestClient, no server:

    python scripts/test_round2.py

  #583  a refused request leaves "<code>: <message>" (or the plain HTTPException detail) in
        audit_log.error_detail; a stream, a file or a big body is never buffered; redaction follows
        the route's body rules; an unhandled 500 still records the exception text.
  #587.1 a zero-byte / whitespace-only file settles OCR as done (empty text); the migration re-settles
        old failed zero-byte rows (and only those).
  #587.2 near-zero-entropy hashes are "no hash": never stored for a blank image, ignored when comparing.
  #587.3 a hand-built PE with a big overlay (or an archive marker in it) suggests Installer, says why,
        and stores overlay_bytes; a plain one still suggests Standalone app.
  #587.4 a PE with an Authenticode certificate table and no CompanyName shows "Signed by <CN>".
  #581  app_name / app_logo: validated, undoable, shown in header, home title, tab title (DEV- kept)
        and logo; unset = exactly the old output; /brand/logo.png serves with no assets/ mounted.
Exits 1 if any check fails.
"""

import asyncio
import datetime
import json
import os
import sqlite3
import struct
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("round2-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # native cairo is not needed here

from PIL import Image  # noqa: E402
from starlette.responses import JSONResponse, StreamingResponse  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, install_config, ocr, paths, similarity, storage  # noqa: E402
_testenv.assert_isolated()
from core.object_types import _pe, application  # noqa: E402
from web import app as webapp, middleware  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def q(sql, *args):
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def last_audit(path):
    rows = q("SELECT * FROM audit_log WHERE path = ? ORDER BY id DESC LIMIT 1", path)
    return rows[0] if rows else None


client = _testenv.client(webapp.app, raise_server_exceptions=False)
with client:  # real startup (init_db + migrations)
    pass

# ============================================================================================
# #583: the audit log says why
# ============================================================================================
r = client.post("/api/upload", files={"file": ("thing.xyz", b"abc", "application/octet-stream")})
row = last_audit("/api/upload")
check("583: a refused upload is a 400", r.status_code == 400, (r.status_code, r.text[:200]))
check("583: its reason is in error_detail",
      row and row["error_detail"] == "bad_request: Unsupported file type: .xyz", row and row["error_detail"])

r = client.post("/api/image/nosuchslug", data={"display_name": "x"})
row = last_audit("/api/image/nosuchslug")
check("583: a 404 leaves 'not_found: ...'", r.status_code == 404 and row and (row["error_detail"] or "").startswith("not_found: "),
      (r.status_code, row and row["error_detail"]))

r = client.post("/api/install-config", data={"owner_name": "x"})  # wrong content type -> HTTPException 415
row = last_audit("/api/install-config")
check("583: a plain HTTPException's detail is recorded (415 content type)",
      r.status_code == 415 and row and "Content-Type must be application/json" in (row["error_detail"] or ""),
      (r.status_code, row and row["error_detail"]))

r = client.post("/api/install-config", json={"nope": "x"})
row = last_audit("/api/install-config")
check("583: a service AppError keeps its own code",
      r.status_code == 400 and row["error_detail"].startswith("bad_install_key: "), row["error_detail"])

r = client.post("/api/install-config", json={"owner_name": "Round Two"})
row = last_audit("/api/install-config")
check("583: a success records no reason", r.status_code == 200 and row["error_detail"] is None, row["error_detail"])

# redaction: a route whose body is logged as redacted keeps only the code
r = client.post("/api/settings", json={"key": "youtube_data_api_key", "value": ""})
row = last_audit("/api/settings")
if r.status_code >= 400:
    check("583: a redacted route's reason keeps only the code (no message text)",
          row["error_detail"] and ": " not in row["error_detail"], row["error_detail"])
else:
    check("583: (settings probe was not refused; unit check below covers redaction)", True)
from web import request_guard  # noqa: E402
check("583: redact_audit_error: 'name_only' route keeps the code only",
      request_guard.redact_audit_error("/api/settings", "bad_request: key=SECRET123 is wrong") == "bad_request")
check("583: redact_audit_error: 'none' route with no code -> [REDACTED]",
      request_guard.redact_audit_error("/api/account/desktop-app-build", "oops SECRET") == request_guard.REDACTED)
check("583: redact_audit_error: ordinary route unchanged",
      request_guard.redact_audit_error("/api/upload", "bad_request: Unsupported file type: .xyz")
      == "bad_request: Unsupported file type: .xyz")

# unit: shapes, and that streams / files / big bodies are never read
check("583: error shape -> 'code: message'",
      middleware.reason_from_error_body({"ok": False, "error": {"code": "forbidden", "message": "No."}, "detail": "No."}) == "forbidden: No.")
check("583: plain detail -> the detail", middleware.reason_from_error_body({"detail": "Not Found"}) == "Not Found")
check("583: a validation list detail is not a plain string (error shape carries it)",
      middleware.reason_from_error_body({"detail": [{"msg": "x"}]}) is None)
check("583: unrecognised body -> None", middleware.reason_from_error_body({"x": 1}) is None and middleware.reason_from_error_body([]) is None)


class _Touched(Exception):
    pass


async def _boom():
    raise _Touched()
    yield b""  # pragma: no cover


async def _unit():
    # a stream with no Content-Length, JSON type, status 500: must not be iterated
    s = StreamingResponse(_boom(), status_code=500, media_type="application/json")
    got = await middleware._error_reason(s)
    # a JSON error that is too big: not read
    big = JSONResponse({"detail": "x" * (middleware.ERROR_BODY_MAX_BYTES + 10)}, status_code=400)
    got_big = await middleware._error_reason(big)
    # a small one: read and the response still replays the same bytes
    small = JSONResponse({"error": {"code": "bad_request", "message": "boom"}}, status_code=400)
    small.body_iterator = _aiter([small.body])  # what BaseHTTPMiddleware hands us
    got_small = await middleware._error_reason(small)
    replay = b"".join([c async for c in small.body_iterator])
    # a 200 and a non-JSON error are left alone
    ok = JSONResponse({"detail": "fine"}, status_code=200)
    ok.body_iterator = _aiter([ok.body])
    html = StreamingResponse(_aiter([b"<p>x</p>"]), status_code=404, media_type="text/html",
                             headers={"content-length": "8"})
    return got, got_big, got_small, replay, await middleware._error_reason(ok), await middleware._error_reason(html)


async def _aiter(items):
    for i in items:
        yield i

try:
    got, got_big, got_small, replay, got_ok, got_html = asyncio.run(_unit())
    check("583: a stream (no Content-Length) is never buffered", got is None)
    check("583: an oversized JSON error is not read", got_big is None)
    check("583: a small JSON error is read, and the body replays intact",
          got_small == "bad_request: boom" and json.loads(replay) == {"error": {"code": "bad_request", "message": "boom"}},
          (got_small, replay))
    check("583: a 200 and a non-JSON error are not read", got_ok is None and got_html is None)
except _Touched:
    check("583: a stream (no Content-Length) is never buffered", False, "iterator was consumed")

# an unhandled exception still records its text
@webapp.app.post("/api/_round2_boom")
async def _boom_route():
    raise RuntimeError("kaboom for the audit log")

r = client.post("/api/_round2_boom")
row = last_audit("/api/_round2_boom")
check("583: an unhandled 500 still records the exception text",
      r.status_code == 500 and row and row["status_code"] == 500 and "kaboom for the audit log" in (row["error_detail"] or ""),
      (r.status_code, row and row["error_detail"]))

# Admin's Recent activity shows it
admin_html = client.get("/admin").text
check("583: Recent activity renders the reason on its own line", "audit-log-reason" in admin_html)
feed = client.get("/api/audit-log?limit=50").json()
check("583: the audit feed carries error_detail",
      any((e.get("error_detail") or "").startswith("bad_request: Unsupported file type") for e in feed))

# ============================================================================================
# #587 item 1: empty files are done, not failed
# ============================================================================================
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()


def mkfile(slug, data, ext, media_type):
    sf = f"{slug}{ext}"
    (paths.storage_dir() / sf).write_bytes(data)
    db.insert_upload(slug, sf, sf, "tester", media_type=media_type, file_size=len(data), ocr_status="pending")
    return slug


EMPTY = mkfile("emptyfile", b"", ".py", "code")
BLANKWS = mkfile("whitespace", b" \n\t\r\n  \n", ".txt", "text")
EMPTYIMG = mkfile("emptyimg", b"", ".png", "image")
REAL = mkfile("realtext", b"print('hi')\n", ".py", "code")
for s in (EMPTY, BLANKWS, EMPTYIMG, REAL):
    ocr.run_ocr(s)
st = {s: (db.get_by_slug(s)["ocr_status"], db.get_by_slug(s)["extracted_text"]) for s in (EMPTY, BLANKWS, EMPTYIMG, REAL)}
check("587.1: a zero-byte file settles done with empty text", st[EMPTY] == ("done", ""), st[EMPTY])
check("587.1: a whitespace-only file settles done", st[BLANKWS][0] == "done" and not (st[BLANKWS][1] or "").strip(), st[BLANKWS])
check("587.1: a zero-byte image settles done too (not 'failed')", st[EMPTYIMG][0] == "done", st[EMPTYIMG])
check("587.1: a file with content still gets its text", st[REAL][0] == "done" and "print" in (st[REAL][1] or ""), st[REAL])

# the migration: failed + really zero bytes -> done; failed + non-empty / missing file -> untouched
M_EMPTY = mkfile("mig_empty", b"", ".zip", "archive")
M_REAL = mkfile("mig_real", b"data", ".bin", "any")
M_MISSING = mkfile("mig_missing", b"", ".bin", "any")
(paths.storage_dir() / "mig_missing.bin").unlink()
for s in (M_EMPTY, M_REAL, M_MISSING):
    db.set_ocr_status(s, "failed")
with db.transaction():
    db._mig_empty_file_ocr_done_587()
after = {s: db.get_by_slug(s)["ocr_status"] for s in (M_EMPTY, M_REAL, M_MISSING)}
check("587.1 migration: a failed zero-byte row becomes done", after[M_EMPTY] == "done", after)
check("587.1 migration: other failed rows are left alone", after[M_REAL] == "failed" and after[M_MISSING] == "failed", after)
with db.transaction():
    db._mig_empty_file_ocr_done_587()
check("587.1 migration: idempotent", db.get_by_slug(M_EMPTY)["ocr_status"] == "done" and db.get_by_slug(M_REAL)["ocr_status"] == "failed")
check("587.1 migration: registered in MIGRATIONS", "empty_file_ocr_done_587" in [n for n, _ in db.MIGRATIONS])

# ============================================================================================
# #587 item 2: degenerate perceptual hashes are "no hash"
# ============================================================================================
for h in ("0000000000000000", "8000000000000000", "0000000000000001", "ffffffffffffffff", "7fffffffffffffff", "", None, "zz"):
    check(f"587.2: {h!r} is degenerate", similarity.is_degenerate_hash(h))
for h in ("d1c3a5b296e1f078", "a5a5a5a5a5a5a5a5", "ffff00000000ffff"):
    check(f"587.2: {h} is a real hash", not similarity.is_degenerate_hash(h))

blank1, blank2 = paths.storage_dir() / "blank1.png", paths.storage_dir() / "blank2.png"
Image.new("RGB", (200, 120), (255, 255, 255)).save(blank1)
im = Image.new("RGB", (200, 120), (250, 250, 250))
im.putpixel((3, 3), (249, 249, 249))
im.save(blank2)
check("587.2: a uniform image gets no hash", similarity.compute_perceptual_hash(blank1) is None)
check("587.2: a nearly uniform image gets no hash", similarity.compute_perceptual_hash(blank2) is None)
real_img = paths.storage_dir() / "real.png"
Image.effect_noise((200, 120), 60).convert("RGB").save(real_img)
real_hash = similarity.compute_perceptual_hash(real_img)
check("587.2: a real image keeps its hash", real_hash and not similarity.is_degenerate_hash(real_hash), real_hash)

# comparison ignores stored degenerate hashes: two rows that already hold 8000000000000000
A, B, C, D = (mkfile(n, b"x", ".png", "image") for n in ("sim_a", "sim_b", "sim_c", "sim_d"))
for s in (A, B):
    db.set_perceptual_hash(s, "8000000000000000")
for s in (C, D):
    db.set_perceptual_hash(s, "d1c3a5b296e1f078")
check("587.2: two blank-hash rows do NOT match each other", [m["slug"] for m in similarity.find_similar(A)] == [],
      similarity.find_similar(A))
check("587.2: two real identical hashes still match", [m["slug"] for m in similarity.find_similar(C)] == [D],
      similarity.find_similar(C))

# ============================================================================================
# #587 items 3 + 4: PE overlay and the Authenticode signer (hand-built PE files)
# ============================================================================================


def build_pe(overlay=b"", cert_der=None):
    """A minimal valid 32-bit GUI PE (one 0x200-byte section, no version resource) followed by
    `overlay`, then optionally a WIN_CERTIFICATE (PKCS#7) wired into data directory 4."""
    section = b"\x90" * 0x200
    opt = struct.pack("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII",
                      0x10B, 1, 0, 0x200, 0, 0, 0x1000, 0x1000, 0x2000, 0x400000, 0x1000, 0x200,
                      4, 0, 0, 0, 4, 0, 0, 0x2000, 0x200, 0, 2, 0, 0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dirs = [(0, 0)] * 16
    body = overlay
    file_len_before_cert = 0x400 + len(overlay)
    if cert_der is not None:
        pad = (-file_len_before_cert) % 8
        body += b"\x00" * pad
        wc = struct.pack("<IHH", 8 + len(cert_der), 0x200, 2) + cert_der
        wc += b"\x00" * ((-len(wc)) % 8)
        dirs[4] = (file_len_before_cert + pad, len(wc))
        body += wc
    dd = b"".join(struct.pack("<II", a, b) for a, b in dirs)
    coff = struct.pack("<HHIIIHH", 0x14C, 1, 1700000000, 0, 0, 96 + len(dd), 0x102)
    sect = struct.pack("<8sIIIIIIHHI", b".text", 0x200, 0x1000, 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
    header = b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x40) + b"PE\x00\x00" + coff + opt + dd + sect
    header += b"\x00" * (0x200 - len(header))
    return header + section + body


def make_cert_blob():
    """A PKCS#7 (DER) signature carrying a chain: root CA -> 'Acme Widgets LLC' (code signing) plus
    an unrelated 'Fake Timestamp Authority' leaf (time-stamping) from the same root."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.datetime.now(datetime.timezone.utc)

    def mk(cn, org, issuer_cert, issuer_key, eku, ca=False):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.ORGANIZATION_NAME, org)])
        b = (x509.CertificateBuilder().subject_name(name)
             .issuer_name(issuer_cert.subject if issuer_cert else name).public_key(key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=30))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if eku:
            b = b.add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        return b.sign(issuer_key or key, hashes.SHA256()), key

    root, root_key = mk("Round2 Test Root", "Round2 Test Root", None, None, None, ca=True)
    leaf, leaf_key = mk("Acme Widgets LLC", "Acme Widgets", root, root_key, ExtendedKeyUsageOID.CODE_SIGNING)
    tsa, _ = mk("Fake Timestamp Authority", "TSA Inc", root, root_key, ExtendedKeyUsageOID.TIME_STAMPING)
    return (pkcs7.PKCS7SignatureBuilder().set_data(b"payload").add_signer(leaf, leaf_key, hashes.SHA256())
            .add_certificate(root).add_certificate(tsa)
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature]))


def pe_file(name, data):
    p = paths.storage_dir() / name
    p.write_bytes(data)
    return p


class _Cand:
    def __init__(self, path, filename):
        self.path, self.filename = path, filename


def ask(path, filename):
    """(question text, {option key: option}) from the exe_classification decision pre_store queues."""
    d = application.pre_store(_Cand(path, filename)).decision
    return d["question"], {o["key"]: o for o in d["options"]}


plain = pe_file("plain.exe", build_pe())
check("587.3 fixture: the hand-built PE is a PE", _pe.is_pe(plain))
info = _pe.overlay_info(plain)
check("587.3: a PE with no overlay has overlay_bytes 0", info["overlay_bytes"] == 0 and not _pe.looks_like_self_extractor(info), info)
text, opts = ask(plain, "tool.exe")
check("587.3: a plain exe still suggests Standalone app",
      opts["application"]["suggested"] and not opts["installer"]["suggested"] and "payload" not in text, text)

big = pe_file("bigoverlay.exe", build_pe(overlay=os.urandom(300 * 1024)))
info = _pe.overlay_info(big)
check("587.3: a 300 KB overlay on a 1 KB program is counted", 300 * 1024 - 8 <= info["overlay_bytes"] <= 300 * 1024, info)
check("587.3: ... and is over 50% of the file", info["overlay_ratio"] > 0.9, info)
text, opts = ask(big, "BrMain4904.exe")
check("587.3: a big overlay suggests Installer", opts["installer"]["suggested"] and not opts["application"]["suggested"], opts)
check("587.3: the question says why", "embedded payload" in text and "KB" in text, text)
meta = application.get_embedded_metadata(big)["type_metadata"][application.STATS_KEY]
check("587.3: overlay_bytes is stored in application_stats", meta.get("overlay_bytes") == info["overlay_bytes"], meta)

# an archive marker wins even when the overlay is a small share of the file
for marker, label in ((b"7z\xbc\xaf\x27\x1c", "7z"), (b"PK\x03\x04", "zip"), (b"MSCF", "cab")):
    f = pe_file(f"marker_{label}.exe", build_pe(overlay=marker + b"\x00" * 64))
    inf = _pe.overlay_info(f)
    text, opts = ask(f, "app.exe")
    check(f"587.3: a {label} marker in a small overlay suggests Installer ({label})",
          inf["overlay_signature"] == label and inf["overlay_ratio"] < 0.5
          and opts["installer"]["suggested"] and label in text, (inf, text))
nsis = pe_file("nsis_like.exe", build_pe(overlay=b"\x00" * 100 + b"\xef\xbe\xad\xdeNullsoftInst" + b"\x00" * 50))
check("587.3: an NSIS marker deeper in the overlay is found", _pe.overlay_info(nsis)["overlay_signature"] == "NSIS")
check("587.3: a name hint alone still works (setup in the name)",
      ask(plain, "Setup.exe")[1]["installer"]["suggested"])

blob = make_cert_blob()
signed = pe_file("signed.exe", build_pe(overlay=b"", cert_der=blob))
check("587.4 fixture: the signed PE parses", _pe.is_pe(signed))
check("587.4: the signer is the code-signing leaf, not the root or the timestamp authority",
      _pe.signer_name(signed) == "Acme Widgets LLC", _pe.signer_name(signed))
fx = _pe.facts(signed)
check("587.4: facts: signed, no CompanyName -> signer filled in, no publisher",
      fx["signed"] and fx.get("signer") == "Acme Widgets LLC" and "publisher" not in fx, fx)
check("587.4: the Authenticode blob is not counted as an overlay payload", fx.get("overlay_bytes", 0) == 0, fx)
props = application.get_properties({"type_metadata": {application.STATS_KEY: fx}, "slug": "x"})
check("587.4: shown as 'Signed by', not 'Publisher'", props.get("Signed by") == "Acme Widgets LLC" and "Publisher" not in props, props)
signed_big = pe_file("signed_big.exe", build_pe(overlay=os.urandom(200 * 1024), cert_der=blob))
fx2 = _pe.facts(signed_big)
check("587.4: a signed exe with a payload: overlay excludes the signature",
      200 * 1024 - 8 <= fx2["overlay_bytes"] <= 200 * 1024 and fx2["signer"] == "Acme Widgets LLC", fx2)
check("587.4: an unsigned exe has no signer", "signer" not in _pe.facts(plain) and _pe.signer_name(plain) is None)
garbage = pe_file("garbage_cert.exe", build_pe(cert_der=b"\x30\x82\x00\x10not a real cert"))
check("587.4: an unreadable certificate table gives no signer (no crash)", _pe.signer_name(garbage) is None)

# ============================================================================================
# #581: app name and logo
# ============================================================================================
install_config.clear_cache()
sys.modules["web.common"].templates.env.globals["is_dev"] = False  # a dev container sets CONSTRUCTICON_ENV=dev
base_html = client.get("/").text
check("581: nothing set -> 'Constructicon' header, bundled logo",
      '<span class="brand-name">Constructicon</span>' in base_html
      and '<img src="/brand/logo.png" alt="Constructicon" width="72" height="72" style="object-fit:contain;display:block;border:none">' in base_html
      and "<title>Constructicon</title>" in base_html, base_html[:400])
check("581: the Coming soon (#11) stub is gone from Admin", "Coming soon" not in client.get("/admin").text)
check("581: the install form lists app_name and app_logo",
      {"app_name", "app_logo"} <= {f["key"] for f in client.get("/api/install-config").json()["fields"]})

r = client.post("/api/install-config", json={"app_name": "Work Archive"})
check("581: saving app_name works", r.status_code == 200, r.text[:200])
batch_name = r.json().get("batch_id")
home = client.get("/").text
check("581: header, home title and tab title read it",
      '<span class="brand-name">Work Archive</span>' in home and '<h1 class="page-title">Work Archive</h1>' in home
      and "<title>Work Archive</title>" in home, home[:300])
adm = client.get("/admin").text
check("581: another page's tab title keeps its '— <name>' shape", "<title>Admin — Work Archive</title>" in adm, adm[:200])
webapp_common = sys.modules["web.common"]
webapp_common.templates.env.globals["is_dev"] = True
check("581: the DEV- prefix is kept on a dev install",
      "<title>DEV-Work Archive</title>" in client.get("/").text and "<title>DEV-Admin — Work Archive</title>" in client.get("/admin").text)
webapp_common.templates.env.globals["is_dev"] = False

# logo: validated against real brand-asset images
r = client.post("/api/install-config", json={"app_logo": "nosuchitem"})
check("581: an unknown logo is refused", r.status_code == 400 and r.json()["error"]["code"] == "bad_install_config", r.text[:200])
PLAINIMG = mkfile("plainlogo", b"x", ".png", "image")
r = client.post("/api/install-config", json={"app_logo": PLAINIMG})
check("581: an image that isn't a brand asset is refused", r.status_code == 400 and "brand asset" in r.json()["error"]["message"], r.text[:200])
LOGO = "brandlogo"
Image.new("RGB", (32, 32), (200, 30, 30)).save(paths.storage_dir() / f"{LOGO}.png")
db.insert_upload(LOGO, f"{LOGO}.png", f"{LOGO}.png", "tester", media_type="image")
from core import items  # noqa: E402
items.update(LOGO, is_brand_asset=True, brand_role="logo")
r = client.post("/api/install-config", json={"app_logo": LOGO})
check("581: a brand-asset image is accepted", r.status_code == 200, r.text[:200])
batch_logo = r.json().get("batch_id")
home = client.get("/").text
check("581: the logo src and alt follow the setting",
      f'<img src="/f/{LOGO}" alt="Work Archive"' in home and '<img src="/brand/logo.png"' not in home)
check("581: /f/<slug> serves the logo", client.get(f"/f/{LOGO}").status_code == 200)

r = client.post("/api/install-config", json={"app_name": "x" * 200})
check("581: an over-long name is refused", r.status_code == 400, r.status_code)

# undo restores the old look exactly
for b in (batch_logo, batch_name):
    ur = client.post(f"/api/changes/{b}/undo")
    check(f"581: undo of {b[:8]} ok", ur.status_code == 200, ur.text[:200])
install_config.clear_cache()
after_undo = client.get("/").text
check("581: after undo the page is the pristine one again",
      '<span class="brand-name">Constructicon</span>' in after_undo and 'src="/brand/logo.png"' in after_undo
      and "<title>Constructicon</title>" in after_undo)
r = client.post("/api/install-config", json={"app_name": "Temp", "app_logo": LOGO})
r = client.post("/api/install-config", json={"app_name": "", "app_logo": ""})
check("581: clearing both returns to the defaults",
      r.status_code == 200 and install_config.app_name() == "Constructicon" and install_config.app_logo_url() == "/brand/logo.png")

# /brand with and without a mounted assets directory
check("581: /brand/logo.png serves (assets mounted)", client.get("/brand/logo.png").status_code == 200)
real_dir = webapp._BRAND_DIR
webapp._BRAND_DIR = real_dir.parent / "no-such-dir"
rb = client.get("/brand/logo.png")
check("581: /brand/logo.png still serves with no assets/ mounted (bundled fallback)",
      rb.status_code == 200 and rb.headers["content-type"] == "image/png" and len(rb.content) > 1000, rb.status_code)
check("581: favicon falls back too", client.get("/brand/favicon-32.png").status_code == 200)
check("581: a file only in assets/brand is 404 when it isn't mounted", client.get("/brand/hooptiej-wordmark.png").status_code == 404)
check("581: path traversal still refused", client.get("/brand/..%2f..%2fcore%2fdb.py").status_code in (400, 404))
webapp._BRAND_DIR = real_dir
check("581: with assets mounted the wordmark is there", client.get("/brand/hooptiej-wordmark.png").status_code == 200)

ctx.__exit__(None, None, None)
print()
print("all passed" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
