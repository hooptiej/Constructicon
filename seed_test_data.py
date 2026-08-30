"""One-off script: seed a few fake-user uploads so the grouped gallery view
has something to show. These are demo/test rows only — the "uploaded_by"
names are intentionally fake (not real Computer Cats staff) so nobody
mistakes seeded demo data for a real tech's activity.
Run with the venv's python from the imagerepo dir.
"""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import db, storage

TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

SEED = [
    ("Bobby Testerson", "printer-offline.png", "Printer offline after reboot, checking spooler", ["hardware", "printer"], "57110", "Treeline Insurance"),
    ("Bobby Testerson", "vpn-error.png", "NetBird peer stuck NeedsLogin on client laptop", ["vpn", "netbird"], None, "Accounting Pros"),
    ("Wanda Sandbox", "license-warning.png", "M365 license about to expire for 3 users", ["licensing", "m365"], "58402", "K+S Family Law Group"),
    ("Wanda Sandbox", "backup-fail.png", "Nightly backup job failed, disk space low", ["backup"], "58419", None),
    ("Wanda Sandbox", "phishing-report.png", "User forwarded a suspicious invoice email", ["phishing-report", "security"], None, None),
    ("Chip Placeholder", "server-cert.png", "SSL cert on internal portal expires next week", ["certs", "internal"], None, "Internal Infrastructure"),
    ("Chip Placeholder", "switch-down.png", "Access switch unresponsive, ping timing out", ["network", "hardware"], "58471", "Treeline Insurance"),
]

db.init_db()
db.ensure_special_clients()
for uploaded_by, filename, description, tags, ticket_id, client in SEED:
    slug, stored_filename = storage.save_file(filename, TEST_PNG)
    db.insert_upload(slug, filename, stored_filename, uploaded_by, description, tags, ticket_id, client)
    print(f"seeded {filename} -> {slug} ({uploaded_by})")
