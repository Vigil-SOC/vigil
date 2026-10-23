"""History search and chat turns stay on the caller's rows (#1328)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from core.chat import conversation_service
from core.storage.connection import get_db_manager
from core.storage.models import Conversation
from services.api.routers import claude

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def test_q_returns_only_the_callers_rows():
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    other = f"analyst-{uuid.uuid4().hex[:8]}"
    mine = f"conv-{uuid.uuid4().hex[:8]}"
    theirs = f"conv-{uuid.uuid4().hex[:8]}"
    untouched = f"conv-{uuid.uuid4().hex[:8]}"
    with get_db_manager().session_scope() as session:
        session.add_all(
            [
                Conversation(
                    id=mine,
                    user_id=owner,
                    title="overview notes",
                    case_id="CASE-1",
                    page_context="overview",
                ),
                Conversation(
                    id=theirs,
                    user_id=other,
                    title="overview notes",
                    case_id="CASE-1",
                    page_context="overview",
                ),
                Conversation(
                    id=untouched,
                    user_id=owner,
                    title="something else",
                    case_id="CASE-9",
                    page_context="cases",
                ),
            ]
        )

    matched = conversation_service.list_conversations(owner, q="overview")
    assert [row["id"] for row in matched] == [mine]

    by_case = conversation_service.list_conversations(owner, q="CASE-9")
    assert [row["id"] for row in by_case] == [untouched]

    by_page = conversation_service.list_conversations(owner, q="cases")
    assert [row["id"] for row in by_page] == [untouched]


def test_page_sticks_and_a_blank_case_clears_without_inserting():
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"
    missing = f"conv-{uuid.uuid4().hex[:8]}"

    assert conversation_service.set_case_id(missing, owner, "CASE-1") is None
    assert conversation_service.get_conversation(missing, owner) is None

    conversation_service.ensure_conversation(
        session_id,
        owner,
        first_user_text="hello",
        page_context="overview",
        case_id="CASE-1",
    )
    conversation_service.ensure_conversation(
        session_id,
        owner,
        page_context="cases",
        case_id="",
    )
    cleared = conversation_service.get_conversation(session_id, owner)
    assert cleared is not None
    assert cleared["page_context"] == "overview"
    assert cleared["case_id"] is None

    conversation_service.ensure_conversation(session_id, owner)
    left = conversation_service.get_conversation(session_id, owner)
    assert left is not None
    assert left["page_context"] == "overview"
    assert left["case_id"] is None

    conversation_service.ensure_conversation(session_id, owner, case_id="CASE-2")
    replaced = conversation_service.get_conversation(session_id, owner)
    assert replaced is not None
    assert replaced["case_id"] == "CASE-2"
    assert replaced["page_context"] == "overview"


def _turn(session_id, user_id, text, reply, case_id):
    claude._persist_chat_turn(
        session_id=session_id,
        user_id=user_id,
        agent_id=None,
        model="m",
        user_text=text,
        assistant_text=reply,
        assistant_thinking=None,
        tool_calls=[],
        complete=True,
        case_id=case_id,
    )


def test_a_turn_on_another_users_conversation_changes_nothing():
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    other = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"

    _turn(session_id, owner, "mine", "a1", case_id="CASE-1")
    _turn(session_id, other, "injected", "a2", case_id="CASE-EVIL")

    conv = conversation_service.get_conversation(session_id, owner)
    assert [m["content"] for m in conv["messages"]] == ["mine", "a1"]
    assert conv["case_id"] == "CASE-1"
    assert conversation_service.get_conversation(session_id, other) is None


def _request(session_id):
    return claude.ChatRequest(
        messages=[{"role": "user", "content": "hi"}], session_id=session_id
    )


@pytest.mark.asyncio
async def test_the_stream_refuses_a_session_another_user_owns(monkeypatch):
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    session_id = f"conv-{uuid.uuid4().hex[:8]}"
    conversation_service.ensure_conversation(session_id, owner)
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

    with pytest.raises(HTTPException) as refused:
        await claude.chat_stream(
            _request(session_id),
            current_user=SimpleNamespace(username="mallory", user_id="someone-else"),
            registry=MagicMock(),
        )
    assert refused.value.status_code == 403

    response = await claude.chat_stream(
        _request(session_id),
        current_user=SimpleNamespace(username="owner", user_id=owner),
        registry=MagicMock(),
    )
    async for _ in response.body_iterator:
        pass
    assert sent["run_id"] == claude.run_id_for(session_id)


def test_case_id_finds_the_exact_case_past_newer_lookalikes():
    owner = f"analyst-{uuid.uuid4().hex[:8]}"
    target = f"conv-{uuid.uuid4().hex[:8]}"
    with get_db_manager().session_scope() as session:
        session.add(Conversation(id=target, user_id=owner, case_id="CASE-1"))
        session.add(
            Conversation(
                id=f"conv-{uuid.uuid4().hex[:8]}",
                user_id=f"{owner}-x",
                case_id="CASE-1",
            )
        )
    with get_db_manager().session_scope() as session:
        session.add_all(
            Conversation(
                id=f"conv-{uuid.uuid4().hex[:8]}",
                user_id=owner,
                title=f"about CASE-1 and CASE-{n}",
                case_id=f"CASE-{n}",
            )
            for n in range(100, 150)
        )

    rows = conversation_service.list_conversations(owner, case_id="CASE-1", limit=1)
    assert [row["id"] for row in rows] == [target]
