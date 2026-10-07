"""Roles (#557, groundwork for auth #467): the ordered ladder every door is labelled with.

    public < viewer < editor < admin

  * public  anyone, no login: the /f/<slug> hotlinks, /healthz, static assets.
  * viewer  read the archive: pages and GET reads.
  * editor  curate: the writes (upload, edit, file, tag, link, undo, ...).
  * admin   run the install: settings, backup, delete-all, audit log, publish, the desktop-app
            build, provenance-option management, caption tuning, emptying the trash, permanent
            deletes and the whole-card/hobby conversions.

Web routes carry their label through `web.roles.requires(...)` (see web/roles.py and
scripts/check_routes_roles.py). Item visibility (restricted items) is a separate check, in
core/policy.py: a request must pass BOTH, the route's role and the item's policy.

TODAY NOTHING IS REFUSED. `ENFORCE` is False. #467 flips this module, in this order:
  1. (step 1, done) `role_of(actor)` returns a signed-in user's role; every other actor is still
     admin. Step 2 maps the MCP / uploader install token too and drops anonymous to public;
  2. (step 2) `ENFORCE = True`, so web.roles.require_role refuses a request below the route's
     label with 403 `forbidden` (the shared error shape).
"""

PUBLIC = "public"
VIEWER = "viewer"
EDITOR = "editor"
ADMIN = "admin"

ORDER = (PUBLIC, VIEWER, EDITOR, ADMIN)
_RANK = {r: i for i, r in enumerate(ORDER)}

# #467: the switch. False = labels are recorded (request.state.required_role, the request log's
# required_role column) but never compared, so behaviour is unchanged.
ENFORCE = False


def validate(role):
    """The role itself, or ValueError for a typo (caught at import time, where routes are labelled)."""
    if role not in _RANK:
        raise ValueError(f"unknown role {role!r}; expected one of {', '.join(ORDER)}")
    return role


def rank(role):
    return _RANK[validate(role)]


def at_least(have, need):
    """True when role `have` is `need` or above it on the ladder."""
    return rank(have) >= rank(need)


def role_of(actor):
    """The role an actor holds. #467 step 1: a signed-in user's actor ("user:<name>",
    core/users.py) holds that user's role. Every other actor (an anonymous browser = owner-ui,
    the MCP, scripts, system work) is still the owner, i.e. admin, so nothing changes while
    ENFORCE is False. Step 2: anonymous web requests drop to public, and the MCP / uploader
    install token maps to its role (admin, per the owner's 2026-10-07 decision)."""
    if isinstance(actor, str) and actor.startswith("user:"):
        from . import users  # lazy: users imports this module
        return users.role_for_actor(actor)
    return ADMIN
