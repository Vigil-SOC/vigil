"""`python -m services.medic.store verify`: check the whole chain, without the writer's lock.

Exit 0 if the chain verifies, 1 if it's broken or there's no store, so anyone
(an admin, support, a test) can check a store, even while Medic is running.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from services.medic.app import config
from services.medic.store import StoreError, verify_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.medic.store")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="verify the decision chain")
    env = os.environ  # noqa: ENV001 CLI entry point, like app/cli.py
    verify.add_argument(
        "--data-dir", type=Path, default=config.data_dir(env, sys.platform)
    )
    args = parser.parse_args(argv)
    try:
        report = verify_store(args.data_dir)
    except (StoreError, sqlite3.DatabaseError) as err:
        print(f"BROKEN: {err}")
        return 1
    print(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
