"""A database that will not answer is an error, not an empty queue (#2899)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routers import orchestrator as router
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


class _Down:
    def session_scope(self):
        raise ConnectionError("postgres unreachable")


@pytest.fixture
def orch(tmp_path):
    o = Orchestrator(OrchestratorConfig(workdir_base=str(tmp_path / "inv")))
    with patch("core.storage.connection.get_db_manager", return_value=_Down()):
        yield o


@pytest.fixture
def client(orch):
    app = FastAPI()
    app.include_router(router.router, prefix="/api/orchestrator")
    with patch.object(router, "_get_orchestrator", return_value=orch):
        yield TestClient(app, raise_server_exceptions=False)


def test_readers_raise(orch):
    with pytest.raises(ConnectionError):
        orch.get_all_investigations()
    with pytest.raises(ConnectionError):
        orch.get_investigation("inv-1")
    with pytest.raises(ConnectionError):
        orch._get_investigations_by_status("executing")


def test_list_is_a_server_error_not_an_empty_queue(client):
    assert client.get("/api/orchestrator/investigations").status_code == 500


def test_detail_is_a_server_error_not_a_404(client):
    assert client.get("/api/orchestrator/investigations/inv-1").status_code == 500


def test_kill_raises_instead_of_marking_nothing(orch):
    with pytest.raises(ConnectionError):
        asyncio.run(orch.kill())
    assert orch.enabled is False


def test_a_failed_legacy_read_does_not_stop_the_loops(orch):
    orch._enabled = True
    shutdown = asyncio.Event()
    started = []

    async def loop(event):
        started.append(1)
        shutdown.set()

    async def go():
        with (
            patch.object(orch, "_sync_enabled_from_db"),
            patch.object(orch, "_intake_loop", side_effect=loop),
            patch.object(orch, "_supervision_loop", AsyncMock()),
            patch.object(orch, "_distil_loop", AsyncMock()),
            patch.object(orch, "_abandon_in_flight"),
        ):
            await orch.run(shutdown)

    asyncio.run(go())
    assert started == [1]
