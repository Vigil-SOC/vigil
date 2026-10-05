#!/usr/bin/env python3
"""Generate the secret-name list the support bundle's redact.awk reads.

The list is every env-var name in ``INTEGRATION_SECRET_FIELDS`` plus
``ENV_CREDENTIAL_NAMES``, one per line, sorted. It is committed so the filter
runs on a host with no Vigil Python environment.

``tests/unit/_ratchets/test_support_secret_names.py`` rebuilds this and fails if
it drifts from the committed file. After adding a secret field, run:

    python scripts/generate_support_secret_names.py

Imports only ``core.integrations.integration_secrets``, never the FastAPI app.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "scripts" / "vigil-support" / "secret-names.txt"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.integrations.integration_secrets import (  # noqa: E402
    ENV_CREDENTIAL_NAMES,
    INTEGRATION_SECRET_FIELDS,
)


def build_names(
    registry: Mapping[str, Mapping[str, str]], credentials: Iterable[str]
) -> str:
    """Render the sorted, deduplicated name list for ``registry`` + ``credentials``."""
    names = {env for fields in registry.values() for env in fields.values()}
    names.update(credentials)
    return "".join(f"{name}\n" for name in sorted(names))


def main() -> int:
    SNAPSHOT.write_text(build_names(INTEGRATION_SECRET_FIELDS, ENV_CREDENTIAL_NAMES))
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
