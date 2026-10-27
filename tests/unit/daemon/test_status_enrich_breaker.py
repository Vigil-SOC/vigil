"""Enrichment breaker fields on GET /status."""

import json
import time
from datetime import datetime

import pytest

from core.time import utcnow
from services.daemon.config import MetricsConfig, ProcessingConfig
from services.daemon.metrics import MetricsServer
from services.daemon.processor import FindingProcessor, _ENRICH_BREAKER_THRESHOLD

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr("services.daemon.metrics.configured_redis_url", lambda: None)


def _processor():
    return FindingProcessor(
        ProcessingConfig(auto_triage_enabled=False, auto_enrich_enabled=False)
    )


async def _status(processor):
    server = MetricsServer(MetricsConfig())
    server.processor = processor
    return json.loads((await server._handle_status(None)).text)


@pytest.mark.asyncio
async def test_tripped_breaker_reports_a_future_pause_and_recovery_clears_it():
    processor = _processor()
    for _ in range(_ENRICH_BREAKER_THRESHOLD):
        processor._note_enrich_failure("f-1")
    deadline = processor._enrich_paused_until

    body = await _status(processor)
    assert body["processor"]["enrich_breaker_tripped"] is True
    paused = datetime.fromisoformat(body["processor"]["enrich_paused_until"])
    assert paused > utcnow()

    # Recovery clears the flag and leaves the monotonic deadline alone.
    processor._enrich_breaker_tripped = False
    body = await _status(processor)
    assert body["processor"]["enrich_breaker_tripped"] is False
    assert body["processor"]["enrich_paused_until"] is None
    assert processor._enrich_paused_until == deadline


@pytest.mark.asyncio
async def test_open_breaker_past_its_deadline_reports_null():
    processor = _processor()
    processor._enrich_breaker_tripped = True
    processor._enrich_paused_until = time.monotonic() - 1
    body = await _status(processor)
    assert body["processor"]["enrich_breaker_tripped"] is True
    assert body["processor"]["enrich_paused_until"] is None
