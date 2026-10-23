"""Deleting a conversation starts a new ledger run and leaves the old one (#2856).

The agent loads earlier tool reads by run id. Generation 0 keeps
``run_id_for(session_id)``, which is where those reads already are. A delete
increments the generation, so the next turn is a different run and the old
reads are not on it. The ledger rows stay.
"""

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

TOOL_READ = "CASE-9 was open under the old conversation"


def _row(session_id: str) -> SimpleNamespace:
    with get_db_manager().session_scope() as session:
        conv = session.get(Conversation, session_id)
        assert conv is not None
        return SimpleNamespace(
            generation=conv.generation,
            deleted_at=conv.deleted_at,
            title=conv.title,
            case_id=conv.case_id,
            page_context=conv.page_context,
            message_count=conv.message_count,
        )


def _ensure_ledger() -> None:
    with get_db_manager().session_scope() as session:
        session.execute(text("""
                CREATE TABLE IF NOT EXISTS agent_events (
                    run_id uuid NOT NULL,
                    seq integer NOT NULL,
                    kind text NOT NULL,
                    payload jsonb NOT NULL,
                    PRIMARY KEY (run_id, seq)
                )
                """))


def _write_read(session_id: str) -> str:
    """One tool read on the generation-0 run. Returns that run id."""
    _ensure_ledger()
    run_id = run_id_for(session_id)
    with get_db_manager().session_scope() as session:
        session.execute(
            text(
                "INSERT INTO agent_events (run_id, seq, kind, payload) "
                "VALUES (CAST(:run_id AS uuid), 1, 'call', CAST(:payload AS jsonb))"
            ),
            {
                "run_id": run_id,
                "payload": json.dumps(
                    {"tool": "lookup_case", "exchange": 1, "result": TOOL_READ}
                ),
            },
        )
    return run_id


def _reads(run_id: str) -> list[str]:
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            text(
                "SELECT payload->>'result' FROM agent_events "
                "WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq"
            ),
            {"run_id": run_id},
        ).scalars()
        return list(rows)


def _user(user_id: str):
    return SimpleNamespace(username="owner", user_id=user_id)


async def _send(monkeypatch, session_id: str, user_id: str, messages: list[dict]):
    sent: dict = {}

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
        claude.ChatRequest(messages=messages, session_id=session_id),
        current_user=_user(user_id),
        registry=MagicMock(),
    )
    async for _ in response.body_iterator:
        pass
    return sent


def _follow_up(question: str) -> list[dict]:
    return [
        {"role": "user", "content": "look up the case"},
        {"role": "assistant", "content": "I looked it up."},
        {"role": "user", "content": question},
    ]


@pytest.mark.asyncio
async def test_a_deleted_session_starts_a_new_run_and_keeps_the_ledger(monkeypatch):
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    other = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"

    conversation_service.ensure_conversation(
        session_id,
        owner,
        first_user_text="look up the case",
        case_id="CASE-9",
        page_context="cases",
    )
    conversation_service.append_message(session_id, "user", "look up the case")
    conversation_service.append_message(
        session_id, "assistant", "I looked it up.", tool_calls=[{"name": "lookup_case"}]
    )
    old_run = _write_read(session_id)

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
    assert conversation_service.list_conversations(owner, q="look up") == []
    assert conversation_service.list_conversations(owner, include_archived=True) == []
    assert _reads(old_run) == [TOOL_READ]

    sent = await _send(monkeypatch, session_id, owner, _follow_up("and the owner?"))
    new_run = run_id_for(f"{session_id}:1")
    assert sent["run_id"] == new_run
    assert sent["run_id"] != old_run
    assert TOOL_READ not in json.dumps(sent["turns"])
    assert _reads(new_run) == []
    assert _reads(old_run) == [TOOL_READ]

    claude._persist_chat_turn(
        session_id=session_id,
        user_id=owner,
        agent_id=None,
        model="m",
        user_text="and the owner?",
        assistant_text="no one new",
        assistant_thinking=None,
        tool_calls=[],
        complete=True,
    )
    revived = conversation_service.get_conversation(session_id, owner)
    assert revived is not None
    assert revived["title"] == "and the owner?"
    assert [m["content"] for m in revived["messages"]] == [
        "and the owner?",
        "no one new",
    ]
    kept = _row(session_id)
    assert kept.generation == 1
    assert kept.deleted_at is None

    assert conversation_service.delete(session_id, owner) is True
    again = _row(session_id)
    assert again.generation == 2
    assert again.deleted_at is not None
    assert again.message_count == 0
    assert conversation_service.get_conversation(session_id, owner) is None
    assert _reads(old_run) == [TOOL_READ]

    resent = await _send(monkeypatch, session_id, owner, _follow_up("once more"))
    assert resent["run_id"] == run_id_for(f"{session_id}:2")
    assert resent["run_id"] != new_run
    assert _reads(old_run) == [TOOL_READ]
    assert _reads(run_id_for(f"{session_id}:2")) == []


@pytest.mark.asyncio
async def test_a_conversation_that_was_never_deleted_keeps_its_reads(monkeypatch):
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"
    fresh = f"conv-{uuid.uuid4().hex[:8]}"

    conversation_service.ensure_conversation(
        session_id, owner, first_user_text="look up the case"
    )
    old_run = _write_read(session_id)
    assert _row(session_id).generation == 0

    sent = await _send(monkeypatch, session_id, owner, _follow_up("what did you find?"))
    assert sent["run_id"] == run_id_for(session_id) == old_run
    assert _reads(sent["run_id"]) == [TOOL_READ]

    missing = await _send(
        monkeypatch, fresh, owner, [{"role": "user", "content": "hi"}]
    )
    assert missing["run_id"] == run_id_for(fresh)


def test_a_deleted_conversation_cannot_be_edited_or_deleted_again():
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"

    conversation_service.ensure_conversation(
        session_id, owner, first_user_text="look up the case"
    )
    assert conversation_service.delete(session_id, owner) is True
    before = _row(session_id)

    assert conversation_service.rename(session_id, owner, "renamed") is None
    assert conversation_service.set_case_id(session_id, owner, "C-9") is None
    assert conversation_service.set_archived(session_id, owner, True) is None
    assert conversation_service.delete(session_id, owner) is False
    assert conversation_service.get_conversation(session_id, owner) is None

    after = _row(session_id)
    assert after.generation == before.generation == 1
    assert after.case_id is None
    assert after.title is None
    assert after.deleted_at is not None
