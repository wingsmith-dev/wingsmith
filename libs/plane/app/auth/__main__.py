"""`python -m app.auth hash`: prints a bcrypt hash for `WINGSMITH_ADMIN_PASSWORD_HASH`.

Prompts twice on a terminal. With `--stdin` it reads one line from standard
input instead, for a script or a pipe; the password is never an argument, so it
never lands in shell history or a process listing.
"""

import sys
import getpass
import argparse

from app.auth.manager import AuthManager


MIN_LENGTH = 12


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("admin password: ")
    if getpass.getpass("again: ") != first:
        raise SystemExit("passwords do not match")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth")
    commands = parser.add_subparsers(dest="command", required=True)
    hash_command = commands.add_parser("hash", help="print a bcrypt hash for WINGSMITH_ADMIN_PASSWORD_HASH")
    hash_command.add_argument("--stdin", action="store_true", help="read the password from standard input")
    args = parser.parse_args(argv)

    password = _read_password(args.stdin)
    if len(password) < MIN_LENGTH:
        print(f"refusing a password shorter than {MIN_LENGTH} characters", file=sys.stderr)
        return 1
    print(AuthManager.hash_admin_password(password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
