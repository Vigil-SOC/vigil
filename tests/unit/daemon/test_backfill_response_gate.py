"""The sweep rates bulk imports, but never responds to them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.daemon.config import ProcessingConfig
from services.daemon.processor import BACKFILL_SOURCE, FindingProcessor

pytestmark = pytest.mark.unit


def _processor(**config) -> FindingProcessor:
    processor = FindingProcessor(
        ProcessingConfig(auto_triage_enabled=False, auto_enrich_enabled=False, **config)
    )
    processor._evaluate_for_response = AsyncMock()
    return processor


def _finding(*, bulk_imported, timestamp="2026-09-01T00:00:00+00:00"):
    return {
        "finding_id": "f1",
        "severity": "high",
        "timestamp": timestamp,
        "bulk_imported": bulk_imported,
    }


async def test_a_swept_import_is_rated_but_not_responded_to():
    processor = FindingProcessor(
        ProcessingConfig(auto_triage_enabled=True, auto_enrich_enabled=False)
    )
    processor._evaluate_for_response = AsyncMock()
    processor._triage_finding = AsyncMock(
        side_effect=lambda f: {**f, "ai_triage": {"severity": "high"}}
    )
    processor._update_finding = AsyncMock()
    processor._data_service = object()

    await processor._enrich_in_background(_finding(bulk_imported=True), BACKFILL_SOURCE)

    processor._triage_finding.assert_awaited_once()
    processor._update_finding.assert_awaited_once()
    processor._evaluate_for_response.assert_not_awaited()
    assert processor.stats["import_not_responded"] == 1


async def test_a_swept_live_finding_with_no_event_time_is_responded_to():
    # LogLM rows can carry no event time; one re-rated after an outage still acts.
    processor = _processor()

    await processor._enrich_in_background(
        _finding(bulk_imported=False, timestamp=None), BACKFILL_SOURCE
    )

    processor._evaluate_for_response.assert_awaited_once()


async def test_a_live_payload_cannot_mark_itself_an_import():
    processor = _processor()

    await processor._enrich_in_background(_finding(bulk_imported=True), "webhook")

    processor._evaluate_for_response.assert_awaited_once()


async def test_the_sweep_tags_what_it_spawns():
    processor = FindingProcessor(ProcessingConfig(enrich_backfill_interval=0.01))
    shutdown = asyncio.Event()
    processor._data_service = SimpleNamespace(
        get_findings_missing_enrichment=lambda **_: [{"finding_id": "f1"}]
    )
    sources = []

    async def spawn(finding, source=None):
        sources.append(source)
        shutdown.set()

    processor._spawn_enrich = spawn

    await asyncio.wait_for(processor._backfill_loop(shutdown), timeout=5)

    assert sources == [BACKFILL_SOURCE]
