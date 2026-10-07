"""The processor builds one IngestionService and reuses it across stores.

Building one health-checks the database and logs, so it must not happen per
finding. A service built while the database was down is not cached, so a
later store can pick the database back up.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest

from services.daemon.config import ProcessingConfig
from services.daemon.processor import FindingProcessor

pytestmark = pytest.mark.unit


class _Service:
    def __init__(self, use_database: bool = True):
        self.use_database = use_database
        self.threads: List[int] = []
        self.finding_ids: List[str] = []

    def ingest_finding(self, finding: Dict[str, Any]) -> bool:
        self.threads.append(threading.get_ident())
        self.finding_ids.append(finding["finding_id"])
        return self.use_database


def _processor() -> FindingProcessor:
    return FindingProcessor(
        ProcessingConfig(
            auto_triage_enabled=False,
            auto_enrich_enabled=False,
            max_concurrent_tasks=1,
        )
    )


def _patch_factory(monkeypatch, factory) -> None:
    monkeypatch.setattr("core.ingestion.ingestion_service.IngestionService", factory)


@pytest.mark.asyncio
async def test_service_is_built_once_across_stores(monkeypatch):
    built: List[_Service] = []

    def factory():
        built.append(_Service())
        return built[-1]

    _patch_factory(monkeypatch, factory)
    processor = _processor()

    for i in range(5):
        assert await processor._store_finding({"finding_id": f"f-{i}"}) is True

    assert len(built) == 1
    assert built[0].finding_ids == [f"f-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_ingest_runs_off_the_event_loop(monkeypatch):
    service = _Service()
    _patch_factory(monkeypatch, lambda: service)
    processor = _processor()

    await processor._store_finding({"finding_id": "f-1"})

    assert service.threads
    assert threading.get_ident() not in service.threads


@pytest.mark.asyncio
async def test_service_built_with_database_down_is_not_cached(monkeypatch):
    built: List[_Service] = []

    def factory():
        # First build finds the database down; the second finds it back.
        built.append(_Service(use_database=bool(built)))
        return built[-1]

    _patch_factory(monkeypatch, factory)
    processor = _processor()

    assert await processor._store_finding({"finding_id": "f-1"}) is False
    assert await processor._store_finding({"finding_id": "f-2"}) is True
    assert await processor._store_finding({"finding_id": "f-3"}) is True
    assert len(built) == 2
    assert built[1].finding_ids == ["f-2", "f-3"]


@pytest.mark.asyncio
async def test_constructor_error_is_a_failed_store_then_rebuilt(monkeypatch):
    built: List[_Service] = []
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("database connection refused")
        built.append(_Service())
        return built[-1]

    _patch_factory(monkeypatch, factory)
    processor = _processor()

    assert await processor._store_finding({"finding_id": "f-1"}) is False
    assert await processor._store_finding({"finding_id": "f-2"}) is True
    assert await processor._store_finding({"finding_id": "f-3"}) is True
    assert attempts["n"] == 2
    assert built[0].finding_ids == ["f-2", "f-3"]
