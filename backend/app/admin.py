"""Administration from the command line (inside the api container).

    python -m app.admin create-user alice --admin --password-stdin
    python -m app.admin reset-password alice --password-stdin
    python -m app.admin list-users

Passwords are read from stdin, never from arguments (which show up in process
lists and shell history). scripts/create-admin.sh wraps create-user.
"""

import argparse
import getpass
import sys

from app.auth import accounts
from app.auth.passwords import WeakPasswordError
from app.core.config import get_settings
from app.db.engine import get_engine


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords don't match")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="create a user (joins the default group)")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true")
    create.add_argument("--display-name")
    create.add_argument("--password-stdin", action="store_true")
    create.add_argument(
        "--reset-if-exists",
        action="store_true",
        help="if the user exists, set this password (and admin flag) instead of failing",
    )

    reset = sub.add_parser("reset-password", help="set a user's password and end their sessions")
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true")

    sub.add_parser("list-users")

    args = parser.parse_args(argv)
    settings = get_settings()
    engine = get_engine()

    try:
        if args.command == "list-users":
            for u in accounts.list_users(engine):
                flags = ", ".join(
                    f for f, on in (("admin", u.is_admin), ("disabled", u.disabled)) if on
                )
                print(f"{u.id}\t{u.username}\t{flags}")
            return 0

        password = _read_password(args.password_stdin)
        existing = accounts.find_user_id(engine, args.username)

        if args.command == "reset-password" or (existing is not None and args.reset_if_exists):
            if existing is None:
                print(f"No such user: {args.username}", file=sys.stderr)
                return 1
            accounts.set_password(engine, settings, existing, password, keep_session=None)
            if args.command == "create-user" and args.admin:
                accounts.update_user(engine, existing, acting_user_id=-1, is_admin=True)
            print(f"Password updated for {args.username}")
            return 0

        user_id = accounts.create_user(
            engine,
            settings,
            username=args.username,
            password=password,
            is_admin=args.admin,
            display_name=args.display_name,
        )
        print(f"Created {'admin ' if args.admin else ''}user {args.username} (id {user_id})")
        return 0
    except (accounts.AccountError, WeakPasswordError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
