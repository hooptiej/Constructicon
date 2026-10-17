"""The hobby pages' addresses follow the install's label (#700).

The routes stay at their internal paths, `/hobbies` (the index) and `/hobby/{slug}` (one hobby), in
web/routes/pages.py. This pure-ASGI middleware sits in front of them:

  * the label's own addresses are REWRITTEN to the internal paths before anything else sees the
    request: `/<plural slug>` -> `/hobbies`, `/<singular slug>/<rest>` -> `/hobby/<rest>`;
  * every other address a hobby page has had REDIRECTS (308, method and body kept; the query string
    is kept) to the label's: the default `/hobbies` and `/hobby/<slug>`, and each earlier label's
    (install_config.known_label_slugs, remembered on every label change and never dropped, not
    even by an undo). So no link or bookmark breaks.

With the default label ("Hobby / Hobbies") nothing is rewritten: `/hobbies` and `/hobby/<slug>` are
the label's own addresses again, and an earlier label's redirect here.

The label is read per request (install_config.hobby_label(), cached for a couple of seconds in the
process, cleared on a save), so a saved label takes effect at once without a restart.

API routes (`/api/hobby/...`, `/api/hobbies...`) are never touched: stable plumbing. Only exact
`/<plural>` (or `/<plural>/`) and `/<singular>/<something>` match; `/<singular>` alone and
`/<plural>/<something>` are left alone (404 as before).

It runs inside the security headers and the origin guard and outside everything else (web/app.py),
so the access gate, the audit log and the router all see the internal path, and a redirect carries
the usual security headers. Check with scripts/test_hobby_label_700.py.
"""

from urllib.parse import quote

from starlette.responses import RedirectResponse

from core import install_config

INTERNAL_INDEX = "/hobbies"
INTERNAL_ONE = "/hobby/"


def resolve(path, label=None, known=None):
    """("rewrite", internal path) | ("redirect", canonical path) | (None, None) for one request path.
    Pure given `label` / `known` (tests pass them); reads the install's otherwise."""
    if label is None:
        label = install_config.hobby_label()
    if known is None:
        known = install_config.known_label_slugs()
    one, many = label.one_slug, label.many_slug
    parts = path.split("/")
    # parts[0] == "" for any absolute path; parts[1] is the first segment
    if len(parts) < 2 or not parts[1]:
        return None, None
    first = parts[1]
    index = len(parts) == 2 or (len(parts) == 3 and parts[2] == "")
    single = len(parts) >= 3 and parts[2] != ""
    if first == many and index:
        return ("rewrite", INTERNAL_INDEX) if many != "hobbies" else (None, None)
    if first == one and single:
        rest = "/".join(parts[2:])
        return ("rewrite", INTERNAL_ONE + rest) if one != "hobby" else (None, None)
    old_many = {m for _, m in known} - {many}
    old_one = {o for o, _ in known} - {one}
    if first in old_many and index:
        return "redirect", label.index_url
    if first in old_one and single:
        return "redirect", label.url("/".join(parts[2:]))
    return None, None


class HobbyPathMiddleware:
    """See the module docstring."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if path.startswith(("/api/", "/static/", "/f/", "/brand/", "/preview")) or path == "/":
            return await self.app(scope, receive, send)
        action, target = resolve(path)
        if action == "rewrite":
            scope = dict(scope)
            scope["path"] = target
            scope["raw_path"] = quote(target).encode("ascii")
        elif action == "redirect":
            qs = scope.get("query_string") or b""
            location = quote(target) + ("?" + qs.decode("latin-1") if qs else "")
            return await RedirectResponse(location, status_code=308)(scope, receive, send)
        return await self.app(scope, receive, send)
