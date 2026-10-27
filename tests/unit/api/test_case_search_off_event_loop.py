"""The command bar's case searches keep the API answering while they run (#3202).

Each search is made to sleep; a concurrent ``GET /api/health`` must still come
back at once, which only holds if the route runs off the event loop.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

pytestmark = pytest.mark.unit

BLOCK_S = 2.0


class _Storage:
    def probe_connection(self):
        pass

    def get_backend_info(self):
        return {"backend": "none", "database_available": False, "demo_mode": False}


@pytest.fixture
def app(monkeypatch):
    from fastapi import FastAPI

    from core.cases import case_search_router
    from services.api.main import health_check
    from services.api.middleware.auth import get_current_active_user

    started = {"at": None}

    def slow(*args, **kwargs):
        started["at"] = time.monotonic()
        time.sleep(BLOCK_S)
        return {"cases": [], "comments": [], "evidence": []}

    monkeypatch.setattr(case_search_router.search_service, "search_full_text", slow)
    monkeypatch.setattr(case_search_router.search_service, "search_by_ioc", slow)

    app = FastAPI()
    app.include_router(
        case_search_router.router, prefix=case_search_router.ROUTER_META.prefix
    )
    app.add_api_route("/api/health", health_check, methods=["GET"])
    app.dependency_overrides[get_current_active_user] = lambda: object()
    app.state.health_storage = _Storage()
    app.state.started = started
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/api/cases/search/full-text?query=ws-042",
        "/api/cases/search/by-ioc?ioc_value=ws-042",
    ],
)
async def test_health_answers_while_a_case_search_runs(app, path):
    started = app.state.started
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        search = asyncio.create_task(client.get(path))
        while started["at"] is None and not search.done():
            await asyncio.sleep(0.01)
        assert started["at"] is not None, (await search).text
        health = await client.get("/api/health")
        # Timed from when the search began blocking: a held loop stalls this too.
        elapsed = time.monotonic() - started["at"]
        response = await search

    assert health.status_code == 200
    assert response.status_code == 200, response.text
    assert elapsed < 0.5, f"/api/health waited {elapsed:.2f}s behind {path}"
