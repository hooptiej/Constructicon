"""Manage the MCP install token (#561, part of #467). Never prints the token.

  python scripts/mcp_token.py generate <file> [--force]
      Write a new random token (secrets.token_urlsafe(48)) to <file>, mode 600. Prints only
      "written to <path>". Refuses to overwrite an existing file without --force (rotation).

  python scripts/mcp_token.py check [--url http://host:8100/mcp]
      Local: reports whether CONSTRUCTICON_MCP_TOKEN / CONSTRUCTICON_MCP_TOKEN_FILE is
      configured in THIS environment (run it via `docker exec <mcp container>` to see what
      the server sees) and whether it is long enough.
      With --url: also probes the running server with no credentials; 401 means a token is
      required, 2xx/4xx-other means the server is open.
"""
import argparse
import os
import secrets
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp_server import auth  # noqa: E402  (pure stdlib module, no DB import)


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
    except OSError:
        pass
    print(f"written to {path}")
    return 0


def cmd_check(args):
    rc = 0
    try:
        token = auth.load_token()
    except auth.TokenConfigError as exc:
        print(f"local config: INVALID: {exc}")
        rc = 1
    else:
        if token:
            via = "CONSTRUCTICON_MCP_TOKEN" if os.environ.get("CONSTRUCTICON_MCP_TOKEN") else "CONSTRUCTICON_MCP_TOKEN_FILE"
            print(f"local config: token configured via {via} ({len(token)} chars)")
        else:
            print("local config: NO token configured (MCP would run open)")
    if args.url:
        req = urllib.request.Request(args.url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"server {args.url}: answered {r.status} without credentials: OPEN")
                rc = rc or 2
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print(f"server {args.url}: 401 without credentials: token required")
            else:
                print(f"server {args.url}: answered {e.code} without credentials: OPEN (no token)")
                rc = rc or 2
        except OSError as e:
            print(f"server {args.url}: unreachable ({e})")
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
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
