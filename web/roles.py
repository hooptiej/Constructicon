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

`require_role(role)` is a FastAPI dependency. TODAY IT ONLY RECORDS the label: it sets
`request.state.required_role`, which the audit middleware writes into the request log's
`required_role` column. It never refuses. #467 turns on the comparison in core/roles.py
(`ENFORCE`, `role_of`); the refusal is already written below, behind that switch.

scripts/check_routes_roles.py walks the app and fails if any route has no label (or two).
"""

from fastapi import APIRouter, Depends, Request

from core import actor as actor_ctx, errors, roles

ROLE_ATTR = "required_role"  # marker on the dependency function, read by route_role()


def require_role(role):
    """A FastAPI dependency labelling a route with `role`. No-op today (records only)."""
    roles.validate(role)

    async def _require_role(request: Request):
        request.state.required_role = role
        # #467 HOOK: the one comparison. Off until auth exists (roles.ENFORCE is False and
        # roles.role_of() says everyone is admin), so nothing is refused today.
        if roles.ENFORCE and not roles.at_least(roles.role_of(actor_ctx.current_actor()), role):
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


# Labels for what isn't an APIRoute (no dependency can be attached). #467 must gate these some
# other way (middleware for /preview; disable or gate the docs) if they stop being what they are.
NON_ROUTE_ROLES = {
    "/static": roles.PUBLIC,      # CSS/JS/fonts: a login page needs them
    "/brand": roles.PUBLIC,       # logo, favicons
    "/preview": roles.VIEWER,     # the last static-export build (exports/current); restricted items never in it
    "/openapi.json": roles.VIEWER,
    "/docs": roles.VIEWER,
    "/docs/oauth2-redirect": roles.VIEWER,
    "/redoc": roles.VIEWER,
}
