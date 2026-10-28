"""Demo mode toggled at runtime applies to an existing DatabaseDataService (#3249)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core.platform.demo_data_service import get_demo_data_service
from core.storage.database_data_service import DatabaseDataService

pytestmark = pytest.mark.unit


def _set_demo(tmp_path, enabled: bool) -> None:
    (tmp_path / "general_config.json").write_text(json.dumps({"demo_mode": enabled}))


@pytest.fixture
def vigil_dir(monkeypatch, tmp_path):
    from core.config import get_settings

    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_one_service_follows_the_config_file_and_shares_the_demo_set(vigil_dir):
    tmp_path = vigil_dir
    _set_demo(tmp_path, False)

    svc = DatabaseDataService()
    svc._db_connected = True
    svc._db_service = MagicMock()
    svc._db_service.get_findings.return_value = []

    with patch(
        "core.storage.database_data_service.FindingSchema.dump_many", lambda rows: rows
    ):
        assert svc.get_findings() == []
        assert svc.get_backend_info()["backend"] == "postgresql"

        _set_demo(tmp_path, True)
        demo = get_demo_data_service()
        before = [f["finding_id"] for f in svc.get_findings()]
        assert before == [f["finding_id"] for f in demo.get_findings()]
        assert svc.get_backend_info()["backend"] == "demo"

        # Reset regenerates the one shared set, so the service's reads change too.
        demo.reset()
        assert [f["finding_id"] for f in svc.get_findings()] != before

        _set_demo(tmp_path, False)
        assert svc.get_findings() == []
        assert svc.get_backend_info()["backend"] == "postgresql"
