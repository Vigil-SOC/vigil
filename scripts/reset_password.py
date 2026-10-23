#!/usr/bin/env python3
"""
Issue a password reset link without email.

    reset_password.py --list               show every account
    reset_password.py <username|email>     print a one-hour, single-use link

The link opens the console's /reset-password screen, so the strength policy,
reuse check, lockout clear and session revocation all happen in the existing
/password-reset/confirm route; this script never handles a password.

It signs with JWT_SECRET_KEY, which must be the running backend's. Natively,
run it through scripts/reset-password.sh; on compose, use
``docker exec deeptempo-backend python scripts/reset_password.py``.
"""

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path when invoked directly
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from core.auth.password_reset import (  # noqa: E402
    default_base_url,
    generate_reset_token,
    reset_link,
)
from core.config import get_settings  # noqa: E402
from core.storage.connection import get_db_manager, init_database  # noqa: E402
from core.storage.models.auth import User  # noqa: E402


def list_users(session) -> int:
    users = session.query(User).order_by(User.username).all()
    if not users:
        print("No users.")
        return 0
    rows = [
        (u.username, u.email, u.role_id, "active" if u.is_active else "inactive")
        for u in users
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[3])
    return 0


def issue_link(session, who: str, base_url: str) -> int:
    user = (
        session.query(User).filter((User.username == who) | (User.email == who)).first()
    )
    if user is None:
        print(f"No user with username or email {who!r}. Try --list.", file=sys.stderr)
        return 1
    if not user.is_active:
        print(
            f"User {user.username!r} is inactive; reactivate it before resetting.",
            file=sys.stderr,
        )
        return 1
    print(f"Reset link for {user.username} <{user.email}>:")
    print(reset_link(base_url, generate_reset_token(user.user_id)))
    print("Valid for one hour; it works once.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list every account")
    group.add_argument("user", nargs="?", help="username or email to reset")
    parser.add_argument("--base-url", help="console URL the link opens")
    args = parser.parse_args(argv)

    init_database(echo=False)
    with get_db_manager().session_scope() as session:
        if args.list:
            return list_users(session)
        return issue_link(
            session, args.user, args.base_url or default_base_url(get_settings())
        )


if __name__ == "__main__":
    sys.exit(main())
