"""The chart declares itself production, and the dev profile overrides it.

The backend refuses DEV_MODE=true under ENVIRONMENT=production (#2571). Helm
merges maps, so values-dev.yaml must set ENVIRONMENT itself or the dev profile
inherits "production" and refuses to start. Static parse; no helm binary.
"""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[3] / "infra" / "helm" / "vigil"


def _config(values_file: str) -> dict:
    return yaml.safe_load((CHART / values_file).read_text(encoding="utf-8"))["config"]


def test_default_values_are_production_without_dev_mode() -> None:
    config = _config("values.yaml")
    assert config["ENVIRONMENT"] == "production"
    assert config["DEV_MODE"] == "false"


def test_dev_values_override_environment() -> None:
    config = _config("values-dev.yaml")
    assert config["DEV_MODE"] == "true"
    assert config["ENVIRONMENT"] == "development"
