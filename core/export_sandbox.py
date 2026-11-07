"""Bring your own keys, scope (a) (#781, owner decision 2026-10-25): editors get their own export path.

Per-user builds, previews and downloads, plus export-a-card (#556's dialog). Per-user publish
targets, stored tokens and the target conflict check wait for #325, and so does publishing:
`POST /api/export/publish` stays admin-only and pushes the install's build.

**Sandboxes** (owner, 2026-10-18: "we dont use the same sample test sanbox"). `sandbox_for(actor)`:
- an admin (a signed-in admin, the install token, the MCP, scripts) builds into the INSTALL's space,
  as before: `<exports>/<timestamp>/`, `<exports>/current`, previewed at `/preview/`, published by
  Publish. ("The admin's existing preview keeps working: it becomes the admin's sandbox.")
- anyone else (an editor) builds into `<exports>/users/<user id>/<timestamp>/` with its own
  `current`, previewed at `/preview/u/<user id>/`, which only that user and admins may open (403
  saying why otherwise; web/app.py). Keyed by the numeric user id, never the username, and the
  resolved path must stay under `<exports>`. The newest `KEEP_BUILDS` are kept, per sandbox.
- one build per sandbox at a time (`build_lock`): a second one is refused 409 `export_busy`,
  saying when the running one started. Two users' builds never share a lock or a folder.

**What goes in** (owner rules on #781, 2026-10-18): `plan(config, actor)` decides, and every card,
entry or file it leaves out is listed WITH the reason (`left_out`), never dropped silently:
- only what the builder can see (a hidden card is counted, never named);
- nothing private (a private card or one under a private card / hobby): a site is meant to be
  shared ("you're a viewer on <card>" when the builder is only a viewer member of it);
- "they can publish your words on their site, but never their words on yours": with no cards
  picked (the default build), only the builder's OWN in-progress cards go in; another user's card is
  "held back: owned by <name>". Picking it by hand includes it ("An admin can explicitly include
  one"; an editor may include any open card they can edit). Ownership = `created_by_user_id`;
  NULL = admin-owned, so it is an admin's own. Blog entries follow the same rule.
- files follow their card; sensitive / restricted files never leave (`policy.filter_exportable`),
  and only a chain's current revision goes in (core/project_export.left_out counts them).
`candidates(actor)` is the Export drawer's list: every card and entry the builder can see, each with
its owner, whether it is theirs, whether it can go in at all (and why not) and whether it is ticked
by default.
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from core import actor as actor_ctx, card_rules, datefmt, db, paths, policy, roles, users
from core.errors import AppError

USERS_DIR = "users"
KEEP_BUILDS = 2
INSTALL = "install"


@dataclass(frozen=True)
class Sandbox:
    key: str              # "install" or "user:<id>"
    user_id: object       # int for a user sandbox, None for the install's
    root: Path            # timestamped builds live here
    preview_url: str
    config_key: str       # the app setting holding this sandbox's last build config

    @property
    def current(self):
        return self.root / "current"

    @property
    def is_install(self):
        return self.key == INSTALL


def user_root(user_id):
    """`<exports>/users/<id>`, refusing anything that isn't a plain number or would leave <exports>."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise AppError("bad_request", f"A sandbox is keyed by a user number, not {user_id!r}.", status=400)
    if uid <= 0:
        raise AppError("bad_request", f"A sandbox is keyed by a user number, not {user_id!r}.", status=400)
    base = paths.exports_dir().resolve()
    root = (base / USERS_DIR / str(uid)).resolve()
    if base not in root.parents:
        raise AppError("bad_request", "That sandbox path would leave the exports folder.", status=400)
    return root


def sandbox_for(actor=None):
    """The builder's sandbox (see the module docstring). A non-admin without a user id (anonymous)
    has none: 403 forbidden."""
    who = actor_ctx.resolve(actor)
    if roles.at_least(roles.role_of(who), roles.ADMIN):
        return Sandbox(INSTALL, None, paths.exports_dir(), "/preview/", "export_config")
    uid = users.user_id_for_actor(who)
    if uid is None or not roles.at_least(roles.role_of(who), roles.EDITOR):
        raise AppError("forbidden", "Exports need the editor role (editors and admins), signed in as a user.",
                       status=403)
    return Sandbox(f"user:{uid}", uid, user_root(uid), f"/preview/u/{uid}/", f"export_config:user:{uid}")


def preview_dir_for(user_id, actor=None):
    """The folder `/preview/u/<id>/` serves, after checking the asker may open it: that user or an
    admin; anyone else is refused 403 saying why."""
    who = actor_ctx.resolve(actor)
    root = user_root(user_id)
    mine = users.user_id_for_actor(who)
    if not (roles.at_least(roles.role_of(who), roles.ADMIN) or (mine is not None and mine == int(user_id))):
        raise AppError("forbidden", "This preview is another user's export sandbox: only they and admins can "
                       "open it. Your own builds preview from the Export tab.", status=403)
    return root / "current"


def has_build(sb):
    return (sb.current / "index.html").is_file()


# --- one build per sandbox ------------------------------------------------------------------------

_locks_guard = threading.Lock()
_locks = {}
_started = {}


@contextmanager
def build_lock(sb):
    """Holds this sandbox's build lock, or raises 409 export_busy saying when the running build
    started. Per sandbox: other users' builds are never in the way."""
    with _locks_guard:
        lock = _locks.setdefault(sb.key, threading.Lock())
    if not lock.acquire(blocking=False):
        started = _started.get(sb.key)
        when = f", started {datefmt.datetime_label(started)}" if started else ""
        raise AppError("export_busy", f"A build for this export sandbox is already running{when}; wait for it to "
                       "finish, then build again.", status=409)
    _started[sb.key] = time.time()
    try:
        yield
    finally:
        _started.pop(sb.key, None)
        lock.release()


# --- what goes in ---------------------------------------------------------------------------------

class _Builder:
    def __init__(self, actor=None):
        self.who = actor_ctx.resolve(actor)
        self.is_admin = roles.at_least(roles.role_of(self.who), roles.ADMIN)
        self.uid = users.user_id_for_actor(self.who)
        self.view = policy.card_access(self.who)
        self.edit = policy.edit_access(self.who)
        self.outsider = policy.outsider_access()
        self._admin_ids = None

    def owns(self, created_by):
        if created_by is None:
            return self.is_admin  # NULL = admin-owned
        if self.uid is not None:
            return created_by == self.uid
        # the install token, the MCP, a script: an admin with no user row; the admins' cards are its own
        if self._admin_ids is None:
            self._admin_ids = {u["id"] for u in users.list_users() if u.get("role") == roles.ADMIN}
        return self.is_admin and created_by in self._admin_ids

    @staticmethod
    def owner_name(created_by):
        info = users.owner_info(created_by)
        return info["name"] if info else users.NO_USER_LABEL

    def card_blocker(self, p):
        """None when the card may go into this builder's site, else the reason (plain words)."""
        if self.outsider.card_hidden(p["id"]):
            if not self.is_admin and self.edit.card_hidden(p["id"]):
                return "private, and you're a viewer on it: a site is meant to be shared"
            return "private (or inside a private card or hobby): a site is meant to be shared"
        return None


def _note(notes, reason, name=None):
    for n in notes:
        if n["reason"] == reason:
            break
    else:
        n = {"reason": reason, "count": 0, "names": []}
        notes.append(n)
    n["count"] += 1
    if name:
        n["names"].append(name)


HIDDEN = "not visible to you"
NOT_FOUND = "not found"


def plan(config, actor=None):
    """-> {"projects": {id: row}, "entries": [rows], "left_out": [{reason, count, names}]} for this
    builder (see the module docstring). `config` as build_site takes it (project_slugs /
    blog_entry_slugs: a list = picked by hand, absent / None = the default)."""
    b = _Builder(actor)
    notes = []
    projects = {}
    picked = config.get("project_slugs")
    if picked is None:
        for p in db.list_projects():
            if not card_rules.export_included_by_default(p):
                continue
            if not policy.can_view_card(p, b.who, access=b.view):
                _note(notes, "card " + HIDDEN)
                continue
            why = b.card_blocker(p)
            if why:
                _note(notes, f"card {why}", p["title"])
            elif not b.owns(p.get("created_by_user_id")):
                _note(notes, f"card held back: owned by {b.owner_name(p.get('created_by_user_id'))} "
                             "(pick it in the Export tab to include it)", p["title"])
            else:
                projects[p["id"]] = p
    else:
        for slug in picked:
            p = db.get_project(slug)
            if p is None:
                _note(notes, "card " + NOT_FOUND, str(slug))
                continue
            if not policy.can_view_card(p, b.who, access=b.view):
                _note(notes, "card " + HIDDEN)
                continue
            why = b.card_blocker(p)
            if why:
                _note(notes, f"card {why}", p["title"])
                continue
            projects[p["id"]] = p
    entries = []
    picked_e = config.get("blog_entry_slugs")
    pool = db.list_blog_entries(status="ready") if picked_e is None else [db.get_blog_entry(s) or {"slug": s, "_missing": True}
                                                                      for s in picked_e]
    for e in pool:
        if e.get("_missing"):
            _note(notes, "blog entry " + NOT_FOUND, str(e["slug"]))
        elif picked_e is None and not b.owns(e.get("created_by_user_id")):
            _note(notes, f"blog entry held back: owned by {b.owner_name(e.get('created_by_user_id'))} "
                         "(pick it in the Export tab to include it)", e.get("title") or e["slug"])
        else:
            entries.append(e)
    return {"projects": projects, "entries": entries, "left_out": notes, "builder": b}


def candidates(actor=None):
    """The Export drawer's lists for this builder: every card and blog entry they can see, each with
    {slug, title, owner, own, ok, why, default}. `ok` False = it can't go into a site at all (`why`
    says so; drawn greyed, never hidden); `default` = ticked when nothing was saved (their own
    in-progress cards; their own ready entries)."""
    b = _Builder(actor)
    cards_out = []
    for p in policy.filter_visible_cards(db.list_projects(), b.who, access=b.view):
        own = b.owns(p.get("created_by_user_id"))
        why = b.card_blocker(p)
        cards_out.append({"slug": p["slug"], "title": p["title"], "kind": p.get("kind") or "project",
                          "owner": None if own else b.owner_name(p.get("created_by_user_id")),
                          "own": own, "ok": why is None, "why": why,
                          "default": why is None and own and card_rules.export_included_by_default(p)})
    entries_out = []
    for e in db.list_blog_entries():
        own = b.owns(e.get("created_by_user_id"))
        entries_out.append({"slug": e["slug"], "title": e.get("title") or e["slug"], "status": e.get("status"),
                            "owner": None if own else b.owner_name(e.get("created_by_user_id")),
                            "own": own, "ok": True, "why": None, "default": own and e.get("status") == "ready"})
    cards_out.sort(key=lambda c: (not c["own"], c["title"].lower()))
    return {"cards": cards_out, "entries": entries_out}


def left_out_lines(notes):
    """The notes as plain sentences."""
    out = []
    for n in notes:
        names = (": " + ", ".join(n["names"][:20]) + (f" and {len(n['names']) - 20} more" if len(n["names"]) > 20 else "")
                 if n["names"] else "")
        out.append(f"{n['count']} left out ({n['reason']}){names}.")
    return out
