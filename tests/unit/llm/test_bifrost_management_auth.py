"""Management calls present Bifrost's admin password. Inference calls do not."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.llm.bifrost import admin, costs, mirror
from services.api.routers import bifrost_config as proxy

pytestmark = pytest.mark.unit

PASSWORD = "mgmt-secret"


def _expected() -> str:
    token = base64.b64encode(f"vigil:{PASSWORD}".encode()).decode()
    return f"Basic {token}"


def _install(monkeypatch, transport: httpx.MockTransport) -> None:
    # One patch: the modules share the httpx module object, and wrapping the
    # wrapper passes transport twice.
    real_client, real_async = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_async(transport=transport, **kw)
    )


def test_api_requests_carry_the_password_and_inference_does_not(monkeypatch) -> None:
    monkeypatch.setenv("BIFROST_URL", "http://bifrost.test:8080")
    monkeypatch.setenv("BIFROST_ADMIN_PASSWORD", PASSWORD)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/quota"):
            body = {"is_active": True}
        elif path.endswith("/histogram/cost"):
            body = {"buckets": []}
        elif path.endswith("/models/details"):
            body = {"models": []}
        elif path == "/v1/models":
            body = {"data": []}
        else:
            body = {"providers": [], "keys": []}
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handle)
    _install(monkeypatch, transport)

    asyncio.run(admin.fetch_catalogue_models("openai"))
    asyncio.run(admin.bifrost_custom_providers())
    asyncio.run(admin.list_gateway_models("openai"))
    assert costs.histogram_cost() == {"buckets": []}
    assert costs.get_vk_quota("vk-test") == {"is_active": True}
    assert asyncio.run(mirror._get("providers")) == {"providers": [], "keys": []}

    monkeypatch.setattr(proxy, "_require_settings_admin", lambda user: None)
    app = FastAPI()
    app.include_router(proxy.router, prefix="/api/bifrost")
    app.dependency_overrides[proxy.get_current_active_user] = lambda: object()
    assert TestClient(app).get("/api/bifrost/providers").status_code == 200

    expected = _expected()
    api = [r for r in seen if r.url.path.startswith("/api/")]
    inference = [r for r in seen if r.url.path.startswith("/v1/")]
    assert {r.url.path for r in api} >= {
        "/api/models/details",
        "/api/providers",
        "/api/logs/histogram/cost",
        "/api/governance/virtual-keys/quota",
    }
    assert api and all(r.headers.get("authorization") == expected for r in api)
    assert inference and all("authorization" not in r.headers for r in inference)
    quota = next(r for r in api if r.url.path.endswith("/quota"))
    assert quota.headers["x-bf-vk"] == "vk-test"
