"""Unit tests for the LLM provider CRUD API (GH #88).

These tests mock the DB session and secrets_manager — no live Postgres
required. CRUD semantics, secret persistence, and provider-type guards
are exercised.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Importing the router pulls in auth_service, which raises at import time if
# DEV_MODE is false and JWT_SECRET_KEY is unset. Default to dev for tests.
os.environ.setdefault("DEV_MODE", "true")

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

from backend.api.llm_providers import router as llm_providers_router
from backend.middleware.auth import get_current_active_user
from database.connection import get_db
from database.models import LLMProviderConfig, User


pytestmark = pytest.mark.unit


def _fake_admin_user() -> User:
    """Stand-in admin user for tests that bypass real JWT/cookie auth."""
    return User(
        user_id="test-admin",
        username="test-admin",
        email="admin@test.local",
        password_hash="",
        role_id="admin-role",
        is_active=True,
        mfa_enabled=False,
    )


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

    @property
    def no_autoflush(self):
        # Real Session.no_autoflush is a context manager; the in-memory fake
        # has no autoflush to suppress, so a no-op context is faithful enough.
        from contextlib import nullcontext

        return nullcontext()

    def flush(self):
        return None

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

    app.dependency_overrides[get_db] = _get_session
    # Bypass cookie/JWT auth for unit tests — the security-coverage tests
    # in tests/security/ exercise the real path.
    app.dependency_overrides[get_current_active_user] = _fake_admin_user

    # Permission checks read DEV_MODE at call time; force-allow in tests.
    with patch(
        "backend.services.auth_service.AuthService.check_permission",
        return_value=True,
    ):
        yield TestClient(app)


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

    with patch("backend.api.llm_providers.set_secret", side_effect=fake_set), patch(
        "backend.api.llm_providers.get_secret", side_effect=fake_get
    ), patch("backend.api.llm_providers.delete_secret", side_effect=fake_delete):
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


@pytest.fixture()
def bifrost_pushes():
    """Capture every push_provider_key(provider_type, value) call."""
    calls = []

    def fake_push(provider_type: str, value: str) -> bool:
        calls.append((provider_type, value))
        return True

    with patch("backend.api.llm_providers.push_provider_key", side_effect=fake_push):
        yield calls


def test_delete_last_of_type_clears_bifrost_key(client, secrets_store, bifrost_pushes):
    """Deleting the only provider of a type blanks the shared Bifrost key."""
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-prod",
            "provider_type": "openai",
            "name": "OpenAI",
            "default_model": "gpt-4o",
            "api_key": "sk-only",
        },
    )
    bifrost_pushes.clear()  # drop the create-time push

    r = client.delete("/api/llm/providers/openai-prod")
    assert r.status_code == 200
    # Last provider of its type → clear the credential.
    assert ("openai", "") in bifrost_pushes
    assert ("openai", "sk-only") not in bifrost_pushes


def test_delete_with_sibling_repushes_sibling_key(
    client, secrets_store, bifrost_pushes
):
    """Deleting one provider re-pushes a same-type sibling's key, never blank."""
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-a",
            "provider_type": "openai",
            "name": "A",
            "default_model": "gpt-4o",
            "api_key": "sk-aaa",
        },
    )
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-b",
            "provider_type": "openai",
            "name": "B",
            "default_model": "gpt-4o",
            "api_key": "sk-bbb",
        },
    )
    bifrost_pushes.clear()

    r = client.delete("/api/llm/providers/openai-a")
    assert r.status_code == 200
    # Sibling b survives with its key → re-push it, do NOT blank the type.
    assert ("openai", "sk-bbb") in bifrost_pushes
    assert ("openai", "") not in bifrost_pushes
    # The deleted provider's own secret is still removed.
    assert "llm_provider_openai-a_api_key" not in secrets_store


def test_update_clear_with_sibling_repushes_sibling_key(
    client, secrets_store, bifrost_pushes
):
    """Clearing one provider's key re-pushes a same-type sibling's key."""
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-a",
            "provider_type": "openai",
            "name": "A",
            "default_model": "gpt-4o",
            "api_key": "sk-aaa",
        },
    )
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-b",
            "provider_type": "openai",
            "name": "B",
            "default_model": "gpt-4o",
            "api_key": "sk-bbb",
        },
    )
    bifrost_pushes.clear()

    r = client.put("/api/llm/providers/openai-a", json={"api_key": ""})
    assert r.status_code == 200
    assert r.json()["has_api_key"] is False
    # openai-a's own secret is gone, but the shared Bifrost key falls back
    # to sibling b's rather than being blanked.
    assert "llm_provider_openai-a_api_key" not in secrets_store
    assert ("openai", "sk-bbb") in bifrost_pushes
    assert ("openai", "") not in bifrost_pushes


def test_update_clear_last_of_type_blanks_bifrost_key(
    client, secrets_store, bifrost_pushes
):
    """Clearing the only provider's key blanks the shared Bifrost key."""
    client.post(
        "/api/llm/providers/",
        json={
            "provider_id": "openai-solo",
            "provider_type": "openai",
            "name": "Solo",
            "default_model": "gpt-4o",
            "api_key": "sk-solo",
        },
    )
    bifrost_pushes.clear()

    r = client.put("/api/llm/providers/openai-solo", json={"api_key": ""})
    assert r.status_code == 200
    assert ("openai", "") in bifrost_pushes


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
