"""Session + email-OTP auth.

In-memory stores — fine for a single-process sandbox, needs to move to
SQLite/Redis before this runs with more than one worker.

Any email can request a code — there's no domain allowlist. Email-OTP
works end to end except the actual send — no SMTP credentials configured
yet, so the code is shown on the verify page instead of emailed (dev mode).
Real auth (SSO or otherwise) still needs to be wired up before this is
anything more than a sandbox login.
"""

import hashlib
import secrets
import time

SESSION_TTL = 60 * 60 * 12
CODE_TTL = 60 * 10
PRESENCE_WINDOW = 90  # seconds since last heartbeat to still count as "online"

sessions = {}
pending_codes = {}
presence = {}


def request_code(email):
    code = f"{secrets.randbelow(1_000_000):06d}"
    pending_codes[email.lower()] = {"code": code, "expires": time.time() + CODE_TTL}
    return code


def verify_code(email, code):
    entry = pending_codes.get(email.lower())
    if entry is None or entry["expires"] < time.time():
        return False
    if not secrets.compare_digest(entry["code"], code):
        return False
    del pending_codes[email.lower()]
    return True


def create_session(email):
    token = secrets.token_urlsafe(32)
    sessions[token] = {"email": email, "expires": time.time() + SESSION_TTL}
    return token


def get_session(token):
    entry = sessions.get(token)
    if entry is None or entry["expires"] < time.time():
        return None
    return entry


def touch_presence(email):
    presence[email] = time.time()


def list_online():
    now = time.time()
    return sorted(email for email, last_seen in presence.items() if now - last_seen <= PRESENCE_WINDOW)


# --- API tokens ---
# Unlike browser sessions, these are for unattended clients (the desktop
# uploader) that can't do an interactive login, and need to keep working
# across weeks and app restarts — so they're durable (stored in SQLite via
# core.db), not the in-memory `sessions` dict above.

API_TOKEN_PREFIX = "ir_"


def generate_api_token():
    return API_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_api_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()
