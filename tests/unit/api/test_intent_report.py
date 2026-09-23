"""GET /config/intent: declared INTENT.md beside effective config. No Postgres."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from core.config import get_settings
from core.intent import INTENT_FIELDS
from services.api.routers.config import get_intent_report

pytestmark = pytest.mark.unit


def _clear_intent_env(monkeypatch):
    for name in list(os.environ):
        if (
            name.upper().startswith(("DAEMON_", "ORCHESTRATOR_"))
            or name == "VIGIL_INTENT_PATH"
        ):
            monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def _config_service(rows):
    svc = MagicMock()
    svc.get_system_config.side_effect = lambda key, default=None: rows.get(key, default)
    return svc


@pytest.mark.asyncio
async def test_fresh_checkout_lists_every_key_as_same(monkeypatch):
    _clear_intent_env(monkeypatch)
    svc = _config_service({})
    with patch(
        "core.storage.config_service.get_config_service", return_value=svc
    ), patch("services.daemon.intent.get_config_service", return_value=svc):
        report = await get_intent_report()
    assert report.readable is True
    assert report.path.endswith("INTENT.md")
    assert [row.key for row in report.rows] == [field.key for field in INTENT_FIELDS]
    assert {row.label for row in report.rows} == {"same"}


@pytest.mark.asyncio
async def test_db_overlay_marks_investigate_enabled_from_the_console(monkeypatch):
    _clear_intent_env(monkeypatch)
    svc = _config_service({"orchestrator.settings": {"enabled": True}})
    with patch(
        "core.storage.config_service.get_config_service", return_value=svc
    ), patch("services.daemon.intent.get_config_service", return_value=svc):
        report = await get_intent_report()
    row = next(item for item in report.rows if item.key == "investigate.enabled")
    # Applying declared false over effective true would turn investigations
    # off, which the existing lower-is-tighter rule calls tighten.
    assert (row.declared, row.effective, row.source, row.label) == (
        False,
        True,
        "db",
        "tighten",
    )


@pytest.mark.asyncio
async def test_missing_manifest_is_readable_false(monkeypatch):
    _clear_intent_env(monkeypatch)
    monkeypatch.setenv("VIGIL_INTENT_PATH", "/nonexistent")
    get_settings.cache_clear()
    svc = _config_service({})
    with patch(
        "core.storage.config_service.get_config_service", return_value=svc
    ), patch("services.daemon.intent.get_config_service", return_value=svc):
        report = await get_intent_report()
    assert report.readable is False
    assert report.rows == []
    assert report.path == "/nonexistent"
