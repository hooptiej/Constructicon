"""Sync the client dropdown list — and the name/nickname pairs OCR auto-
tagging matches against — from a Hudu company export.

Run periodically (see the KB note on how this gets triggered) with a JSON
array on the given path — special categories (Unknown, Not Business,
Internal Infrastructure) are untouched.

Each array item is either a plain company-name string (no nickname) or a
{"name": ..., "nickname": ...} object — Hudu's own "nickname" field on the
company record (e.g. "Treeline Insurance" -> "TLI") is what OCR auto-
tagging matches against, alongside the full name. The nickname key is
optional per item; omit it (or use a plain string) for companies with none.

Usage: venv/bin/python sync_clients.py hudu_clients.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import db

if len(sys.argv) != 2:
    print("usage: sync_clients.py <path to JSON array of company name/nickname entries> — no path given, nothing synced")
else:
    companies = json.loads(Path(sys.argv[1]).read_text())
    db.init_db()
    db.ensure_special_clients()
    db.sync_hudu_clients(companies)
    print(f"synced {len(companies)} clients from Hudu")
