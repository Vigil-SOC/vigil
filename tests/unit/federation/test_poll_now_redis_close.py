"""Poll Now closes its Redis clients on success and on failure.

``request_poll_now`` runs on every Poll Now click and ``_consume_poll_now``
on every runner tick. Each opens a fresh client, so one that is not closed
leaks a connection pool per call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.federation import runner as runner_mod
from core.federation.runner import FederationRunner, request_poll_now

pytestmark = pytest.mark.unit

URL = "redis://fake:6379/0"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        runner_mod, "get_settings", lambda: SimpleNamespace(redis_url=URL)
    )


def _sync_client(set_side_effect=None):
    client = MagicMock()
    client.__enter__.return_value = client
    # Mirror redis.Redis.__exit__: close, and never swallow the exception.
    client.__exit__.side_effect = lambda *a: bool(client.close()) and False
    client.set.side_effect = set_side_effect
    return client


def test_request_poll_now_closes_client_on_success(monkeypatch):
    import redis

    client = _sync_client()
    monkeypatch.setattr(redis, "from_url", MagicMock(return_value=client))

    assert request_poll_now("src-1") is True
    client.set.assert_called_once()
    assert client.set.call_args.args[0] == "vigil:federation:trigger:src-1"
    client.close.assert_called_once()


def test_request_poll_now_closes_client_when_set_raises(monkeypatch):
    import redis

    client = _sync_client(set_side_effect=ConnectionError("down"))
    monkeypatch.setattr(redis, "from_url", MagicMock(return_value=client))

    assert request_poll_now("src-1") is False
    client.close.assert_called_once()


def _async_client(getdel):
    client = MagicMock()
    client.getdel = AsyncMock(side_effect=getdel)
    client.aclose = AsyncMock()
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("value, expected", [("123", True), (None, False)])
async def test_consume_poll_now_closes_client_on_success(monkeypatch, value, expected):
    import redis.asyncio as aioredis

    client = _async_client(lambda key: value)
    monkeypatch.setattr(aioredis, "from_url", MagicMock(return_value=client))

    assert await FederationRunner(None)._consume_poll_now("src-1") is expected
    client.getdel.assert_awaited_once_with("vigil:federation:trigger:src-1")
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_poll_now_closes_client_when_getdel_raises(monkeypatch):
    import redis.asyncio as aioredis

    def boom(key):
        raise ConnectionError("down")

    client = _async_client(boom)
    monkeypatch.setattr(aioredis, "from_url", MagicMock(return_value=client))

    assert await FederationRunner(None)._consume_poll_now("src-1") is False
    client.aclose.assert_awaited_once()
