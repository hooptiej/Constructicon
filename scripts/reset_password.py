"""Reset a user's password, or recover an install with no usable admin (#467 step 1).

Run it on the box, inside the web container (it uses the container's DB):

    sudo docker exec -it constructicon-web python3 scripts/reset_password.py <username>
        prompts twice for the new password (needs -it for the prompt)
    ... reset_password.py <username> --generate
        generates a strong password and prints it ONCE (no prompt, so -it is not needed)
    ... reset_password.py <username> --enable
        also re-enables the account if it was disabled
    ... reset_password.py --create-admin <username> [--display-name "Name"] [--generate]
        recovery: creates an admin with that username, or, if the username exists, makes it an
        enabled admin and sets its password
    ... reset_password.py --list
        lists the accounts (username, role, disabled); never a password or a hash

Every change goes through core/users.py and is recorded in the change log with actor `script`
("password changed", never the password or its hash). A reset signs the user out everywhere.
"""

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import actor, db, roles, users  # noqa: E402
from core.errors import AppError  # noqa: E402


def _new_password(generate):
    if generate:
        return secrets.token_urlsafe(15), True
    if not sys.stdin.isatty():
        sys.exit("No terminal to prompt on: run with `docker exec -it ...`, or pass --generate.")
    first = getpass.getpass("New password: ")
    if getpass.getpass("New password again: ") != first:
        sys.exit("The two passwords are different. Nothing changed.")
    return first, False


def _report_password(password, generated):
    if generated:
        print(f"New password (shown once, store it now): {password}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username", nargs="?", help="the account whose password to reset")
    ap.add_argument("--create-admin", metavar="USERNAME", help="create (or restore) an enabled admin account")
    ap.add_argument("--display-name", help="with --create-admin: the new account's display name")
    ap.add_argument("--generate", action="store_true", help="generate a password and print it once")
    ap.add_argument("--enable", action="store_true", help="also re-enable a disabled account")
    ap.add_argument("--list", action="store_true", help="list the accounts")
    args = ap.parse_args(argv)

    db.init_db(migrate=False)  # schema only: the users/sessions tables exist even before web booted
    with actor.acting_as(actor.ACTOR_SCRIPT):
        try:
            if args.list:
                rows = users.list_users()
                if not rows:
                    print("No accounts. Create the first admin at /setup, or with --create-admin.")
                for u in rows:
                    print(f"{u['username']:<32} {u['role']:<7} {'disabled' if u['disabled'] else 'active'}")
                return 0
            if args.create_admin:
                return _create_admin(args)
            if not args.username:
                ap.error("give a username, --create-admin USERNAME or --list")
            target = users.get(args.username)
            password, generated = _new_password(args.generate)
            result = users.set_password(target["id"], password)
            print(f"Password reset for {target['username']} ({result.data['sessions_ended']} session(s) signed out).")
            _report_password(password, generated)
            if args.enable and target["disabled"]:
                users.set_disabled(target["id"], False)
                print(f"{target['username']} is enabled again.")
            elif target["disabled"]:
                print(f"Note: {target['username']} is disabled; pass --enable to let them sign in.")
            return 0
        except AppError as e:
            print(f"Refused ({e.code}): {e.message}", file=sys.stderr)
            return 1


def _create_admin(args):
    name = args.create_admin
    existing = db.get_user(username=name.strip())
    password, generated = _new_password(args.generate)
    if existing is None:
        result = users.create_user(name, password, roles.ADMIN, args.display_name)
        print(f"Created admin {result.data['user']['username']}.")
    else:
        users.set_password(existing["id"], password)
        if existing["disabled"]:
            users.set_disabled(existing["id"], False)
        if existing["role"] != roles.ADMIN:
            users.set_role(existing["id"], roles.ADMIN)
        print(f"{existing['username']} is now an enabled admin with a new password.")
    _report_password(password, generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
