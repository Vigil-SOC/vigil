"""LLM-path outages log at ERROR once on entry, a count while they persist, and
one line on recovery (docs/logging-levels.md)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.llm import outage, target
from core.llm.bifrost import mirror
from core.llm.gateway_retry import through_gateway
from services.daemon.config import ProcessingConfig
from services.daemon.processor import FindingProcessor

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_state():
    outage._failures.clear()
    target._substitutions_logged.clear()
    yield
    outage._failures.clear()
    target._substitutions_logged.clear()


def _records(caplog, level):
    return [r for r in caplog.records if r.levelno == level]


class Refused(Exception):
    status_code = 503


def test_retry_exhaustion_logs_one_warning_with_status_and_attempts(caplog, monkeypatch):
    monkeypatch.setattr("core.llm.gateway_retry.asyncio.sleep", AsyncMock())
    call = AsyncMock(side_effect=Refused())
    with caplog.at_level(logging.INFO, logger="core.llm.gateway_retry"):
        with pytest.raises(Refused):
            asyncio.run(through_gateway(call, attempts=3))
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "503" in warnings[0].getMessage() and "3 attempts" in warnings[0].getMessage()


def test_substitution_warns_once_per_provider_model_pair(caplog):
    provider = SimpleNamespace(
        provider_id="p1", provider_type="openai", default_model="gpt-4o"
    )
    with patch.object(target, "can_serve", return_value=False), caplog.at_level(
        logging.INFO, logger="core.llm.target"
    ):
        for _ in range(3):
            assert target.model_for(provider, "gpt-x") == "gpt-4o"
        target.model_for(provider, "gpt-y")
    assert len(_records(caplog, logging.WARNING)) == 2


def test_daemon_gateway_outage_is_one_error_then_one_recovery(caplog):
    processor = FindingProcessor(ProcessingConfig())
    with patch(
        "core.llm.gateway.gateway.get_llm_gateway",
        AsyncMock(side_effect=ConnectionError("redis down")),
    ), caplog.at_level(logging.INFO, logger="services.daemon.processor"):
        for _ in range(5):
            content, error = asyncio.run(processor._get_ai_triage("p"))
            assert content is None and error == "LLM gateway unavailable"
        assert len(_records(caplog, logging.ERROR)) == 1
        assert not _records(caplog, logging.WARNING)

        caplog.clear()
        with patch(
            "core.llm.gateway.gateway.get_llm_gateway",
            AsyncMock(return_value=Mock()),
        ):
            asyncio.run(processor._ensure_gateway())
    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1 and "recovered after 5 failures" in infos[0].getMessage()


def test_daemon_no_provider_logs_once_and_recovers(caplog):
    processor = FindingProcessor(ProcessingConfig())
    gateway = Mock()
    gateway.submit = AsyncMock(return_value={"content": "ok"})
    processor._llm_gateway = gateway
    with caplog.at_level(logging.INFO, logger="services.daemon.processor"):
        with patch.object(FindingProcessor, "_resolve_triage_target", return_value=None):
            for _ in range(4):
                asyncio.run(processor._get_ai_triage("p"))
        assert len(_records(caplog, logging.ERROR)) == 1
        with patch.object(
            FindingProcessor, "_resolve_triage_target", return_value=("p", "m")
        ):
            asyncio.run(processor._get_ai_triage("p"))
    assert len(_records(caplog, logging.ERROR)) == 1
    assert len(_records(caplog, logging.INFO)) == 1


def test_enrichment_breaker_logs_error_on_trip_and_info_on_resume(caplog):
    processor = FindingProcessor(ProcessingConfig(auto_triage_enabled=True))
    processor._data_service = None
    with caplog.at_level(logging.INFO, logger="services.daemon.processor"):
        for _ in range(8):
            processor._note_enrich_failure("f")
        assert len(_records(caplog, logging.ERROR)) == 1

        processor._enrich_paused_until = 0.0
        with patch.object(
            FindingProcessor,
            "_triage_finding",
            AsyncMock(return_value={"finding_id": "f", "ai_triage": {"x": 1}}),
        ), patch.object(FindingProcessor, "_evaluate_for_response", AsyncMock()):
            asyncio.run(processor._enrich_in_background({"finding_id": "f"}))
    assert any("resumed" in r.getMessage() for r in _records(caplog, logging.INFO))


def test_mirror_unreachable_is_one_error_then_one_recovery(caplog):
    with caplog.at_level(logging.INFO, logger="core.llm.bifrost.mirror"):
        with patch.object(mirror, "_get", AsyncMock(side_effect=OSError("down"))):
            for _ in range(3):
                assert asyncio.run(mirror.reconcile_all()) is None
        assert len(_records(caplog, logging.ERROR)) == 1
        with patch.object(mirror, "_get", AsyncMock(return_value={"providers": []})):
            asyncio.run(mirror.reconcile_all())
    assert len(_records(caplog, logging.ERROR)) == 1
    assert len(_records(caplog, logging.INFO)) == 1


def test_rate_refresh_failure_is_one_error_then_recovers(caplog):
    from core.llm.bifrost import admin

    db = Mock()
    db._engine = object()
    db.session_scope.side_effect = RuntimeError("db down")
    with caplog.at_level(logging.INFO, logger="core.llm.bifrost.admin"), patch(
        "core.storage.connection.get_db_manager", return_value=db
    ):
        for _ in range(3):
            asyncio.run(admin.refresh_gateway_rates())
        assert len(_records(caplog, logging.ERROR)) == 1


def test_reminder_every_n_failures(caplog):
    log = logging.getLogger("t")
    with caplog.at_level(logging.INFO, logger="t"):
        for _ in range(outage.REMINDER_EVERY):
            outage.report_outage(log, "k", "down: %s", "why")
    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 2 and "still failing" in errors[1].getMessage()
