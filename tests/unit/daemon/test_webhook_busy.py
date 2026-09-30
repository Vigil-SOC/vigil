"""The ingest webhook over HTTP: 503 + Retry-After when the hand-off is full."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.daemon.config import PollingConfig
from services.daemon.poller import DataPoller

pytestmark = pytest.mark.unit

TOKEN = "t0ken"


class _Dedup:
    def __init__(self):
        self.processed: set = set()

    async def is_processed(self, key: str) -> bool:
        return key in self.processed

    async def mark_processed(self, key: str) -> None:
        self.processed.add(key)


@pytest.fixture(autouse=True)
def no_disabled_integrations(monkeypatch):
    class _Config:
        def get_disabled_integration_ids(self):
            return set()

    monkeypatch.setattr(
        "core.storage.config_service.get_config_service", lambda: _Config()
    )


def _poller(maxsize: int) -> DataPoller:
    poller = DataPoller(PollingConfig(webhook_token=TOKEN))
    poller._webhook_dedup = _Dedup()  # type: ignore[assignment]
    poller.set_output_queue(asyncio.Queue(maxsize=maxsize))
    return poller


async def _post(poller: DataPoller, body):
    async with TestClient(TestServer(poller._webhook_app())) as client:
        resp = await client.post(
            "/ingest", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        return resp.status, resp.headers.get("Retry-After"), await resp.json()


@pytest.mark.asyncio
async def test_a_push_with_room_is_accepted():
    poller = _poller(maxsize=10)

    status, _, body = await _post(poller, [{"finding_id": "a"}, {"finding_id": "b"}])

    assert status == 200
    assert body["ingested"] == 2
    assert poller._output_queue.qsize() == 2


@pytest.mark.asyncio
async def test_a_full_hand_off_answers_503_and_the_retry_loses_nothing():
    poller = _poller(maxsize=1)
    batch = [{"finding_id": "a"}, {"finding_id": "b"}]

    status, retry_after, body = await _post(poller, batch)

    assert status == 503
    assert retry_after is not None and int(retry_after) > 0
    assert body["ingested"] == 1

    # The processor drains; the sender retries the whole batch.
    first = poller._output_queue.get_nowait()
    status, _, body = await _post(poller, batch)

    assert status == 200
    assert body["ingested"] == 1
    second = poller._output_queue.get_nowait()
    assert [first["data"]["finding_id"], second["data"]["finding_id"]] == ["a", "b"]
