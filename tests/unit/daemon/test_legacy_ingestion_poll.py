"""Legacy Sentinel / Security Hub / Defender polls must survive a running loop.

``ingest_alerts`` stays synchronous and calls ``asyncio.run``. Invoking it on
the daemon loop raises ``RuntimeError`` before any vendor call, so every tick
was an error and ``last_poll_time`` never moved. The poll offloads that call
to a thread. A ``success: False`` result still raises ``IngestionError``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from services.daemon.config import PollingConfig
from services.daemon.poller import DataPoller, IngestionError

pytestmark = pytest.mark.unit


class ProbeSIEM(SIEMIngestionService):
    """Subclass whose fetch is observable and whose writes never hit a database."""

    def __init__(
        self,
        alerts: List[Dict[str, Any]],
        *,
        fail_ids: Optional[set] = None,
        explode: bool = False,
        on_fetch=None,
    ):
        super().__init__()
        self.siem_name = "Probe"
        self._alerts = alerts
        self._fail_ids = fail_ids or set()
        self._explode = explode
        self._on_fetch = on_fetch
        self.seen_limits: List[int] = []
        self.ingestion_service = MagicMock()
        self.ingestion_service.ingest_finding.return_value = True

    async def fetch_alerts(
        self,
        start_time=None,
        end_time=None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        self.seen_limits.append(limit)
        if self._on_fetch:
            self._on_fetch()
        if self._explode:
            raise RuntimeError("vendor down")
        return list(self._alerts)

    def transform_alert_to_finding(self, alert: Dict[str, Any]):
        if alert.get("id") in self._fail_ids:
            return None
        return {"finding_id": alert["id"], "title": "probe"}


def _poller(service: ProbeSIEM) -> DataPoller:
    with (
        patch("services.daemon.poller.FederationRunner"),
        patch("services.daemon.poller.RedisDedupSet"),
    ):
        poller = DataPoller(PollingConfig())
    poller._federation.is_active_for.return_value = False
    poller._azure_sentinel_service = service
    return poller


def _capture(service: ProbeSIEM) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}
    real = service.ingest_alerts

    def spy(*args, **kwargs):
        captured["result"] = real(*args, **kwargs)
        return captured["result"]

    service.ingest_alerts = spy
    return captured


@pytest.mark.asyncio
async def test_poll_inside_running_loop_ingests_fetched_alerts():
    alerts = [{"id": "a"}, {"id": "b"}]
    service = ProbeSIEM(alerts)
    captured = _capture(service)
    poller = _poller(service)

    await poller._poll_ingestion_source("azure_sentinel")

    assert service.seen_limits == [100]
    assert captured["result"]["success"] is True
    assert captured["result"]["fetched"] == 2
    assert captured["result"]["ingested"] == 2
    assert poller.stats["azure_sentinel_polls"] == 1
    assert poller.stats["azure_sentinel_findings"] == 2
    assert poller.stats["errors"] == 0
    assert service.ingestion_service.ingest_finding.call_count == 2


@pytest.mark.asyncio
async def test_partial_transform_failure_stays_success():
    service = ProbeSIEM(
        [{"id": "ok"}, {"id": "bad"}],
        fail_ids={"bad"},
    )
    captured = _capture(service)
    poller = _poller(service)

    await poller._poll_ingestion_source("azure_sentinel")

    assert captured["result"]["success"] is True
    assert captured["result"]["ingested"] == 1
    assert captured["result"]["failed"] == 1
    assert poller.stats["azure_sentinel_findings"] == 1
    assert poller.stats["dropped"] == 1
    assert poller.stats["errors"] == 0


@pytest.mark.asyncio
async def test_clean_poll_drops_nothing():
    service = ProbeSIEM([{"id": "a"}])
    poller = _poller(service)

    await poller._poll_ingestion_source("azure_sentinel")

    assert poller.stats["dropped"] == 0


@pytest.mark.asyncio
async def test_failed_ingest_raises_and_loop_does_not_stamp_last_poll():
    shutdown = asyncio.Event()
    service = ProbeSIEM([], explode=True, on_fetch=shutdown.set)
    poller = _poller(service)

    with pytest.raises(IngestionError, match="vendor down"):
        await poller._poll_ingestion_source("azure_sentinel")

    # The direct call above set the event. Clear it so the loop takes one
    # tick, then the fetch signals shutdown and the loop exits.
    shutdown.clear()
    await poller._poll_ingestion_loop("azure_sentinel", shutdown)

    assert poller.stats["errors"] == 1
    assert poller._azure_sentinel_state.last_poll_time is None
