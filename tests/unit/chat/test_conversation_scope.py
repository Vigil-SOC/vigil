"""History search stays on the caller's rows (#1328)."""

from __future__ import annotations

import uuid

import pytest

from core.chat import conversation_service
from core.storage.connection import get_db_manager
from core.storage.models import Conversation

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
