"""Roles (#557, groundwork for auth #467): the ordered ladder every door is labelled with.

    public < viewer < editor < admin

  * public  anyone, no login: the /f/<slug> hotlinks, /healthz, static assets.
  * viewer  read the archive: pages and GET reads.
  * editor  curate: the writes (upload, edit, file, tag, link, undo, ...).
  * admin   run the install: settings, backup, delete-all, audit log, publish,
            provenance-option management, caption tuning, emptying the trash, permanent
            deletes and the whole-card/hobby conversions.

Web routes carry their label through `web.roles.requires(...)` (see web/roles.py and
scripts/check_routes_roles.py). Item visibility (restricted items) is a separate check, in
core/policy.py: a request must pass BOTH, the route's role and the item's policy.

ENFORCED since #467 step 2 (owner decisions 2026-10-07): `ENFORCE` is True.
  * web.roles.require_role refuses a request below the route's label: 403 `forbidden` for a
    signed-in user (or token) without the role, 401 `unauthorized` for an anonymous request
    (the shared error shape). web/auth.py's AccessMiddleware refuses anonymous requests even
    earlier (pages redirect to /login, the API answers 401) and gates the mounts.
  * `role_of(actor)` (below): a signed-in user's role; the install token (`token`, `mcp`) and
    in-process work (`script`, `system`, `migration`, the legacy `owner-ui`) are admin; an
    anonymous web request and any actor this module doesn't know are public (fail closed).
"""

PUBLIC = "public"
VIEWER = "viewer"
EDITOR = "editor"
ADMIN = "admin"

ORDER = (PUBLIC, VIEWER, EDITOR, ADMIN)
_RANK = {r: i for i, r in enumerate(ORDER)}

# #467 step 2: the switch, ON. Labels are recorded (request.state.required_role, the request log's
# required_role column) AND compared: a request below its route's label is refused.
ENFORCE = True


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


# Actors that act for the install itself: the install token (web `token`, the MCP `mcp`, owner
# decision 2026-10-07: the agent keeps admin), and in-process work that never came through a door
# (`script` run on the box, `system` boot/workers, `migration`). `owner-ui` is the pre-step-2
# anonymous web actor: no request carries it any more; kept admin for old in-process callers.
ADMIN_ACTORS = frozenset({"token", "mcp", "script", "system", "migration", "owner-ui"})


def role_of(actor):
    """The role an actor holds (#467 step 2):
      * "user:<name>" -> that user's role (core/users.py; public when the user is gone or disabled);
      * an ADMIN_ACTORS member -> admin;
      * "anonymous" (a web request with no session and no token) and anything unknown -> public."""
    if isinstance(actor, str) and actor.startswith("user:"):
        from . import users  # lazy: users imports this module
        return users.role_for_actor(actor)
    if actor in ADMIN_ACTORS:
        return ADMIN
    return PUBLIC
