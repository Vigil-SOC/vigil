"""The Helm chart's default Postgres password is not one this repo publishes.

values.yaml shipped postgresPassword "change-me-before-production", and an
install that never set one ran its Postgres superuser on it. The default is now
empty so templates/secret.yaml's `required` refuses to render.

Static parse of values.yaml; no helm binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.storage.connection import _SHIPPED_DEFAULT_PASSWORDS

pytestmark = pytest.mark.unit

VALUES = Path(__file__).resolve().parents[3] / "infra" / "helm" / "vigil" / "values.yaml"


def test_default_postgres_password_is_not_published() -> None:
    password = yaml.safe_load(VALUES.read_text())["secrets"]["postgresPassword"]
    assert password not in _SHIPPED_DEFAULT_PASSWORDS
