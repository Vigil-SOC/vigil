"""The sweep rates Findings that arrived as history, but never responds to them."""

from __future__ import annotations

import asyncio
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.time import utcnow
from services.daemon.config import ProcessingConfig
from services.daemon.processor import BACKFILL_SOURCE, FindingProcessor

pytestmark = pytest.mark.unit


def _processor(**config) -> FindingProcessor:
    processor = FindingProcessor(
        ProcessingConfig(auto_triage_enabled=False, auto_enrich_enabled=False, **config)
    )
    processor._evaluate_for_response = AsyncMock()
    return processor


def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat()


# Shaped like the sweep's rows: FindingSchema dumps datetimes as "+00:00" strings.
def _swept(*, stored_ago, event_before_stored):
    stored = utcnow() - stored_ago
    return {
        "finding_id": "f1",
        "severity": "high",
        "created_at": _iso(stored),
        "timestamp": (
            None if event_before_stored is None else _iso(stored - event_before_stored)
        ),
    }


async def test_an_upload_of_old_events_is_rated_but_not_responded_to():
    processor = FindingProcessor(
        ProcessingConfig(auto_triage_enabled=True, auto_enrich_enabled=False)
    )
    processor._evaluate_for_response = AsyncMock()
    processor._triage_finding = AsyncMock(
        side_effect=lambda f: {**f, "ai_triage": {"severity": "high"}}
    )
    processor._update_finding = AsyncMock()
    processor._data_service = object()
    finding = _swept(
        stored_ago=timedelta(hours=1), event_before_stored=timedelta(days=30)
    )

    await processor._enrich_in_background(finding, BACKFILL_SOURCE)

    processor._triage_finding.assert_awaited_once()
    processor._update_finding.assert_awaited_once()
    processor._evaluate_for_response.assert_not_awaited()
    assert processor.stats["backfill_not_responded"] == 1


async def test_a_finding_rerated_after_an_outage_still_is():
    processor = _processor()
    finding = _swept(
        stored_ago=timedelta(days=6), event_before_stored=timedelta(minutes=1)
    )

    await processor._enrich_in_background(finding, BACKFILL_SOURCE)

    processor._evaluate_for_response.assert_awaited_once()


async def test_a_swept_finding_with_no_event_time_is_not():
    processor = _processor()
    finding = _swept(stored_ago=timedelta(hours=1), event_before_stored=None)

    await processor._enrich_in_background(finding, BACKFILL_SOURCE)

    processor._evaluate_for_response.assert_not_awaited()


async def test_live_ingest_of_an_old_event_still_is():
    processor = _processor()
    finding = _swept(
        stored_ago=timedelta(minutes=1), event_before_stored=timedelta(days=30)
    )

    await processor._enrich_in_background(finding, "webhook")

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
