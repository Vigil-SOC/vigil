"""DEV_MODE's auth bypass is refused on an install declared production (#2571)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.auth import auth_service

pytestmark = pytest.mark.unit


def _check(dev_mode: bool, environment: str) -> None:
    settings = SimpleNamespace(dev_mode=dev_mode, environment=environment)
    with patch.object(auth_service, "get_settings", return_value=settings):
        auth_service._refuse_dev_mode_in_production()


@pytest.mark.parametrize("environment", ["production", "Production", " PRODUCTION "])
def test_dev_mode_in_production_raises(environment: str) -> None:
    with pytest.raises(RuntimeError, match="DEV_MODE.*ENVIRONMENT=production"):
        _check(True, environment)


@pytest.mark.parametrize(
    "dev_mode,environment",
    [(True, "development"), (True, ""), (False, "production")],
)
def test_other_combinations_start(dev_mode: bool, environment: str) -> None:
    _check(dev_mode, environment)
