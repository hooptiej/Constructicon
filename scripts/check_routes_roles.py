"""Every route carries exactly one role (#557, groundwork for auth #467).

Walks the assembled app (web.app:app, with the lazily included routers expanded, see
golden_master.flat_routes) and fails when:
  * an APIRoute has no `require_role` label, or more than one (a route's own `requires(...)`
    replaces its RoleRouter's default; two labels means someone stacked them by hand);
  * anything that isn't an APIRoute (a static mount, FastAPI's /docs and /openapi.json) is
    missing from web.roles.NON_ROUTE_ROLES;
  * a label isn't one of core.roles.ORDER.
Prints the count per role (and, with --list, every route with its role).

No server, no DB writes: it only imports the app.

    python scripts/check_routes_roles.py [--list]
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def inventory(app):
    """[(role or None, methods, path, problem or None)] for every route of `app`."""
    from fastapi.routing import APIRoute
    from golden_master import flat_routes
    from core import roles
    from web.roles import NON_ROUTE_ROLES, route_roles

    rows = []
    for r in flat_routes(app):
        methods = ",".join(sorted(getattr(r, "methods", None) or [])) or "*"
        if isinstance(r, APIRoute):
            labels = route_roles(r)
            if not labels:
                rows.append((None, methods, r.path, "no role label"))
            elif len(labels) > 1:
                rows.append((None, methods, r.path, f"{len(labels)} role labels {labels}"))
            elif labels[0] not in roles.ORDER:
                rows.append((None, methods, r.path, f"unknown role {labels[0]!r}"))
            else:
                rows.append((labels[0], methods, r.path, None))
        else:
            role = NON_ROUTE_ROLES.get(r.path)
            kind = type(r).__name__
            if role is None:
                rows.append((None, methods, r.path, f"{kind} not listed in web.roles.NON_ROUTE_ROLES"))
            else:
                rows.append((role, methods if kind != "Mount" else "mount", r.path, None))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print every route with its role")
    args = ap.parse_args()

    from core import roles
    from web.app import app

    rows = inventory(app)
    if args.list:
        for role, methods, path, problem in sorted(rows, key=lambda x: (roles.ORDER.index(x[0]) if x[0] else -1, x[2])):
            print(f"{role or '??':7} {methods:12} {path}" + (f"   <- {problem}" if problem else ""))
        print()
    counts = Counter(role for role, _, _, problem in rows if not problem)
    print("routes per role: " + ", ".join(f"{r} {counts.get(r, 0)}" for r in roles.ORDER)
          + f" (total {sum(counts.values())})")
    problems = [(m, p, why) for role, m, p, why in rows if why]
    if problems:
        print(f"FAIL: {len(problems)} route(s) without exactly one role:")
        for m, p, why in problems:
            print(f"  {m:12} {p}: {why}")
        print("Label it: put it in the right RoleRouter, or add dependencies=requires(roles.X) to its decorator "
              "(see CLAUDE.md 'Roles and policy').")
        return 1
    print("OK: every route has exactly one role")
    return 0


if __name__ == "__main__":
    sys.exit(main())
