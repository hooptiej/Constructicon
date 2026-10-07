#!/usr/bin/env python3
"""Self-contained check for the email object type (#582).

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient. The
.eml fixtures are generated here with the stdlib `email` package at run time (nothing binary is
committed); attachments are harmless text and a tiny PNG, never anything executable or archived.

    python scripts/test_email_type.py

Exits 1 if any check fails.
"""
import base64
import email.header
import email.message
import email.policy
import email.utils
import os
import re
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("emailtype582-")
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from core import db, object_types  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# 1x1 transparent PNG: a harmless attachment
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
TOKEN = "zqmailneedle"


def base(subject, frm="Alice Sender <alice@example.com>", to="Bob <bob@example.com>", date="Tue, 01 Oct 2024 10:30:00 -0600"):
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    if date:
        m["Date"] = date
    m["Message-ID"] = "<fixture-" + re.sub(r"\W", "", subject)[:20] + "@example.com>"
    return m


def make_plain():
    m = base("Plain note about " + TOKEN)
    m["Cc"] = "Carol <carol@example.com>"
    m.set_content(f"Hello Bob,\nthe {TOKEN} reservation is approved.\n<b>not bold</b>\n")
    return m


def make_html_only():
    m = base("HTML only")
    html = ('<html><head><title>x</title><style>.a{}</style></head><body><p>Hi <b>there</b> htmlneedle</p>'
            '<script>alert(1)</script><img src="http://tracker.example.com/pixel.gif">'
            '<a href="javascript:alert(2)">click</a></body></html>')
    m.set_content(html, subtype="html")
    return m


def make_multipart():
    m = base("Multipart with attachments")
    m.set_content("See attached notes and picture.\n")
    m.add_attachment(b"just some notes\n", maintype="text", subtype="plain", filename="notes.txt")
    m.add_attachment(PNG, maintype="image", subtype="png", filename="dot.png")
    return m


def make_nonascii():
    # RFC 2047 encoded-words, built explicitly so the fixture is pure ASCII on the wire
    enc = lambda s: email.header.Header(s, "utf-8").encode()  # noqa: E731
    m = base(enc("Café résumé — 日本語 test"),
             frm=email.utils.formataddr(("Zoë Müller", "zoe@example.com"), charset="utf-8"),
             to=email.utils.formataddr(("Søren", "soren@example.com"), charset="utf-8"))
    m.set_content("Body with ünïcode and 日本語 text.\n")
    return m


def make_baddate():
    m = base("Garbage date", date=None)
    m["Date"] = "placeholder"
    m.set_content("Body of the garbage-date message.\n")
    return m


def garble_date(data):
    """EmailMessage refuses to write a nonsense Date, so patch the serialized bytes."""
    out, n = re.subn(rb"(?m)^Date:.*$", b"Date: not a real date at all", data, count=1)
    assert n == 1
    return out


FIXTURES = {
    "plain.eml": make_plain, "htmlonly.eml": make_html_only, "multi.eml": make_multipart,
    "nonascii.eml": make_nonascii, "baddate.eml": make_baddate,
}

db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402

client = TestClient(webapp.app, raise_server_exceptions=False)
with client:
    pass

check("email type is registered", object_types.get_object_type("email").key == "email")
check("email type is not restricted by default", not object_types.get_object_type("email").restricted)

slugs = {}
for name, make in FIXTURES.items():
    data = make().as_bytes(policy=email.policy.default)
    if name == "baddate.eml":
        data = garble_date(data)
    r = client.post("/api/upload", files={"file": (name, data, "message/rfc822")})
    ok = r.status_code == 200
    check(f"{name} uploads", ok, (r.status_code, r.text[:200]))
    if ok:
        slugs[name] = r.json().get("slug") or r.json().get("object", {}).get("slug")
    check(f"{name} is typed email", ok and db.get_by_slug(slugs[name])["media_type"] == "email",
          ok and db.get_by_slug(slugs[name])["media_type"])


def row(name):
    return db.get_by_slug(slugs[name])


def props(name):
    spec = object_types.get_object_type("email")
    return spec.properties_fn(row(name))


def render(name, mode):
    spec = object_types.get_object_type("email")
    ctx = object_types.PreviewContext(item=row(name), media_url="/f/x", thumb_url=None, page_url=None, mode=mode)
    return str(object_types.render_preview(spec, ctx))


print("\n--- properties per fixture ---")
for n in FIXTURES:
    print(n, "->", props(n))
    print("   content_date:", row(n)["content_date"], "| display_name:", row(n)["display_name"], "| ocr_status:", row(n)["ocr_status"])

p = props("plain.eml")
check("plain: sender", "alice@example.com" in p.get("From", ""), p)
check("plain: recipients (To + Cc)", "bob@example.com" in p.get("To", "") and "carol@example.com" in p.get("Cc", ""), p)
check("plain: subject", TOKEN in p.get("Subject", ""), p)
check("plain: date", "2024" in p.get("Date", ""), p)
check("plain: message-id", p.get("Message-ID", "").startswith("<fixture-"), p)
check("plain: no attachments", p.get("Attachments") == "none", p)
check("plain: content_date from Date header (2024-10-01 16:30Z)", row("plain.eml")["content_date"] == 1727800200.0, row("plain.eml")["content_date"])
check("plain: subject becomes the title", TOKEN in (row("plain.eml")["display_name"] or ""), row("plain.eml")["display_name"])

hits = [r["slug"] for r in db.search(query=TOKEN)]
check("plain: body searchable via db.search", slugs["plain.eml"] in hits, hits)
check("plain: extracted_text holds subject and body", TOKEN in row("plain.eml")["extracted_text"] and "approved" in row("plain.eml")["extracted_text"])
check("html-only: body text searchable", slugs["htmlonly.eml"] in [r["slug"] for r in db.search(query="htmlneedle")])
check("html-only: extracted_text has no tags or script", not re.search(r"<\s*(script|b|p|img)\b|alert\(", row("htmlonly.eml")["extracted_text"]),
      row("htmlonly.eml")["extracted_text"])

# --- html-only preview is inert ---
for mode in ("live", "export"):
    h = render("htmlonly.eml", mode)
    check(f"html-only {mode}: no <script", "<script" not in h.lower(), h[:300])
    check(f"html-only {mode}: no <img", "<img" not in h.lower())
    check(f"html-only {mode}: no img src=http", not re.search(r"<img[^>]+src\s*=\s*[\"']?http", h, re.I))
    check(f"html-only {mode}: no remote tracker url", "tracker.example.com" not in h)
    check(f"html-only {mode}: body text present", "htmlneedle" in h)
    # every '<' in the output must be one of our own few structural tags, never from the message
    tags = set(re.findall(r"</?([a-z0-9]+)", h))
    check(f"html-only {mode}: only structural tags", tags <= {"div", "table", "tbody", "tr", "th", "td", "h4", "span", "pre", "p", "ul", "li"}, tags)

h = render("plain.eml", "live")
check("plain preview escapes literal <b> in the body", "&lt;b&gt;not bold&lt;/b&gt;" in h and "<b>" not in h, h[:400])

# --- hostile header content is escaped ---
hostile = base("<script>alert(1)</script>", frm='"<img src=x onerror=alert(1)>" <h@example.com>')
hostile.set_content("x")
r = client.post("/api/upload", files={"file": ("hostile.eml", hostile.as_bytes(policy=email.policy.default), "message/rfc822")})
hs = r.json().get("slug") or r.json().get("object", {}).get("slug")
slugs["hostile.eml"] = hs
hh = render("hostile.eml", "live")
check("hostile headers escaped in preview", "<script" not in hh and "<img" not in hh, hh[:300])
page = client.get(f"/object/{hs}")
check("hostile: object page renders 200 with no raw script/img", page.status_code == 200 and "<script>alert(1)" not in page.text
      and "<img src=x onerror" not in page.text, page.status_code)

# --- attachments ---
p = props("multi.eml")
check("multipart: attachments listed with names", "notes.txt" in p.get("Attachments", "") and "dot.png" in p.get("Attachments", ""), p)
check("multipart: attachment count 2", p.get("Attachments", "").startswith("2:"), p)
check("multipart: attachment sizes and types", f"({len(PNG)} B, image/png)" in p.get("Attachments", ""), p)
h = render("multi.eml", "live")
check("multipart preview lists attachments and notes they are not extracted", "notes.txt" in h and "dot.png" in h and "not extracted" in h)
check("multipart: attachment bytes not in extracted_text", "just some notes" not in row("multi.eml")["extracted_text"])

# --- non-ASCII (RFC 2047) ---
p = props("nonascii.eml")
check("nonascii: subject decoded", "Café résumé" in p.get("Subject", "") and "日本語" in p.get("Subject", ""), p)
check("nonascii: From decoded", "Zoë Müller" in p.get("From", ""), p)
check("nonascii: To decoded", "Søren" in p.get("To", ""), p)
check("nonascii: preview shows decoded text", "Zoë" in render("nonascii.eml", "live"))
check("nonascii: body searchable", slugs["nonascii.eml"] in [r["slug"] for r in db.search(query="ünïcode")])

# --- garbage date ---
p = props("baddate.eml")
check("baddate: uploaded and shows its raw Date text", p.get("Date") == "not a real date at all", p)
check("baddate: no content_date, no crash", row("baddate.eml")["content_date"] is None, row("baddate.eml")["content_date"])
check("baddate: other fields intact", "alice@example.com" in p.get("From", "") and p.get("Subject") == "Garbage date", p)

# --- a file that is not an email at all must not break the upload ---
r = client.post("/api/upload", files={"file": ("junk.eml", b"\x00\x01\x02 not an email \xff\xfe", "message/rfc822")})
check("garbage .eml still uploads", r.status_code == 200, (r.status_code, r.text[:200]))
js = r.json().get("slug") or r.json().get("object", {}).get("slug")
spec = object_types.get_object_type("email")
junk_ctx = object_types.PreviewContext(item=db.get_by_slug(js), media_url="/f/x", thumb_url=None, page_url=None)
check("garbage .eml previews without raising", object_types.render_preview(spec, junk_ctx) is not None)
check("garbage .eml object page 200", client.get(f"/object/{js}").status_code == 200)

# --- .msg is refused cleanly ---
r = client.post("/api/upload", files={"file": ("outlook.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "application/octet-stream")})
check(".msg refused cleanly with 400 Unsupported file type", r.status_code == 400 and "Unsupported file type: .msg" in r.text, (r.status_code, r.text[:200]))

# --- live object pages render ---
for n in ("plain.eml", "htmlonly.eml", "multi.eml", "nonascii.eml", "baddate.eml"):
    pr = client.get(f"/object/{slugs[n]}")
    check(f"{n}: /object page 200", pr.status_code == 200, pr.status_code)
pr = client.get(f"/object/{slugs['htmlonly.eml']}").text
check("html-only /object page has no script from the message or tracker", "alert(1)" not in pr and "tracker.example.com" not in pr)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASSED")
