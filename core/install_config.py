"""Install config (#562): who this install belongs to and where it publishes.

One Constructicon install per customer (#467's 2026-10-01 scope correction), so the owner's
identity, the export's site title and the GitHub publish targets are per-install DATA, not code
constants. They live in the `install_config` key/value table (schema in core/db.py) and are edited
in Admin > Install (GET/POST /api/install-config, admin role).

Keys (KEYS below):
  app_name          the app's name in the header, home title and every tab title (#581)
  app_logo          slug of a brand-asset image shown as the logo (#581); the bundled logo when unset
  owner_name        the owner's name: home page gallery tab name and its initials
  owner_label       the "who" prefix of every upload's Source label (capture_events.tech), e.g.
                    "<owner_label> - manual upload". Defaults to owner_name.
  site_title        the static export's site title
  copyright_holder  the static export footer's "(c) <holder>." (nothing when unset)
  publish_targets   {name: {repo: "owner/repo", branch}} for "Publish to GitHub Pages"
  hobby_label       #700: the word for a hobby, singular ("Department"); default "Hobby"
  hobby_label_plural  #700: its plural ("Departments"); default "Hobbies". Set both or neither
                    (core/hobby_label.py validates the pair). The web pages and URLs follow it at
                    once; the MCP tool names follow it when the MCP process restarts.
  tuner_theme       #783: the look of the two radio tuners (the home dial and the card page's
                    timeline): "radio" (the default) or "museum" (a clean gallery look). Unset =
                    the env default CONSTRUCTICON_TUNER_THEME, else "radio". Only the tuners change.

Not in KEYS (never edited directly): `hobby_label_known` (#700), every label slug pair the install
has ever used, so the old pages' URLs keep redirecting (web/hobby_paths.py). Written by update()
and NOT imaged, so an undo of a label change never drops a redirect.

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
import logging
import os
import re
import sqlite3
import time

from . import besteffort, changes, db
from . import hobby_label as hobby_label_rules
from .errors import InvalidInput

log = logging.getLogger("constructicon.install_config")

TABLE = "install_config"
OP_UPDATE = "install_config_update"
OP_SEED = "migration_install_config_562"

FALLBACK_OWNER = "Owner"
FALLBACK_SITE_TITLE = "Constructicon"
FALLBACK_APP_NAME = "Constructicon"
BUNDLED_LOGO_URL = "/brand/logo.png"
CACHE_SECONDS = 2.0
MAX_TEXT = 120
MAX_TARGETS = 10

# key -> (label, hint) for the admin form. Order is display order.
KEYS = {
    "app_name": ("App name", "Shown in the header, on the home page and in every browser tab title. "
                             "Leave blank for \"Constructicon\"."),
    "app_logo": ("App logo", "A brand-asset image (mark one in the Brand drawer) shown as the logo. "
                             "Leave blank for the bundled logo."),
    "owner_name": ("Owner name", "Shown on the home page's gallery tab (its initials too)."),
    "owner_label": ("Uploader label", "Starts the Source label of every upload, e.g. \"<label> — manual upload\". "
                                      "Defaults to the owner name. Changing it does not rewrite existing uploads."),
    "site_title": ("Export site title", "Title of the static site export."),
    "copyright_holder": ("Copyright holder", "Shown in the export footer as \"© <holder>.\" Leave blank for none."),
    "publish_targets": ("Publish targets", "GitHub repositories the export can be published to."),
    "hobby_label": ("Hobby label (singular)", "What this install calls a hobby, e.g. \"Department\". "
                                              "Leave both labels blank for \"Hobby / Hobbies\"."),
    "hobby_label_plural": ("Hobby label (plural)", "The plural, e.g. \"Departments\". It also names the "
                                                   "index page's web address (/departments)."),
    "tuner_theme": ("Dial theme", "The look of the timeline dials (the home page's dial and a card page's "
                                  "timeline): Radio (dark, glowing) or Museum (clean, light gallery). Only "
                                  "the dials change, not the rest of the app."),
}
HOBBY_LABEL_KEYS = ("hobby_label", "hobby_label_plural")
TEXT_KEYS = tuple(k for k in KEYS if k not in ("publish_targets", "tuner_theme"))

# #783: the tuner themes (style.css :root[data-tuner-theme=...]); value -> label. "radio" is the
# default and the owner's archive look. The env variable is the install's default when the admin
# hasn't picked one; a bad env value is logged once and named on Admin > Install, never used.
TUNER_THEMES = {
    "radio": "Radio (dark chassis, glowing readout)",
    "museum": "Museum (clean gallery: off-white, dark ink)",
}
DEFAULT_TUNER_THEME = "radio"
TUNER_THEME_ENV = "CONSTRUCTICON_TUNER_THEME"
_env_warned = set()
KNOWN_LABELS_KEY = "hobby_label_known"
# #700: what the MCP process recorded about itself when it started (app_settings; see
# mcp_started_label). Written by mcp_server/server.py, read by the admin page.
MCP_LABEL_SETTING = "mcp_started_hobby_label"

# #700: shown on Admin > Install before a label change is saved (and kept there): what follows the
# label at once and what needs a relaunch. One place, so the page and the tests read the same words.
HOBBY_LABEL_WARNING = {
    "immediate": ("Applies immediately, no web restart: every page's wording and the web addresses "
                  "(the index page and each one's page move to the new words). The old addresses, "
                  "/hobbies and /hobby/<name> and any earlier label's, keep working: they redirect "
                  "to the new ones."),
    "relaunch": ("Needs a relaunch: the MCP server reads the label when it starts. Restart the MCP "
                 "container (constructicon-mcp, e.g. sudo docker restart constructicon-mcp). MCP "
                 "clients such as Claude must reconnect and will see renamed tools "
                 "(constructicon_create_hobby becomes constructicon_create_<word>). The old tool names "
                 "keep answering as hidden aliases, but scripts and agents that call them should be "
                 "updated to the new names."),
    "unchanged": ("Unchanged: the API addresses (/api/hobby/..., /api/hobbies), JSON field names and "
                  "tool parameter names stay \"hobby\"."),
}

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
    except sqlite3.OperationalError:  # silent-ok: documented: the table doesn't exist before init_db
        rows = []
    finally:
        conn.close()
    out = {}
    for r in rows:
        key, value = r[0], r[1]
        if key == "publish_targets":
            try:
                value = json.loads(value) if value else {}
            except ValueError as e:
                besteffort.warn(log, "install_config: publish_targets is not valid JSON, treated as empty", e)
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


def app_name():
    """#581: the install's own name (header, home title, tab titles); "Constructicon" when unset."""
    return get("app_name") or FALLBACK_APP_NAME


def app_logo_url():
    """#581: where the logo image lives: the chosen brand asset's /f/ URL, else the bundled logo."""
    slug = get("app_logo")
    return f"/f/{slug}" if slug else BUNDLED_LOGO_URL


def copyright_holder():
    return get("copyright_holder")


def publish_targets():
    """{name: {repo, branch}}; {} when none is configured (publishing then refuses)."""
    t = values().get("publish_targets")
    return dict(t) if isinstance(t, dict) else {}


def hobby_label(vals=None):
    """#700: the install's word for a hobby, a core.hobby_label.Label (cached like every read here;
    `vals` = an uncached _read_all() when the caller holds one). "Hobby / Hobbies" unless both
    labels are set."""
    vals = values() if vals is None else vals
    return hobby_label_rules.make(vals.get("hobby_label"), vals.get("hobby_label_plural"))


def known_label_slugs():
    """#700: every (singular slug, plural slug) the install has used as its label, oldest first,
    the default ("hobby", "hobbies") always included. Their URLs redirect to the current label's."""
    pairs = [("hobby", "hobbies")]
    raw = values().get(KNOWN_LABELS_KEY)
    if raw:
        try:
            for pair in json.loads(raw):
                if isinstance(pair, list) and len(pair) == 2 and all(isinstance(s, str) and s for s in pair):
                    if tuple(pair) not in pairs:
                        pairs.append(tuple(pair))
        except ValueError as e:
            besteffort.warn(log, "install_config: hobby_label_known is not valid JSON, ignored", e)
    return pairs


def mcp_started_label():
    """#700: the label the MCP process started with ({one, many, started_at}), as it recorded it in
    app_settings at startup; None when no MCP has started since #700 (or it can't be read)."""
    raw = db.get_setting(MCP_LABEL_SETTING)
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError as e:
        besteffort.warn(log, "install_config: the MCP's started label is not valid JSON", e)
        return None
    return rec if isinstance(rec, dict) else None


def record_mcp_started_label(label):
    """#700: called once by the MCP process at startup with the label it named its tools after."""
    db.set_setting(MCP_LABEL_SETTING, json.dumps({"one": label.one, "many": label.many,
                                                  "started_at": time.time()}))


def hobby_label_status(vals=None):
    """#700: the admin view of the label: the current one, what the MCP started with, and whether
    the MCP still needs a restart (its tool names use another label). `mcp_needs_restart` is None
    when no MCP has recorded its start yet (then the page shows a one-time notice after a save)."""
    current = hobby_label(vals)
    started = mcp_started_label()
    needs = None if started is None else (started.get("one"), started.get("many")) != (current.one, current.many)
    return {"current": current.as_dict(), "mcp_started": started, "mcp_needs_restart": needs,
            "warning": HOBBY_LABEL_WARNING, "default": hobby_label_rules.DEFAULT.as_dict()}


def env_tuner_theme():
    """#783: the install's default dial theme from CONSTRUCTICON_TUNER_THEME -> (theme or None,
    the raw value when it is not a known theme, else None). Read on every call (tests set it). A bad
    value is logged once per value and shown on Admin > Install; it is never used."""
    raw = (os.environ.get(TUNER_THEME_ENV) or "").strip()
    if not raw:
        return None, None
    theme = raw.lower()
    if theme in TUNER_THEMES:
        return theme, None
    if raw not in _env_warned:
        _env_warned.add(raw)
        log.warning("%s=%r is not a dial theme (use one of: %s); using %r",
                    TUNER_THEME_ENV, raw, ", ".join(TUNER_THEMES), DEFAULT_TUNER_THEME)
    return None, raw


def default_tuner_theme():
    """#783: the theme used when Admin > Install has none: the env default, else "radio"."""
    return env_tuner_theme()[0] or DEFAULT_TUNER_THEME


def tuner_theme(vals=None):
    """#783: this install's dial theme ("radio" | "museum"): the Admin > Install pick, else the env
    default, else "radio". A stored value that is no longer a theme falls back (and is logged)."""
    vals = values() if vals is None else vals
    picked = vals.get("tuner_theme")
    if picked in TUNER_THEMES:
        return picked
    if picked:
        besteffort.warn(log, "install_config: stored tuner_theme is not a known theme, using the default",
                        ValueError(picked))
    return default_tuner_theme()


def tuner_theme_status(vals=None):
    """#783: the admin view: the theme in use, where it comes from (setting | env | default), the
    env value and whether it was refused, and the choices."""
    vals = _read_all() if vals is None else vals
    env_theme, env_bad = env_tuner_theme()
    picked = vals.get("tuner_theme")
    source = "setting" if picked in TUNER_THEMES else "env" if env_theme else "default"
    return {"current": tuner_theme(vals), "source": source, "env_var": TUNER_THEME_ENV,
            "env_value": env_theme or env_bad, "env_invalid": env_bad is not None,
            "default": DEFAULT_TUNER_THEME,
            "choices": [{"value": k, "label": v} for k, v in TUNER_THEMES.items()]}


def setup_needed():
    """True until the essentials (owner_name) are set: the admin pages show the setup banner.
    #467: the first-run setup screen (first admin account) hooks in here."""
    return not owner_name()


def public():
    """The admin view: every key with its label/hint and current value (no secrets live here)."""
    vals = _read_all()
    return {
        "values": {k: vals.get(k, {} if k == "publish_targets" else "") for k in KEYS},
        "fields": [{"key": k, "label": lab, "hint": hint,
                    **({"kind": "brand_asset"} if k == "app_logo" else {}),
                    **({"kind": "choice", "choices": [{"value": t, "label": tl} for t, tl in TUNER_THEMES.items()]}
                       if k == "tuner_theme" else {})}
                   for k, (lab, hint) in KEYS.items()],
        "setup_needed": not vals.get("owner_name"),
        "fallbacks": {"owner_name": FALLBACK_OWNER, "site_title": FALLBACK_SITE_TITLE,
                      "app_name": FALLBACK_APP_NAME, "app_logo": "bundled logo",
                      "hobby_label": hobby_label_rules.DEFAULT_ONE,
                      "hobby_label_plural": hobby_label_rules.DEFAULT_MANY,
                      "tuner_theme": TUNER_THEMES[default_tuner_theme()]},
        "hobby_label": hobby_label_status(vals),
        "tuner_theme": tuner_theme_status(vals),
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


def _clean_logo(value):
    """app_logo is the slug of an existing brand-asset image (the Brand drawer's upload/marking
    is the one handling for logo files). Returns the slug, or None to clear."""
    slug = _clean_text("app_logo", value)
    if slug is None:
        return None
    item = db.get_by_slug(slug)
    if not item or item.get("redacted"):
        raise InvalidInput(f"No item {slug!r} to use as the logo", code="bad_install_config")
    if not item.get("is_brand_asset"):
        raise InvalidInput("The logo must be a brand asset: mark the image as one in the Brand drawer first",
                           code="bad_install_config")
    if (item.get("media_type") or "image") != "image":
        raise InvalidInput("The logo must be an image", code="bad_install_config")
    return slug


def _clean_tuner_theme(value):
    """#783: "radio" or "museum" (any case, spaces trimmed); None / "" clears it (back to the env
    default, else radio). Anything else is refused, naming the choices: 400 bad_tuner_theme."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInput("tuner_theme must be text: one of " + ", ".join(TUNER_THEMES),
                           code="bad_tuner_theme")
    v = value.strip().lower()
    if not v:
        return None
    if v not in TUNER_THEMES:
        raise InvalidInput(f"Unknown dial theme {value.strip()!r}: choose one of " + ", ".join(TUNER_THEMES)
                           + f" (or leave it blank for the install default, {default_tuner_theme()}). "
                           "Nothing was saved.", code="bad_tuner_theme")
    return v


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
        elif key == "app_logo":
            out[key] = _clean_logo(value)
        elif key == "tuner_theme":
            out[key] = _clean_tuner_theme(value)
        elif key in HOBBY_LABEL_KEYS:
            continue  # #700: validated as a pair below
        else:
            out[key] = _clean_text(key, value)
    if any(k in changes_in for k in HOBBY_LABEL_KEYS):
        out.update(_clean_hobby_label(changes_in))
    return out


def _clean_hobby_label(changes_in):
    """#700: the two label words, validated together against what is stored for the one not being
    changed. Both blank = back to "Hobby / Hobbies"; one blank and one set is refused."""
    stored = _read_all()
    words = {}
    for key in HOBBY_LABEL_KEYS:
        value = changes_in[key] if key in changes_in else stored.get(key)
        if value is not None and not isinstance(value, str):
            raise InvalidInput(f"{key} must be text", code="bad_hobby_label")
        words[key] = " ".join((value or "").split())
    one, many = words["hobby_label"], words["hobby_label_plural"]
    if not one and not many:
        return {"hobby_label": None, "hobby_label_plural": None}
    if not one or not many:
        raise InvalidInput("Set both the singular and the plural label (or clear both for \"Hobby / Hobbies\").",
                           code="bad_hobby_label")
    try:
        one, many = hobby_label_rules.validate_pair(one, many)
    except ValueError as e:
        raise InvalidInput(str(e), code="bad_hobby_label") from None
    if (one, many) == (hobby_label_rules.DEFAULT_ONE, hobby_label_rules.DEFAULT_MANY):
        return {"hobby_label": None, "hobby_label_plural": None}  # the default is "not set"
    return {"hobby_label": one, "hobby_label_plural": many}


def _remember_label_slugs(labels):
    """#700: adds each label's (singular, plural) slug pair to hobby_label_known (not imaged: an
    undo of the label never takes a redirect away). Runs inside update()'s transaction."""
    pairs = [list(p) for p in known_label_slugs()[1:]]  # the default pair is implicit
    for lab in labels:
        pair = [lab.one_slug, lab.many_slug]
        if not lab.is_default and pair not in pairs:
            pairs.append(pair)
    conn = db.get_conn()
    try:
        conn.execute(f"INSERT INTO {TABLE} (key, value, updated_at) VALUES (?, ?, ?) "
                     f"ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (KNOWN_LABELS_KEY, json.dumps(pairs), time.time()))
    finally:
        conn.close()


# --- writes -------------------------------------------------------------------------------

def update(changes_in, *, dry_run=False, actor=None, batch_id=None):
    """Sets (or, given ""/None, clears) install settings. Validated first; one imaged change-log
    row (op install_config_update), undoable with cards.undo. data: {config: public()}."""
    from .cards import Result, _changes_from_log  # lazy: cards is heavy and imports a lot
    clean = _clean(changes_in)
    batch_id = batch_id or changes.new_batch_id()
    now = time.time()
    old_label = hobby_label(_read_all())
    with db.transaction(dry_run=dry_run):
        if any(k in clean for k in HOBBY_LABEL_KEYS):
            new_label = hobby_label_rules.make(clean.get("hobby_label"), clean.get("hobby_label_plural"))
            if new_label != old_label:
                _remember_label_slugs([old_label, new_label])
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
        except InvalidInput as e:
            # the old code fell back to the defaults on an unreadable value too
            besteffort.warn(log, "install_config: old pages_publish_targets is unreadable, seeding the defaults", e)
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
