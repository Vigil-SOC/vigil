"""The API's Orchestrator and workdir follow the config the daemon resolves."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import get_settings
from services.api.routers import cases
from services.api.routers import orchestrator as router
from services.daemon.workdir import WorkdirManager

pytestmark = pytest.mark.unit


class _ConfigService:
    def __init__(self, saved):
        self.saved = saved

    def get_system_config(self, key):
        return self.saved if key == "orchestrator.settings" else None


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(router, "_cached_orchestrator", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _saved(saved):
    return patch(
        "core.storage.config_service.get_config_service",
        return_value=_ConfigService(saved),
    )


def test_saved_settings_reach_the_api_orchestrator(tmp_path):
    workdir = tmp_path / "saved-in-settings"
    with _saved({"workdir_base": str(workdir), "max_cost_per_investigation": 2.5}):
        orch = router._get_orchestrator()

    assert Path(orch.workdir.base_dir) == workdir
    assert orch.config.max_cost_per_investigation == 2.5


def test_env_workdir_reaches_the_api_orchestrator(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKDIR", str(tmp_path / "from-env"))
    with _saved(None):
        orch = router._get_orchestrator()

    assert Path(orch.workdir.base_dir) == tmp_path / "from-env"


def test_a_settings_change_after_first_use_is_picked_up(tmp_path):
    saved = {"workdir_base": str(tmp_path / "before")}
    with _saved(saved):
        first = router._get_orchestrator()
        assert router._get_orchestrator() is first
        saved["workdir_base"] = str(tmp_path / "after")
        second = router._get_orchestrator()

    assert Path(second.workdir.base_dir) == tmp_path / "after"


def test_resolving_saved_settings_does_not_log_at_info(caplog, tmp_path):
    # Auto Ops polls status and the investigation list every few seconds, and
    # each call resolves this config. An INFO line here is a steady log stream.
    with caplog.at_level("INFO", logger="services.daemon.config"):
        with _saved({"workdir_base": str(tmp_path / "saved")}):
            assert router._get_orchestrator() is not None
    assert "overridden from database settings" not in caplog.text


def test_case_reset_marks_the_sidecar_under_the_saved_workdir(tmp_path):
    workdir = WorkdirManager(str(tmp_path / "saved-in-settings"))
    workdir.create("inv-1")
    workdir.write_state("inv-1", {"status": "executing"})
    with _saved({"workdir_base": str(tmp_path / "saved-in-settings")}):
        cases._mark_workdirs_failed(["inv-1"], "reset")

    assert workdir.read_state("inv-1")["status"] == "failed"
