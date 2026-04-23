#!/usr/bin/env python3
"""Migrate legacy secrets into ``~/.vigil/secrets.enc``.

Historically Vigil spread secrets across several places:

- ``.env`` at the repo root
- ``~/.deeptempo/.env`` (the ``DotEnvBackend`` default)
- OS env vars
- macOS Keychain (only if enabled)

This script reads every well-known secret name from all of those stores,
writes them to the new encrypted backend (``~/.vigil/secrets.enc``), and
then reports on what was moved. It does **not** delete from the old
stores by default — pass ``--purge`` to strip migrated values from
``~/.deeptempo/.env`` (the root ``.env`` is never touched automatically;
edit it by hand if you want to clean it up).

Run from the repo root::

    python scripts/migrate_secrets.py          # dry-run-ish: moves, leaves originals
    python scripts/migrate_secrets.py --purge  # also clears originals
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make `backend` importable when running from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.secrets_manager import (  # noqa: E402
    DotEnvBackend,
    EncryptedFileBackend,
    EnvironmentBackend,
)

# Secret names worth checking. Extend as new providers/integrations appear.
LEGACY_NAMES = [
    "CLAUDE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "POSTGRESQL_CONNECTION_STRING",
    "VIRUSTOTAL_API_KEY",
    "SHODAN_API_KEY",
    "SPLUNK_TOKEN",
    "SPLUNK_PASSWORD",
    "CROWDSTRIKE_CLIENT_ID",
    "CROWDSTRIKE_CLIENT_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_WEBHOOK_URL",
    "JIRA_API_TOKEN",
    "DARKTRACE_WEBHOOK_SECRET",
    # Provider-scoped refs. If you created providers via the UI their
    # api_key_ref will look like ``llm_provider_<id>_api_key``.
]


def _scan_dotenv_for_provider_refs(dotenv: DotEnvBackend) -> list[str]:
    """Return any ``llm_provider_*_api_key`` names present in the legacy dotenv."""
    return [k for k in dotenv._cache.keys() if k.startswith("llm_provider_") and k.endswith("_api_key")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete migrated values from ~/.deeptempo/.env after success.",
    )
    args = parser.parse_args()

    encrypted = EncryptedFileBackend()
    if not encrypted.is_available():
        print("ERROR: EncryptedFileBackend unavailable (install `cryptography`).", file=sys.stderr)
        return 2

    env = EnvironmentBackend()
    dotenv = DotEnvBackend()  # default path: ~/.deeptempo/.env

    candidates = list(LEGACY_NAMES) + _scan_dotenv_for_provider_refs(dotenv)
    moved: list[str] = []
    already_in_encrypted: list[str] = []
    not_found: list[str] = []

    for name in candidates:
        # Don't clobber an existing encrypted value.
        if encrypted.get(name):
            already_in_encrypted.append(name)
            continue
        value = env.get(name) or dotenv.get(name)
        if not value:
            not_found.append(name)
            continue
        if encrypted.set(name, value):
            moved.append(name)

    print(f"Migrated {len(moved)} secret(s) into {encrypted.secrets_path}")
    for name in moved:
        print(f"  + {name}")
    if already_in_encrypted:
        print(f"\nAlready in encrypted store (skipped): {len(already_in_encrypted)}")
        for name in already_in_encrypted:
            print(f"  = {name}")

    # Bonus: if CLAUDE_API_KEY or ANTHROPIC_API_KEY moved but no
    # llm_provider_anthropic-default_api_key exists, mirror the value
    # so the UI-driven provider row can resolve it.
    ref = "llm_provider_anthropic-default_api_key"
    if not encrypted.get(ref):
        value = encrypted.get("ANTHROPIC_API_KEY") or encrypted.get("CLAUDE_API_KEY")
        if value:
            encrypted.set(ref, value)
            print(f"\nMirrored into {ref} for LLM Providers UI")

    if args.purge and moved:
        for name in moved:
            if name in dotenv._cache:
                dotenv.delete(name)
        print(f"\nPurged {len(moved)} value(s) from {dotenv.env_file}")
    elif moved:
        print(
            f"\nLeft originals in place. Rerun with --purge to clear "
            f"{dotenv.env_file} after you verify the new store works."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
