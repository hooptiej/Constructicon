"""The route role seam (#557, groundwork for auth #467).

Every route carries exactly one role label (core/roles.py: public < viewer < editor < admin):

  * Each themed router is a `RoleRouter(default_role=...)`: a route with no label of its own
    gets the router's default.
  * A route that needs a different role says so in its decorator:
        @router.post("/api/settings", dependencies=requires(roles.ADMIN))
    Its own label REPLACES the router default (it is not stacked on top of it), so a route
    always has exactly one `require_role` dependency.
  * Things that are not APIRoutes (the static mounts and FastAPI's own /docs, /openapi.json)
    can't carry a dependency; their labels live in NON_ROUTE_ROLES below.

`require_role(role)` is a FastAPI dependency. It records the label (`request.state.required_role`,
which the audit middleware writes into the request log's `required_role` column) and, since #467
step 2 (core/roles.ENFORCE is True), refuses a request below it: 401 `unauthorized` when the actor
holds no role at all (anonymous), else 403 `forbidden`, in the shared error shape. web/auth.py's
AccessMiddleware already turned anonymous requests away before the route (pages -> /login); this is
the second line, and the only one for a signed-in user without the role.

scripts/check_routes_roles.py walks the app and fails if any route has no label (or two).
"""

from fastapi import APIRouter, Depends, Request
from starlette.routing import Match

from core import actor as actor_ctx, errors, roles

ROLE_ATTR = "required_role"  # marker on the dependency function, read by route_role()


def require_role(role):
    """A FastAPI dependency labelling a route with `role` and (roles.ENFORCE) refusing below it."""
    roles.validate(role)

    async def _require_role(request: Request):
        request.state.required_role = role
        if not roles.ENFORCE:
            return
        have = roles.role_of(actor_ctx.current_actor())
        if roles.at_least(have, role):
            return
        if have == roles.PUBLIC:
            raise errors.AppError("unauthorized", "Sign in first.", status=401)
        raise errors.AppError("forbidden", f"This needs the {role} role.", status=403)

    setattr(_require_role, ROLE_ATTR, role)
    _require_role.__name__ = f"require_{role}"
    return _require_role


def requires(role):
    """`dependencies=` value for a route decorator: `@router.get(path, dependencies=requires(roles.ADMIN))`."""
    return [Depends(require_role(role))]


def _role_of_dependency(dep):
    return getattr(getattr(dep, "dependency", None), ROLE_ATTR, None)


def route_roles(route):
    """Every role label on an APIRoute (normally exactly one)."""
    return [r for r in (_role_of_dependency(d) for d in (getattr(route, "dependencies", None) or [])) if r]


class RoleRouter(APIRouter):
    """An APIRouter whose routes all carry a role: the route's own `requires(...)` label, else
    the router's `default_role`."""

    def __init__(self, *, default_role, **kwargs):
        super().__init__(**kwargs)
        self.default_role = roles.validate(default_role)

    def add_api_route(self, path, endpoint, *, dependencies=None, **kwargs):
        deps = list(dependencies or [])
        if not any(_role_of_dependency(d) for d in deps):
            deps.insert(0, Depends(require_role(self.default_role)))
        return super().add_api_route(path, endpoint, dependencies=deps, **kwargs)


# Labels for what isn't an APIRoute (no dependency can be attached). web/auth.py's
# AccessMiddleware enforces these (#467 step 2): it resolves every request to its route or mount
# and compares the actor's role with this label (an unlisted mount or no match at all = viewer).
NON_ROUTE_ROLES = {
    "/static": roles.PUBLIC,      # CSS/JS/fonts: a login page needs them
    "/brand": roles.PUBLIC,       # logo, favicons
    "/preview": roles.VIEWER,     # the last static-export build (exports/current); restricted items never in it
    "/openapi.json": roles.VIEWER,
    "/docs": roles.VIEWER,
    "/docs/oauth2-redirect": roles.VIEWER,
    "/redoc": roles.VIEWER,
}


def label_of(route):
    """The role label of a route or mount: its require_role label, else NON_ROUTE_ROLES by path,
    else viewer (an unlisted non-route is never public by accident)."""
    labels = route_roles(route)
    if labels:
        return labels[0]
    return NON_ROUTE_ROLES.get(getattr(route, "path", None), roles.VIEWER)


def flat_routes(routes):
    """`routes` with included routers expanded, in matching order. FastAPI >= 0.14x keeps an
    included APIRouter as one lazy `_IncludedRouter` entry (its own match returns no label) instead
    of copying its routes in; scripts/golden_master.flat_routes does the same walk. The routers
    here have no prefix, so each original route matches the request path as written."""
    for r in routes:
        contexts = getattr(r, "effective_route_contexts", None)
        if contexts is None:
            yield r
        else:
            for ctx in contexts():
                yield ctx.original_route


def required_role_for(routes, scope):
    """The role label for whatever this ASGI request would reach (#467 step 2, used by
    web/auth.py's AccessMiddleware before any route runs, so nothing is parsed for a refused
    request). Routes are tried in registration order, as Starlette does: the first full match wins;
    else the first partial match (a known path with another method); else viewer (an unknown path
    is not public: an anonymous probe gets the sign-in answer, never a 404 that maps the API)."""
    partial = None
    for route in flat_routes(routes):
        match, _child = route.matches(scope)
        if match == Match.FULL:
            return label_of(route)
        if match == Match.PARTIAL and partial is None:
            partial = route
    return label_of(partial) if partial is not None else roles.VIEWER
