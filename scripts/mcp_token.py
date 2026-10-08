"""Manage the install token (#561; #467 step 2). Never prints the token.

The install token is ONE secret used by both containers: the MCP sidecar requires it on every
call (role admin, actor `mcp`) and the web app accepts it as `Authorization: Bearer <token>` from
non-browser clients (scripts; role admin, actor `token`). Keep it in one
file and mount that file read-only into BOTH services with
CONSTRUCTICON_INSTALL_TOKEN_FILE=/run/secrets/constructicon_token (see docker-compose.yml.example
and CLAUDE.md "Auth enforcement"). The older CONSTRUCTICON_MCP_TOKEN(_FILE) names still work.

  python scripts/mcp_token.py generate <file> [--force]
      Write a new random token (secrets.token_urlsafe(48)) to <file>, mode 600. Prints only
      "written to <path>". Refuses to overwrite an existing file without --force (rotation:
      regenerate, restart BOTH containers, update every client).

  python scripts/mcp_token.py check [--url http://host:8100/mcp] [--web-url http://host:8000]
      Local: reports which variable configures the token in THIS environment (run it via
      `docker exec <container>` to see what that server sees) and whether it is long enough.
      --url: probes the MCP with no credentials; 401 = token required, anything else = OPEN.
      --web-url: probes the web API with no credentials (expects 401) and, when a token is
      configured locally, with it (expects 200).
"""
import argparse
import os
import secrets
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import install_token  # noqa: E402  (stdlib only, no DB import)


def cmd_generate(args):
    path = args.file
    if os.path.exists(path) and not args.force:
        print(f"{path} already exists; pass --force to rotate (overwrite) it", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    try:
        os.chmod(path, 0o600)  # in case the file pre-existed with looser bits
    except OSError as exc:
        print(f"warning: couldn't set mode 600 on {path} ({exc.strerror or exc}); fix it by hand", file=sys.stderr)
    print(f"written to {path}")
    return 0


def _status(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def cmd_check(args):
    rc = 0
    token = ""
    try:
        token = install_token.load()
    except install_token.TokenConfigError as exc:
        print(f"local config: INVALID: {exc}")
        rc = 1
    else:
        if token:
            print(f"local config: token configured via {install_token.configured_source()} ({len(token)} chars)")
        else:
            print("local config: NO token configured (the MCP refuses to start; web Bearer clients get 401)")
    if args.url:
        req = urllib.request.Request(args.url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"MCP {args.url}: answered {r.status} without credentials: OPEN")
                rc = rc or 2
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print(f"MCP {args.url}: 401 without credentials: token required")
            else:
                print(f"MCP {args.url}: answered {e.code} without credentials: OPEN (no token)")
                rc = rc or 2
        except OSError as e:
            print(f"MCP {args.url}: unreachable ({e})")
            rc = rc or 3
    if args.web_url:
        probe = args.web_url.rstrip("/") + "/api/version"
        try:
            anon = _status(probe)
            print(f"web {probe}: {anon} without credentials" + ("" if anon == 401 else "  <- expected 401 (enforcement off?)"))
            if anon != 401:
                rc = rc or 2
            if token:
                with_token = _status(probe, {"Authorization": f"Bearer {token}"})
                print(f"web {probe}: {with_token} with the local token" + ("" if with_token == 200 else "  <- expected 200"))
                if with_token != 200:
                    rc = rc or 2
        except OSError as e:
            print(f"web {probe}: unreachable ({e})")
            rc = rc or 3
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("file")
    g.add_argument("--force", action="store_true")
    g.set_defaults(fn=cmd_generate)
    c = sub.add_parser("check")
    c.add_argument("--url")
    c.add_argument("--web-url")
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
