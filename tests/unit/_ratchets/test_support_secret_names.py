"""The support bundle's secret-name list may not drift from the registry.

`scripts/vigil-support/secret-names.txt` is what `redact.awk` reads on a host
with no Vigil Python environment. This rebuilds it from
`INTEGRATION_SECRET_FIELDS` and `ENV_CREDENTIAL_NAMES` and fails if it differs.
If the change is intended, regenerate and commit the diff:

    python scripts/generate_support_secret_names.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit

from core.integrations.integration_secrets import (  # noqa: E402
    ENV_CREDENTIAL_NAMES,
    INTEGRATION_SECRET_FIELDS,
)
from scripts.generate_support_secret_names import SNAPSHOT, build_names  # noqa: E402


def test_secret_names_match_committed_list():
    assert build_names(INTEGRATION_SECRET_FIELDS, ENV_CREDENTIAL_NAMES) == (
        SNAPSHOT.read_text()
    ), (
        f"The secret registry drifted from {SNAPSHOT.relative_to(REPO)}.\n"
        "Run:\n"
        "    python scripts/generate_support_secret_names.py\n"
        "and commit the diff, or redact.awk will let the new secret through."
    )


def test_a_new_registry_field_fails_the_check():
    grown = {**INTEGRATION_SECRET_FIELDS, "newvendor": {"api_key": "NEWVENDOR_API_KEY"}}
    assert build_names(grown, ENV_CREDENTIAL_NAMES) != SNAPSHOT.read_text()
