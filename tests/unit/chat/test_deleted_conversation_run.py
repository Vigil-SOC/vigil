"""Deleting a chat keeps its ledger rows and starts the next life on a new run."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from core.chat import conversation_service
from core.llm.chat_layers import run_id_for
from core.storage.connection import get_db_manager
from core.storage.models import Conversation
from services.api.routers import claude

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def ledger():
    """The unit database is create_all and does not model agent_events."""
    ddl = """
        CREATE TABLE agent_events (
            run_id uuid NOT NULL,
            seq integer NOT NULL,
            run_kind text NOT NULL,
            kind text NOT NULL,
            payload jsonb NOT NULL,
            schema_version integer NOT NULL,
            PRIMARY KEY (run_id, seq)
        )
    """
    engine = get_db_manager().engine
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS agent_events"))
        conn.execute(text(ddl))
    yield
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS agent_events"))


def _events(run_id: str) -> int:
    with get_db_manager().engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM agent_events WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()


def _insert_call(run_id: str) -> None:
    with get_db_manager().engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_events "
                "(run_id, seq, run_kind, kind, payload, schema_version) "
                "VALUES (:run_id, 1, 'chat', 'call', CAST(:payload AS jsonb), 1)"
            ),
            {
                "run_id": run_id,
                "payload": json.dumps(
                    {"tool": "search_cases", "exchange": 1, "result": "CASE-1"}
                ),
            },
        )


def _row(session_id: str) -> Conversation:
    with get_db_manager().session_scope() as session:
        conv = session.get(Conversation, session_id)
        session.expunge(conv)
        return conv


def _request(session_id, messages=None):
    return claude.ChatRequest(
        messages=messages or [{"role": "user", "content": "hi"}],
        session_id=session_id,
    )


async def _sent(monkeypatch, request, user_id):
    sent = {}

    async def _relay(payload, *_args):
        sent.update(payload)
        yield ""

    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda *_: (None, "m")
    )
    monkeypatch.setattr(
        claude, "provider_for", lambda _: SimpleNamespace(provider_type="gemini")
    )
    monkeypatch.setattr(claude, "model_for", lambda _p, model: model)
    monkeypatch.setattr(claude, "live_mcp_tools", lambda _: [])
    monkeypatch.setattr(claude, "_relay", _relay)
    response = await claude.chat_stream(
        request,
        current_user=SimpleNamespace(username="owner", user_id=user_id),
        registry=MagicMock(),
    )
    async for _ in response.body_iterator:
        pass
    return sent


@pytest.mark.asyncio
async def test_a_conversation_that_was_never_deleted_keeps_its_run(ledger, monkeypatch):
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"
    conversation_service.ensure_conversation(
        session_id,
        owner,
        first_user_text="where is the case",
        case_id="CASE-1",
        page_context="cases",
    )
    conversation_service.append_message(session_id, "user", "where is the case")
    old_run = run_id_for(session_id)
    _insert_call(old_run)

    missing = f"conv-{uuid.uuid4().hex[:8]}"
    fresh = await _sent(monkeypatch, _request(missing), owner)
    assert fresh["run_id"] == run_id_for(missing)

    follow = await _sent(
        monkeypatch,
        _request(
            session_id,
            [
                {"role": "user", "content": "where is the case"},
                {"role": "assistant", "content": "CASE-1"},
                {"role": "user", "content": "who owns it"},
            ],
        ),
        owner,
    )
    # The agent stitches this run's call events back on as earlier reads.
    assert follow["run_id"] == old_run
    assert _events(old_run) == 1
    assert (
        conversation_service.get_conversation(session_id, owner)["case_id"] == "CASE-1"
    )


@pytest.mark.asyncio
async def test_a_deleted_session_advances_its_run_and_keeps_the_ledger(
    ledger, monkeypatch
):
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    other = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"
    conversation_service.ensure_conversation(
        session_id,
        owner,
        first_user_text="where is the case",
        case_id="CASE-1",
        page_context="cases",
    )
    conversation_service.append_message(session_id, "user", "where is the case")
    conversation_service.append_message(session_id, "assistant", "CASE-1")
    old_run = run_id_for(session_id)
    _insert_call(old_run)

    assert conversation_service.delete(session_id, other) is False
    assert _row(session_id).generation == 0

    assert conversation_service.delete(session_id, owner) is True
    hidden = _row(session_id)
    assert hidden.generation == 1
    assert hidden.deleted_at is not None
    assert hidden.title is None
    assert hidden.case_id is None
    assert hidden.page_context is None
    assert hidden.message_count == 0
    assert conversation_service.get_conversation(session_id, owner) is None
    assert conversation_service.list_conversations(owner) == []
    assert _events(old_run) == 1

    first_life = await _sent(
        monkeypatch,
        _request(
            session_id,
            [
                {"role": "user", "content": "where is the case"},
                {"role": "assistant", "content": "CASE-1"},
                {"role": "user", "content": "who owns it"},
            ],
        ),
        owner,
    )
    assert first_life["run_id"] == run_id_for(f"{session_id}:1")
    assert first_life["run_id"] != old_run
    assert _events(first_life["run_id"]) == 0
    assert _events(old_run) == 1

    claude._persist_chat_turn(
        session_id=session_id,
        user_id=owner,
        agent_id=None,
        model="m",
        user_text="who owns it",
        assistant_text="the analyst",
        assistant_thinking=None,
        tool_calls=[],
        complete=True,
    )
    continued = _row(session_id)
    assert continued.deleted_at is None
    assert continued.generation == 1
    assert continued.title == "who owns it"
    visible = conversation_service.get_conversation(session_id, owner)
    assert [m["content"] for m in visible["messages"]] == ["who owns it", "the analyst"]

    assert conversation_service.delete(session_id, owner) is True
    assert _row(session_id).generation == 2
    assert _events(old_run) == 1

    second_life = await _sent(monkeypatch, _request(session_id), owner)
    assert second_life["run_id"] == run_id_for(f"{session_id}:2")
    assert _events(second_life["run_id"]) == 0
    assert _events(old_run) == 1
