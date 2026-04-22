"""Unit tests for the per-component AI config API (GH #89).

DB access is mocked with an in-memory fake session so the tests don't
require a live Postgres. Model-listing endpoints are exercised with the
registry stubbed to avoid hitting external provider APIs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

from backend.api.ai_config import router as ai_config_router  # noqa: E402
from database.connection import get_db_session  # noqa: E402
from database.models import AIModelConfig, LLMProviderConfig  # noqa: E402
from services.model_registry import ModelInfo  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeSession:
    def __init__(self):
        self.assignments: Dict[str, AIModelConfig] = {}
        self.providers: Dict[str, LLMProviderConfig] = {}

    # --- get (PK lookup) ---
    def get(self, model, pk):
        if model is AIModelConfig:
            return self.assignments.get(pk)
        if model is LLMProviderConfig:
            return self.providers.get(pk)
        return None

    # --- query().all() ---
    class _Query:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return list(self.rows)

    def query(self, model):
        if model is AIModelConfig:
            return _FakeSession._Query(self.assignments.values())
        if model is LLMProviderConfig:
            return _FakeSession._Query(self.providers.values())
        return _FakeSession._Query([])

    def add(self, row):
        if isinstance(row, AIModelConfig):
            self.assignments[row.component] = row

    def delete(self, row):
        if isinstance(row, AIModelConfig):
            self.assignments.pop(row.component, None)

    def commit(self):
        pass

    def refresh(self, row):
        # to_dict() reads updated_at — populate so response serialization works.
        import datetime

        if hasattr(row, "updated_at") and row.updated_at is None:
            row.updated_at = datetime.datetime.utcnow()


@pytest.fixture()
def session() -> _FakeSession:
    s = _FakeSession()
    s.providers["anthropic-default"] = LLMProviderConfig(
        provider_id="anthropic-default",
        provider_type="anthropic",
        name="Anthropic (default)",
        default_model="claude-sonnet-4-5-20250929",
        is_active=True,
        is_default=True,
        config={},
    )
    s.providers["ollama-local"] = LLMProviderConfig(
        provider_id="ollama-local",
        provider_type="ollama",
        name="Local Ollama",
        base_url="http://localhost:11434",
        default_model="llama3:latest",
        is_active=True,
        is_default=True,
        config={},
    )
    return s


@pytest.fixture()
def client(session):
    app = FastAPI()
    app.include_router(ai_config_router, prefix="/api/ai")

    def _get_session():
        return session

    app.dependency_overrides[get_db_session] = _get_session
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/ai/config
# ---------------------------------------------------------------------------


def test_get_config_empty(client):
    r = client.get("/api/ai/config")
    assert r.status_code == 200
    body = r.json()
    assert "components" in body
    assert body["assignments"] == {}
    assert "chat_default" in body["components"]


# ---------------------------------------------------------------------------
# PUT /api/ai/config/{component}
# ---------------------------------------------------------------------------


def test_put_component_creates_assignment(client, session):
    r = client.put(
        "/api/ai/config/triage",
        json={"provider_id": "ollama-local", "model_id": "llama3:latest"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["component"] == "triage"
    assert body["provider_id"] == "ollama-local"
    assert body["model_id"] == "llama3:latest"
    assert "triage" in session.assignments


def test_put_component_rejects_unknown_component(client):
    r = client.put(
        "/api/ai/config/totally-not-real",
        json={"provider_id": "anthropic-default", "model_id": "x"},
    )
    assert r.status_code == 400


def test_put_component_rejects_unknown_provider(client):
    r = client.put(
        "/api/ai/config/triage",
        json={"provider_id": "does-not-exist", "model_id": "x"},
    )
    assert r.status_code == 400


def test_put_component_rejects_inactive_provider(client, session):
    session.providers["anthropic-default"].is_active = False
    r = client.put(
        "/api/ai/config/summarization",
        json={"provider_id": "anthropic-default", "model_id": "x"},
    )
    assert r.status_code == 400


def test_put_component_updates_existing(client, session):
    # seed
    client.put(
        "/api/ai/config/summarization",
        json={"provider_id": "anthropic-default", "model_id": "a"},
    )
    # update
    r = client.put(
        "/api/ai/config/summarization",
        json={"provider_id": "anthropic-default", "model_id": "b"},
    )
    assert r.status_code == 200
    assert session.assignments["summarization"].model_id == "b"


# ---------------------------------------------------------------------------
# DELETE /api/ai/config/{component}
# ---------------------------------------------------------------------------


def test_delete_clears_assignment(client, session):
    client.put(
        "/api/ai/config/summarization",
        json={"provider_id": "anthropic-default", "model_id": "x"},
    )
    r = client.delete("/api/ai/config/summarization")
    assert r.status_code == 200
    assert r.json()["cleared"] is True
    assert "summarization" not in session.assignments


def test_delete_missing_is_idempotent(client):
    r = client.delete("/api/ai/config/triage")
    assert r.status_code == 200
    assert r.json()["cleared"] is False


# ---------------------------------------------------------------------------
# GET /api/ai/models — registry stubbed so no external calls happen
# ---------------------------------------------------------------------------


def test_list_models(client):
    stub_models: List[ModelInfo] = [
        ModelInfo(
            model_id="claude-sonnet-4-5-20250929",
            provider_id="anthropic-default",
            provider_type="anthropic",
            display_name="Claude Sonnet 4.5",
            context_window=200_000,
            input_cost_per_1k=0.003,
            output_cost_per_1k=0.015,
            supports_tools=True,
            supports_thinking=True,
            supports_vision=True,
        ),
        ModelInfo(
            model_id="llama3:latest",
            provider_id="ollama-local",
            provider_type="ollama",
            display_name="llama3:latest",
            context_window=0,
            input_cost_per_1k=0.0,
            output_cost_per_1k=0.0,
            supports_tools=False,
            supports_thinking=False,
            supports_vision=False,
        ),
    ]

    async def fake_list():
        return stub_models

    with patch(
        "services.model_registry.ModelRegistry.list_available_models",
        side_effect=fake_list,
    ):
        r = client.get("/api/ai/models")

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["models"]) == 2
    assert {m["provider_type"] for m in body["models"]} == {"anthropic", "ollama"}


def test_model_info_404_when_missing(client):
    async def fake_list():
        return []

    with patch(
        "services.model_registry.ModelRegistry.list_available_models",
        side_effect=fake_list,
    ):
        r = client.get("/api/ai/models/nothing/info")

    assert r.status_code == 404
