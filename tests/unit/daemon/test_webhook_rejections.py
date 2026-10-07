"""The daemon ingest webhook logs and counts 401/503 rejections, rate-limited."""

import asyncio
import logging
import socket
from unittest.mock import patch

import pytest
from aiohttp import ClientSession

from core import webhook_rejections as wr
from services.daemon.config import PollingConfig
from services.daemon.poller import DataPoller


class _Config:
    def get_disabled_integration_ids(self):
        return {"crowdstrike"}


async def _post(session, port, body, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for _ in range(50):
        try:
            async with session.post(
                f"http://127.0.0.1:{port}/ingest", json=body, headers=headers
            ) as resp:
                return resp.status
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError("webhook server did not start")


@pytest.mark.asyncio
async def test_rejections_logged_once_per_window_and_counted(monkeypatch, caplog):
    wr._counts.clear()
    wr._log_state.clear()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    poller = DataPoller(PollingConfig(webhook_token="good", webhook_port=port))
    caplog.set_level(logging.WARNING)

    def warnings():
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    shutdown = asyncio.Event()
    server = asyncio.create_task(poller._run_webhook_server(shutdown))
    try:
        with patch(
            "core.storage.config_service.get_config_service", return_value=_Config()
        ):
            async with ClientSession() as session:
                for _ in range(100):
                    assert await _post(session, port, {}, token="bad") == 401
                assert len(warnings()) == 1
                assert "endpoint=daemon/ingest reason=bad_token" in warnings()[0]
                assert "source_ip=127.0.0.1" in warnings()[0]
                assert "bad" not in warnings()[0].replace("bad_token", "")

                body = {"finding_id": "f-1", "data_source": "crowdstrike"}
                assert await _post(session, port, body, token="good") == 503
                assert "reason=disabled" in warnings()[-1]
                assert "crowdstrike" in warnings()[-1]

            # Missing token: every request 503s, but only one log line.
            poller.config.webhook_token = ""
            async with ClientSession() as session:
                for _ in range(20):
                    assert await _post(session, port, {}, token="x") == 503
            no_secret = [w for w in warnings() if "reason=no_secret" in w]
            assert len(no_secret) == 1
            assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

        assert poller.stats["webhook_rejections"] == {
            "daemon/ingest": {"bad_token": 100, "disabled": 1, "no_secret": 20}
        }
    finally:
        shutdown.set()
        await server
