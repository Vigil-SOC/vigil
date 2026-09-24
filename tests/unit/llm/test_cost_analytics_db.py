"""``/analytics/cost`` over stored-NULL (unpriced) rows (#1115).

A priced row, a genuinely free row and an unpriced (NULL) row: the dollar
figure is the priced sum, and each aggregate says how many calls it could
not price.
"""

from __future__ import annotations

import uuid

import pytest

from core.reporting.analytics_service import get_cost_breakdown

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

INV = f"inv-unpriced-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def seeded():
    from core.storage.connection import get_db_manager
    from core.storage.models import LLMInteractionLog

    rows = [
        ("claude-sonnet-4-5-20250929", 1.25),
        ("llama3", 0.0),
        ("mystery-model", None),
    ]
    ids = [str(uuid.uuid4()) for _ in rows]
    db = get_db_manager()
    with db.session_scope() as session:
        session.query(LLMInteractionLog).delete()
        for interaction_id, (model, cost) in zip(ids, rows):
            session.add(
                LLMInteractionLog(
                    interaction_id=interaction_id,
                    agent_id="triage",
                    investigation_id=INV,
                    model=model,
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=cost,
                )
            )
    yield
    with db.session_scope() as session:
        session.query(LLMInteractionLog).filter(
            LLMInteractionLog.interaction_id.in_(ids)
        ).delete(synchronize_session=False)


@pytest.mark.asyncio
async def test_unpriced_rows_are_counted_not_summed(seeded):
    from core.storage.connection import get_db_manager

    with get_db_manager().session_scope() as session:
        out = await get_cost_breakdown(session, "24h")

    assert out["totals"]["calls"] == 3
    assert out["totals"]["cost_usd"] == pytest.approx(1.25)
    assert out["totals"]["unpriced_calls"] == 1

    by_model = {m["model"]: m for m in out["by_model"]}
    assert by_model["mystery-model"]["unpriced_calls"] == 1
    assert by_model["llama3"]["unpriced_calls"] == 0
    assert by_model["llama3"]["cost_usd"] == 0.0
    assert by_model["claude-sonnet-4-5-20250929"]["unpriced_calls"] == 0

    (agent,) = out["by_agent"]
    assert (agent["cost_usd"], agent["unpriced_calls"]) == (pytest.approx(1.25), 1)
    (inv,) = out["top_investigations"]
    assert (inv["investigation_id"], inv["unpriced_calls"]) == (INV, 1)
