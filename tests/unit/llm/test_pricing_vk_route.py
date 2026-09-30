"""GET /internal/pricing/vk: the key the agent layer sends as x-bf-vk (#1266).

The route answers should_enforce()'s decision, so the agent follows the same
bypass table test_budget_service.py pins for the router.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents import internal_auth
from core.llm.cost.pricing_router import router

pytestmark = pytest.mark.unit


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "secret")
    app = FastAPI()
    app.include_router(router, prefix="/internal/pricing")
    return TestClient(app)


def _vk(client, settings, token="secret"):
    with patch("core.llm.cost.budget._get_settings", return_value=settings):
        return client.get(
            "/internal/pricing/vk", headers={"Authorization": f"Bearer {token}"}
        )


def test_enforced_key_is_handed_over(client, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    response = _vk(client, {"default_vk": " sk-bf-real "})
    assert response.status_code == 200
    assert response.json() == {"vk": "sk-bf-real"}


@pytest.mark.parametrize("env", ["DEV_MODE", "LLM_BUDGET_UNLIMITED"])
def test_bypass_sends_no_key(client, monkeypatch, env):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    monkeypatch.setenv(env, "true")
    assert _vk(client, {"default_vk": "sk-bf-real"}).json() == {"vk": None}


def test_no_key_configured_sends_none(client, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    assert _vk(client, {"default_vk": ""}).json() == {"vk": None}


def test_a_bad_token_is_refused(client):
    assert _vk(client, {"default_vk": "sk-bf-real"}, token="wrong").status_code == 401
