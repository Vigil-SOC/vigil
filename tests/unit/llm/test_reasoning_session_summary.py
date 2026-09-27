"""Session summary counts unpriced calls instead of showing them as free (#1202)."""

from __future__ import annotations

import uuid

import pytest

from core.storage.connection import get_db_manager
from core.storage.models import LLMInteractionLog
from services.api.routers.reasoning import get_session_summary

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _insert(session_id: str, rows: list[tuple[str, float | None]]) -> list[str]:
    ids = [str(uuid.uuid4()) for _ in rows]
    db = get_db_manager()
    with db.session_scope() as session:
        for interaction_id, (agent_id, cost) in zip(ids, rows):
            session.add(
                LLMInteractionLog(
                    interaction_id=interaction_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    model="test-model",
                    input_tokens=10,
                    output_tokens=4,
                    cost_usd=cost,
                )
            )
    return ids


@pytest.fixture
def db():
    ids: list[str] = []
    yield ids
    if not ids:
        return
    with get_db_manager().session_scope() as session:
        session.query(LLMInteractionLog).filter(
            LLMInteractionLog.interaction_id.in_(ids)
        ).delete(synchronize_session=False)


@pytest.mark.asyncio
async def test_mixed_session_sums_priced_rows_only(db):
    session_id = f"sess-mixed-{uuid.uuid4().hex[:8]}"
    db.extend(
        _insert(session_id, [("triage", 0.42), ("free", 0.0), ("hunter", None)])
    )

    out = await get_session_summary(session_id)

    assert out["total_cost_usd"] == pytest.approx(0.42)
    assert out["unpriced_calls"] == 1
    assert out["total_interactions"] == 3
    assert out["agents"]["triage"]["cost_usd"] == pytest.approx(0.42)
    assert out["agents"]["triage"]["unpriced_calls"] == 0
    assert out["agents"]["free"]["cost_usd"] == 0.0
    assert out["agents"]["free"]["unpriced_calls"] == 0
    assert out["agents"]["hunter"]["cost_usd"] is None
    assert out["agents"]["hunter"]["unpriced_calls"] == 1


@pytest.mark.asyncio
async def test_all_unpriced_session_cost_is_null(db):
    session_id = f"sess-none-{uuid.uuid4().hex[:8]}"
    db.extend(_insert(session_id, [("triage", None)]))

    out = await get_session_summary(session_id)

    assert out["total_cost_usd"] is None
    assert out["unpriced_calls"] == 1
    assert out["agents"]["triage"]["cost_usd"] is None
    assert out["agents"]["triage"]["unpriced_calls"] == 1


@pytest.mark.asyncio
async def test_empty_session_stays_zero(db):
    out = await get_session_summary(f"sess-empty-{uuid.uuid4().hex[:8]}")

    assert out["total_interactions"] == 0
    assert out["total_cost_usd"] == 0.0
    assert out["agents"] == {}
    assert "unpriced_calls" not in out
