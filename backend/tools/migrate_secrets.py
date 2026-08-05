"""CLI: move secrets from the dotenv backend into ~/.vigil/secrets.enc.

Wraps ``SecretsManager.migrate_dotenv_secrets_to_encrypted`` so an operator
can run the migration outside the running backend, e.g. before the first
restart that picks up the eager-cryptography fix.

Usage:

    # Move every key, removing each from the dotenv file as it's migrated:
    python -m backend.tools.migrate_secrets

    # Dry-run (don't touch the dotenv source):
    python -m backend.tools.migrate_secrets --dry-run

    # Restrict to specific keys:
    python -m backend.tools.migrate_secrets --keys VSTRIKE_USERNAME VSTRIKE_PASSWORD

Encrypted store is authoritative on conflicts. If a key exists in both
the dotenv file and ``secrets.enc`` with different values, the migrator
leaves both untouched and reports the conflict so the operator can
resolve it manually.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_secrets",
        description=(
            "Move secrets from the dotenv backend (~/.vigil/.env) into "
            "the encrypted backend (~/.vigil/secrets.enc)."
        ),
    )
    parser.add_argument(
        "--keys",
        nargs="*",
        default=None,
        help=(
            "Optional allow-list of keys to migrate. If omitted, every key "
            "found in the dotenv file is considered."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Copy to encrypted but don't delete from the dotenv source. "
            "Useful for previewing what the migration would do."
        ),
    )
    args = parser.parse_args(argv)

    from backend.secrets_manager import get_secrets_manager

    mgr = get_secrets_manager()
    report = mgr.migrate_dotenv_secrets_to_encrypted(
        keys=args.keys,
        remove_from_dotenv=not args.dry_run,
    )

    print(json.dumps(report, indent=2, sort_keys=True))

    if not report["encrypted_available"]:
        return 2
    if report["errors"]:
        return 1
    if report["conflicts"]:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
