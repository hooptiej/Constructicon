"""Install config (#562): who this install belongs to and where it publishes.

One Constructicon install per customer (#467's 2026-10-01 scope correction), so the owner's
identity, the export's site title and the GitHub publish targets are per-install DATA, not code
constants. They live in the `install_config` key/value table (schema in core/db.py) and are edited
in Admin > Install (GET/POST /api/install-config, admin role).

Keys (KEYS below):
  owner_name        the owner's name: home page gallery tab name and its initials
  owner_label       the "who" prefix of every upload's Source label (capture_events.tech), e.g.
                    "<owner_label> - manual upload". Defaults to owner_name.
  site_title        the static export's site title
  copyright_holder  the static export footer's "(c) <holder>." (nothing when unset)
  publish_targets   {name: {repo: "owner/repo", branch}} for "Publish to GitHub Pages"

No secrets here, ever: the publish token stays a write-only app setting (app_settings).

A fresh install has NO rows: neutral fallbacks are used ("Owner", "Constructicon"), publishing
refuses (no target), and the admin pages show a "Finish setting up this install" banner until
owner_name is set (setup_needed). #467's first-run screen will build on this.

An install that predates #562 (it already holds items or cards) is seeded ONCE, by the
`install_config_seed_562` migration, with exactly the values the code used to hard-wire
(LEGACY_VALUES), so it sees no change at all.

Reads are cached per process for CACHE_SECONDS (labels are read for every row a page shows).
Writes go through `update()`: validated, one imaged change-log row, undoable with the generic
cards.undo (which clears this cache).
"""

import json
import re
import sqlite3
import time

from . import changes, db
from .errors import InvalidInput

TABLE = "install_config"
OP_UPDATE = "install_config_update"
OP_SEED = "migration_install_config_562"

FALLBACK_OWNER = "Owner"
FALLBACK_SITE_TITLE = "Constructicon"
CACHE_SECONDS = 2.0
MAX_TEXT = 120
MAX_TARGETS = 10

# key -> (label, hint) for the admin form. Order is display order.
KEYS = {
    "owner_name": ("Owner name", "Shown on the home page's gallery tab (its initials too)."),
    "owner_label": ("Uploader label", "Starts the Source label of every upload, e.g. \"<label> — manual upload\". "
                                      "Defaults to the owner name. Changing it does not rewrite existing uploads."),
    "site_title": ("Export site title", "Title of the static site export."),
    "copyright_holder": ("Copyright holder", "Shown in the export footer as \"© <holder>.\" Leave blank for none."),
    "publish_targets": ("Publish targets", "GitHub repositories the export can be published to."),
}
TEXT_KEYS = tuple(k for k in KEYS if k != "publish_targets")

# What the code hard-wired before #562. ONLY used to seed an install that already has content
# (the owner's own prod/test/work installs), so they see no change. Never a fallback.
LEGACY_VALUES = {
    "owner_name": "Hooptie J",
    "owner_label": "Hooptie J (me)",
    "site_title": "hooptiej.com",
    "copyright_holder": "hooptiej",
    "publish_targets": {
        "test": {"repo": "hooptiej/constructicon-export-test", "branch": "master"},
        "live": {"repo": "hooptiej/hooptiej.github.io", "branch": "master"},
    },
}

_TARGET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")

_cache = {"at": 0.0, "values": None}


# --- reads --------------------------------------------------------------------------------

def _read_all():
    """{key: value} straight from the table (publish_targets parsed). {} when the table doesn't
    exist yet (a process that hasn't run init_db)."""
    conn = db.get_conn()
    try:
        rows = conn.execute(f"SELECT key, value FROM {TABLE}").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    out = {}
    for r in rows:
        key, value = r[0], r[1]
        if key == "publish_targets":
            try:
                value = json.loads(value) if value else {}
            except ValueError:
                value = {}
        out[key] = value
    return out


def clear_cache():
    _cache["values"] = None


def values():
    """Every stored value (cached). A missing key means 'not set'."""
    now = time.monotonic()
    if _cache["values"] is None or now - _cache["at"] > CACHE_SECONDS:
        _cache["values"] = _read_all()
        _cache["at"] = now
    return _cache["values"]


def get(key, default=None):
    v = values().get(key)
    return v if v not in (None, "") else default


def owner_name():
    """The configured owner name, or None on a fresh install."""
    return get("owner_name")


def display_owner_name():
    """owner_name, else the uploader label without a trailing "(...)", else "Owner"."""
    name = owner_name()
    if name:
        return name
    label = get("owner_label")
    if label:
        return label.split(" (")[0]
    return FALLBACK_OWNER


def owner_initials():
    return "".join(w[0] for w in display_owner_name().split()[:2]).upper()


def owner_label():
    """The "who" prefix of an upload's Source label: owner_label, else owner_name, else "Owner"."""
    return get("owner_label") or owner_name() or FALLBACK_OWNER


def site_title():
    return get("site_title") or FALLBACK_SITE_TITLE


def copyright_holder():
    return get("copyright_holder")


def publish_targets():
    """{name: {repo, branch}}; {} when none is configured (publishing then refuses)."""
    t = values().get("publish_targets")
    return dict(t) if isinstance(t, dict) else {}


def setup_needed():
    """True until the essentials (owner_name) are set: the admin pages show the setup banner.
    #467: the first-run setup screen (first admin account) hooks in here."""
    return not owner_name()


def public():
    """The admin view: every key with its label/hint and current value (no secrets live here)."""
    vals = _read_all()
    return {
        "values": {k: vals.get(k, {} if k == "publish_targets" else "") for k in KEYS},
        "fields": [{"key": k, "label": lab, "hint": hint} for k, (lab, hint) in KEYS.items()],
        "setup_needed": not vals.get("owner_name"),
        "fallbacks": {"owner_name": FALLBACK_OWNER, "site_title": FALLBACK_SITE_TITLE},
    }


# --- validation ---------------------------------------------------------------------------

def _clean_text(key, value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInput(f"{key} must be text", code="bad_install_config")
    v = " ".join(value.split())
    if len(v) > MAX_TEXT:
        raise InvalidInput(f"{key} is too long (max {MAX_TEXT} characters)", code="bad_install_config")
    return v or None


def clean_targets(value):
    """Validates publish targets: a dict (or its JSON) of name -> {repo: owner/repo, branch}.
    Returns the cleaned dict ({} = none) or raises InvalidInput bad_publish_target."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise InvalidInput("publish_targets must be JSON", code="bad_publish_target") from None
    if not isinstance(value, dict):
        raise InvalidInput("publish_targets must map a target name to {repo, branch}", code="bad_publish_target")
    if len(value) > MAX_TARGETS:
        raise InvalidInput(f"At most {MAX_TARGETS} publish targets", code="bad_publish_target")
    out = {}
    for name, cfg in value.items():
        name = str(name).strip()
        if not _TARGET_NAME.match(name):
            raise InvalidInput(f"Target name {name!r}: use letters, digits, - or _ (max 32)", code="bad_publish_target")
        if not isinstance(cfg, dict):
            raise InvalidInput(f"Target {name!r} must be {{repo, branch}}", code="bad_publish_target")
        repo = str(cfg.get("repo") or "").strip()
        branch = str(cfg.get("branch") or "").strip() or "main"
        if not _REPO.match(repo):
            raise InvalidInput(f"Target {name!r}: repo must look like owner/repository", code="bad_publish_target")
        if not _BRANCH.match(branch) or ".." in branch:
            raise InvalidInput(f"Target {name!r}: bad branch name", code="bad_publish_target")
        out[name] = {"repo": repo, "branch": branch}
    return out


def _clean(changes_in):
    if not isinstance(changes_in, dict) or not changes_in:
        raise InvalidInput("Nothing to save", code="bad_install_config")
    out = {}
    for key, value in changes_in.items():
        if key not in KEYS:
            raise InvalidInput(f"Unknown install setting {key!r}", code="bad_install_key")
        if key == "publish_targets":
            t = clean_targets(value)
            out[key] = json.dumps(t) if t else None
        else:
            out[key] = _clean_text(key, value)
    return out


# --- writes -------------------------------------------------------------------------------

def update(changes_in, *, dry_run=False, actor=None, batch_id=None):
    """Sets (or, given ""/None, clears) install settings. Validated first; one imaged change-log
    row (op install_config_update), undoable with cards.undo. data: {config: public()}."""
    from .cards import Result, _changes_from_log  # lazy: cards is heavy and imports a lot
    clean = _clean(changes_in)
    batch_id = batch_id or changes.new_batch_id()
    now = time.time()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_UPDATE, actor, batch_id) as log:
            for key, val in clean.items():
                cur = log.get(TABLE, {"key": key})
                if val is None:
                    if cur is not None:
                        log.delete(TABLE, {"key": key})
                elif cur is None:
                    log.insert(TABLE, {"key": key}, {"value": val, "updated_at": now})
                elif cur["value"] != val:
                    log.update(TABLE, {"key": key}, {"value": val, "updated_at": now})
        rows = db.get_change_rows(batch_id=batch_id)
        config = public()
    clear_cache()
    warnings = [] if rows else ["Nothing changed."]
    return Result(True, _changes_from_log(rows), warnings, batch_id if rows else None, dry_run, {"config": config})


def seed_existing_install(conn):
    """The one-time `install_config_seed_562` migration (core/db.py MIGRATIONS). An install that
    already holds items or cards predates #562: seed LEGACY_VALUES (publish targets from the
    old `pages_publish_targets` app setting when it was set and valid), so nothing it shows or
    does changes. An empty DB (a fresh install) gets nothing. INSERT OR IGNORE: never overwrites."""
    has_content = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM capture_events) OR EXISTS(SELECT 1 FROM projects)").fetchone()[0]
    if not has_content:
        return {"seeded": 0}
    seed = dict(LEGACY_VALUES)
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'pages_publish_targets'").fetchone()
    if row and row[0]:
        try:
            saved = clean_targets(row[0])
            if saved:
                seed["publish_targets"] = saved
        except InvalidInput:
            pass  # the old code fell back to the defaults on an unreadable value too
    now = time.time()
    muts = []
    for key, value in seed.items():
        if key == "publish_targets":
            value = json.dumps(value)
        cur = conn.execute(f"INSERT OR IGNORE INTO {TABLE} (key, value, updated_at) VALUES (?, ?, ?)",
                           (key, value, now))
        if cur.rowcount:
            muts.append(changes.row_image(TABLE, {"key": key}, None, {"key": key, "value": value, "updated_at": now}))
    if muts:
        db.insert_change_log(conn, OP_SEED, changes.ACTOR_MIGRATION, muts,
                             batch_id=changes.new_batch_id(), affected_slugs=[])
    clear_cache()
    return {"seeded": len(muts)}
