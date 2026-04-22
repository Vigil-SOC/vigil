"""Unit tests for the LLM provider CRUD API (GH #88).

These tests mock the DB session and secrets_manager — no live Postgres
required. CRUD semantics, secret persistence, and provider-type guards
are exercised.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

from backend.api.llm_providers import router as llm_providers_router
from database.connection import get_db_session
from database.models import LLMProviderConfig


pytestmark = pytest.mark.unit


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def order_by(self, *_a, **_kw):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Minimal SQLAlchemy-session stand-in backed by an in-memory dict."""

    def __init__(self):
        self._store: Dict[str, LLMProviderConfig] = {}
        self._added = []
        self.commits = 0

    def query(self, _model):
        return _FakeQuery(self._store.values())

    def get(self, _model, pk):
        return self._store.get(pk)

    def add(self, row):
        self._added.append(row)
        self._store[row.provider_id] = row

    def delete(self, row):
        self._store.pop(row.provider_id, None)

    def commit(self):
        self.commits += 1

    def refresh(self, _row):
        return None

    def execute(self, stmt):
        # We approximate _clear_other_defaults by flipping is_default flags
        # on rows of the same provider_type (except the keep_id). This is
        # sufficient to cover the "one default per type" invariant.
        try:
            # stmt.whereclause is a ClauseList; walk elements for .right values
            clauses = list(stmt.whereclause.clauses)
            provider_type = clauses[0].right.value
            keep_id = clauses[1].right.value
        except Exception:
            return None
        for r in list(self._store.values()):
            if r.provider_type == provider_type and r.provider_id != keep_id:
                r.is_default = False
        return None


@pytest.fixture()
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture()
def client(session):
    app = FastAPI()
    app.include_router(llm_providers_router, prefix="/api/llm/providers")

    def _get_session():
        return session

    app.dependency_overrides[get_db_session] = _get_session
    return TestClient(app)


@pytest.fixture()
def secrets_store():
    store: Dict[str, str] = {}

    def fake_set(key: str, value: str) -> bool:
        store[key] = value
        return True

    def fake_get(key: str) -> Optional[str]:
        return store.get(key)

    def fake_delete(key: str) -> bool:
        store.pop(key, None)
        return True

    with patch("backend.api.llm_providers.set_secret", side_effect=fake_set), \
         patch("backend.api.llm_providers.get_secret", side_effect=fake_get), \
         patch("backend.api.llm_providers.delete_secret", side_effect=fake_delete):
        yield store


def test_list_empty(client):
    r = client.get("/api/llm/providers/")
    assert r.status_code == 200
    assert r.json() == []


def test_create_ollama_no_key(client, secrets_store):
    r = client.post(
        "/api/llm/providers/",
        json={
            "provider_type": "ollama",
            "name": "Local Ollama",
            "base_url": "http://localhost:11434",
            "default_model": "llama3.1:8b",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["provider_id"] == "local-ollama"
    assert body["has_api_key"] is False
    assert body["provider_type"] == "ollama"
    assert secrets_store == {}


def test_create_openai_persists_key(client, secrets_store):
    r = client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-prod",
            "provider_type": "openai",
            "name": "OpenAI (prod)",
            "default_model": "gpt-4o-mini",
            "api_key": "sk-test-xyz",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_api_key"] is True
    assert secrets_store["llm_provider_openai-prod_api_key"] == "sk-test-xyz"


def test_create_rejects_unknown_type(client):
    r = client.post(
        "/api/llm/providers/",
        json={
            "provider_type": "gemini",
            "name": "x",
            "default_model": "y",
        },
    )
    assert r.status_code == 400


def test_create_rejects_duplicate_id(client, secrets_store):
    payload = {
        "provider_id": "dup",
        "provider_type": "ollama",
        "name": "a",
        "default_model": "m",
    }
    assert client.post("/api/llm/providers/", json=payload).status_code == 201
    assert client.post("/api/llm/providers/", json=payload).status_code == 409


def test_update_rotates_key_and_clears(client, secrets_store):
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-prod",
            "provider_type": "openai",
            "name": "OpenAI",
            "default_model": "gpt-4o",
            "api_key": "sk-old",
        },
    )
    # Rotate
    r = client.put(
        "/api/llm/providers/openai-prod",
        json={"api_key": "sk-new"},
    )
    assert r.status_code == 200
    assert secrets_store["llm_provider_openai-prod_api_key"] == "sk-new"

    # Clear (empty string)
    r = client.put(
        "/api/llm/providers/openai-prod",
        json={"api_key": ""},
    )
    assert r.status_code == 200
    assert r.json()["has_api_key"] is False
    assert "llm_provider_openai-prod_api_key" not in secrets_store


def test_delete_removes_secret(client, secrets_store):
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-prod",
            "provider_type": "openai",
            "name": "OpenAI",
            "default_model": "gpt-4o",
            "api_key": "sk-bye",
        },
    )
    assert "llm_provider_openai-prod_api_key" in secrets_store
    r = client.delete("/api/llm/providers/openai-prod")
    assert r.status_code == 200
    assert "llm_provider_openai-prod_api_key" not in secrets_store


def test_set_default_enforces_single_default(client, secrets_store, session):
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "ollama-a",
            "provider_type": "ollama",
            "name": "A",
            "default_model": "m",
            "is_default": True,
        },
    )
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "ollama-b",
            "provider_type": "ollama",
            "name": "B",
            "default_model": "m",
        },
    )
    r = client.post("/api/llm/providers/ollama-b/set-default")
    assert r.status_code == 200
    a = session.get(LLMProviderConfig, "ollama-a")
    b = session.get(LLMProviderConfig, "ollama-b")
    assert b.is_default is True
    assert a.is_default is False
