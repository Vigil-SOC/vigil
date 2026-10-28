"""GET /api/llm/health is the providers map, from the shared Bifrost snapshot.

Viewer 200, anonymous 401, and the body carries no key material. Ten calls
across this route, /api/bifrost/routability and /api/analytics/budget/quota
inside the cache window cause one routability read and one quota read.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

from core.llm.bifrost import path_health  # noqa: E402
from core.llm.bifrost.path_health_router import llm_path_health  # noqa: E402
from services.api.routers.bifrost_config import (  # noqa: E402
    routability as admin_routability,
)
from services.api.routers.budgets import get_budget_quota  # noqa: E402

pytestmark = pytest.mark.unit

ROUTABILITY = {
    "providers": {"gemini": True, "openai": False},
    "keys": {
        "sk-canary-key": {
            "provider": "gemini",
            "routable": True,
            "health": "healthy",
            "description": "sk-live-secret",
        }
    },
}
QUOTA = {"virtual_key_name": "sk-vk-name", "budgets": [{"id": "budget-1"}]}
VK = "sk-vk-canary"


def _patch_reads(monkeypatch, routability_impl, *, vk=VK, quota=QUOTA):
    counts = {"routability": 0, "quota": 0}

    async def _routability():
        counts["routability"] += 1
        return await routability_impl()

    def _quota(vk_id):
        counts["quota"] += 1
        assert vk_id == vk
        return quota

    monkeypatch.setattr("core.llm.bifrost.mirror.routability", _routability)
    monkeypatch.setattr("core.llm.bifrost.costs.get_vk_quota", _quota)
    monkeypatch.setattr("core.llm.cost.budget.read_vk", lambda: vk)
    monkeypatch.setattr(
        "core.auth.permissions.AuthService.check_permission",
        lambda *_a, **_k: True,
    )
    return counts


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    from core.config import get_settings
    from services.api.main import app as fastapi_app
    from services.api.middleware.auth import get_current_active_user, get_current_user

    get_settings.cache_clear()
    yield fastapi_app
    fastapi_app.dependency_overrides.pop(get_current_active_user, None)
    fastapi_app.dependency_overrides.pop(get_current_user, None)


def _as(app, user):
    from services.api.middleware.auth import get_current_active_user, get_current_user

    # The platform.read gate resolves the caller through get_current_user.
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user


def test_anonymous_caller_is_refused(app):
    assert TestClient(app).get("/api/llm/health").status_code == 401


def test_viewer_gets_only_the_providers_map(app, monkeypatch):
    _patch_reads(
        monkeypatch,
        lambda: _ready(ROUTABILITY),
    )
    _as(
        app,
        SimpleNamespace(user_id="viewer", role_id="role-viewer", is_active=True),
    )

    response = TestClient(app).get("/api/llm/health")

    assert response.status_code == 200
    assert response.json() == {"providers": {"gemini": True, "openai": False}}
    assert "sk-" not in response.text
    for hidden in (
        "keys",
        "virtual_key_id",
        "description",
        "budget",
        "reachable",
        "latency_ms",
    ):
        assert hidden not in response.json()


def test_ten_calls_across_the_three_routes_refresh_once(monkeypatch):
    counts = _patch_reads(monkeypatch, lambda: _slow(ROUTABILITY))
    user = SimpleNamespace(user_id="admin")

    async def ten():
        jobs = []
        for i in range(10):
            slot = i % 3
            if slot == 0:
                jobs.append(llm_path_health())
            elif slot == 1:
                jobs.append(admin_routability(user))
            else:
                jobs.append(get_budget_quota())
        return await asyncio.gather(*jobs)

    results = asyncio.run(ten())

    assert counts == {"routability": 1, "quota": 1}
    viewer = [body for body in results if set(body) == {"providers"}]
    admin = [body for body in results if "keys" in body]
    quota = [body for body in results if "configured" in body]
    assert viewer and admin and quota
    assert all("sk-" not in str(body) for body in viewer)
    assert all(
        body["keys"]["sk-canary-key"]["description"] == "sk-live-secret"
        for body in admin
    )
    assert all(
        body["virtual_key_id"] == VK and body["available"] is True for body in quota
    )


def test_unreachable_bifrost_is_502_for_admin_and_an_empty_map_for_a_viewer(
    app, monkeypatch
):
    async def down():
        raise httpx.ConnectError("refused")

    counts = _patch_reads(monkeypatch, down, vk=None, quota=None)
    client = TestClient(app)
    _as(app, SimpleNamespace(user_id="admin", role_id="role-admin", is_active=True))

    admin = client.get("/api/bifrost/routability")
    viewer = client.get("/api/llm/health")
    again = client.get("/api/bifrost/routability")

    assert admin.status_code == 502
    assert "Bifrost unreachable" in admin.json()["detail"]
    assert viewer.status_code == 200
    assert viewer.json() == {"providers": {}}
    assert again.status_code == 502
    assert counts["routability"] == 1
    assert counts["quota"] == 0


def test_a_caller_after_the_window_refreshes(monkeypatch):
    counts = _patch_reads(monkeypatch, lambda: _ready(ROUTABILITY))
    now = {"t": 0.0}
    monkeypatch.setattr(path_health.time, "monotonic", lambda: now["t"])

    async def twice():
        await path_health.read()
        now["t"] = path_health.CACHE_S - 0.1
        await path_health.read()
        now["t"] = path_health.CACHE_S
        await path_health.read()

    asyncio.run(twice())
    assert counts == {"routability": 2, "quota": 2}


async def _ready(body):
    return body


async def _slow(body):
    await asyncio.sleep(0.05)
    return body
