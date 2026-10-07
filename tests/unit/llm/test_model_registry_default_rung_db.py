"""The resolution chain's last rung picks a provider that can route (#1005).

With no assignment, ``resolve_model_for_component`` used to end at the default
*Anthropic* row whether or not it was active, so an install whose only working
provider is Ollama resolved to a retired ``bifrost-anthropic`` and its
``claude-*`` model. DB-backed on purpose: the rule lives in the ``WHERE`` clause
of ``get_default_provider_spec``, and a stub would assert nothing about it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.llm.providers.registry import ModelRegistry
from core.storage.connection import get_db_session
from core.storage.models import AIModelConfig, LLMProviderConfig
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def session():
    db = get_db_session()

    def _clear():
        db.query(AIModelConfig).delete()
        db.query(LLMProviderConfig).delete()
        db.commit()

    try:
        _clear()
        yield db
    finally:
        db.rollback()
        _clear()
        db.close()


def _provider(session, provider_id, provider_type, model, *, is_active, age):
    session.add(
        LLMProviderConfig(
            provider_id=provider_id,
            provider_type=provider_type,
            name=provider_id,
            default_model=model,
            is_active=is_active,
            is_default=True,
            config={},
            created_at=utcnow() - timedelta(minutes=age),
        )
    )
    session.commit()


def test_retired_anthropic_default_does_not_outrank_active_ollama(session):
    # The Anthropic row is older, so it would win the created_at tiebreak if
    # is_active were not honoured.
    _provider(
        session,
        "bifrost-anthropic",
        "anthropic",
        "claude-sonnet-4-6",
        is_active=False,
        age=10,
    )
    _provider(session, "bifrost-ollama", "ollama", "llama3.1:8b", is_active=True, age=1)
    assert ModelRegistry().resolve_model_for_component("chat_default") == (
        "bifrost-ollama",
        "llama3.1:8b",
    )


def test_active_anthropic_default_still_resolves(session):
    _provider(
        session,
        "bifrost-anthropic",
        "anthropic",
        "claude-sonnet-4-6",
        is_active=True,
        age=1,
    )
    assert ModelRegistry().resolve_model_for_component("triage") == (
        "bifrost-anthropic",
        "claude-sonnet-4-6",
    )


def test_nothing_active_resolves_to_none(session):
    _provider(
        session,
        "bifrost-anthropic",
        "anthropic",
        "claude-sonnet-4-6",
        is_active=False,
        age=1,
    )
    assert ModelRegistry().resolve_model_for_component("chat_default") is None
