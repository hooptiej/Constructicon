"""Apple VPP / Apps and Books content tokens (issue #602): .vpptoken.

The file you download from Apple Business Manager and load into an MDM so it can assign app
licences. MSPs keep one per client and it expires yearly, so "whose token is this, and when does it
expire?" is the thing worth keeping.

Restricted type (restricted=True, like certkey): the file is a LIVE credential, whoever holds it can
manage the organisation's app licences. Admin-only on every door (#467/#557), never exported. The
file is stored whole; nothing is ever derived from it that could carry the secret.

The format is base64-encoded JSON, roughly {"token": ..., "expDate": ..., "orgName": ...}. Only
the **organisation name** and the **expiry** are read out. The `token` value is never copied into
type_metadata, extracted_text, search, the audit log, previews, properties or the MCP output: the
parser below returns just those two fields and never binds the secret to anything that outlives
the call. No OCR, no embedding, no thumbnail (all off for this spec).

A file that doesn't decode or parse is NOT refused: it is kept as an opaque restricted file with a
note ("couldn't read the token's details"). Expiry is judged at view time from the stored date, so
"expires soon" (inside 30 days) and "expired" stay true as time passes.

content_date is deliberately NOT set from the expiry: core/embedded_metadata.py rejects any
content_date in the future (a clock-sentinel guard), and an expiry isn't the moment the content
happened anyway. The expiry lives in type_metadata and is shown on the item.
"""

import base64
import binascii
import datetime
import json
import re
from pathlib import Path

from .. import datefmt, storage
from . import _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "vpptoken_stats"
MAX_BYTES = 256 * 1024  # a real token file is well under 2 KB; never read more than this
MAX_ORG_CHARS = 200
SOON_DAYS = 30

EXTENSIONS = frozenset({".vpptoken"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TZ_NO_COLON = re.compile(r"([+-]\d\d)(\d\d)$")


def _decode_json(raw):
    """The token file's JSON object, or None. base64 (standard or urlsafe, padding optional), then
    UTF-8, then JSON. Errors are reduced to None: their text could quote file content."""
    text = raw.strip()
    if not text:
        return None
    padded = text + b"=" * (-len(text) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            blob = decoder(padded)
        except (binascii.Error, ValueError):  # silent-ok: trying each base64 alphabet; not decodable = unreadable, noted on the item
            continue
        try:
            obj = json.loads(blob.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):  # silent-ok: not JSON = unreadable; the message would quote file content, so it is not kept
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _expiry_epoch(value):
    """UTC unix seconds for Apple's expDate ("2027-10-07T18:30:00+0000", "...Z", a bare date, or an
    epoch number), else None."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
        return epoch / 1000 if epoch > 1e11 else epoch  # milliseconds -> seconds
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    s = _TZ_NO_COLON.sub(r"\1:\2", s)
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:  # silent-ok: an expDate in a shape we don't know = no expiry shown; the value isn't echoed
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def parse_token_file(path):
    """{"parsed": bool, "org": str, "expires": epoch or None}: the ONLY fields ever taken from the
    file. The token value is deliberately never read into the result."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES)
    except OSError as e:
        print(f"VPP token read failed for {Path(path).name}: {type(e).__name__}", flush=True)
        return {"parsed": False}
    obj = _decode_json(raw)
    if obj is None:
        return {"parsed": False}
    org = obj.get("orgName") or obj.get("organizationName") or obj.get("org") or ""
    org = _CONTROL.sub(" ", org).strip()[:MAX_ORG_CHARS] if isinstance(org, str) else ""
    expires = _expiry_epoch(obj.get("expDate") or obj.get("expirationDate") or obj.get("expiry"))
    if not org and expires is None:
        return {"parsed": False}  # valid JSON but not a VPP token we recognise
    return {"parsed": True, "org": org, "expires": expires}


def get_embedded_metadata(path):
    """embedded_metadata_fn: read the organisation and expiry once, at upload (no secret kept)."""
    return {"type_metadata": {STATS_KEY: parse_token_file(path)}}


# ---------------------------------------------------------------- display

def _date(ts):
    return datefmt.iso_day(ts)


def expiry_status(expires, now=None):
    """(code, label) for a stored expiry: "expired" / "soon" / "ok" / "unknown"."""
    if expires is None:
        return "unknown", "expiry unknown"
    now = datetime.datetime.now(datetime.timezone.utc).timestamp() if now is None else now
    days = int((expires - now) // 86400)
    if expires < now:
        return "expired", f"EXPIRED on {_date(expires)}"
    if days <= SOON_DAYS:
        return "soon", f"expires soon: {_date(expires)} ({days} days left)"
    return "ok", f"expires {_date(expires)} ({days} days left)"


def get_properties(row):
    """properties_fn: organisation, expiry and its status (computed now). The first property is the
    one the admin list shows as a hint. {} on failure."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if stats is None and row.get("stored_filename"):
            path = storage.path_for(row["stored_filename"])
            if path.exists():
                stats = parse_token_file(path)
        if stats is None:
            return {}
        if not stats.get("parsed"):
            return {
                "Token": "Apple Business Manager content token (VPP), details unreadable",
                "Note": "Couldn't read the token's details. Kept as an opaque restricted file.",
            }
        _, status = expiry_status(stats.get("expires"))
        org = stats.get("org") or "unknown organization"
        return {"Token": f"{org}: {status}", "Organization": org, "Status": status}
    except Exception as e:
        print(f"VPP token properties failed for {row.get('slug')}: {type(e).__name__}", flush=True)
        return {}


def preview(ctx):
    """preview_fn: file icon only, never anything read from the file (restricted items are never
    exported, so export mode never runs)."""
    return _preview.file_icon(ctx)


register(ObjectTypeSpec(
    key="vpptoken",
    label="Apple VPP token",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    caption_capable=False,
    extensions=EXTENSIONS,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,  # organisation + expiry only, once at upload
    preview_fn=preview,
    restricted=True,  # a live credential: admin-only, never exported (#443, #467)
    badge_icon="\U0001F34E",  # red apple
    badge_text="VPP",
))
