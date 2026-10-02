"""Reasoning summary and list read only the columns they return (#1440).

The session summary used to load every row of a session, prompt bodies and
all, to add up five scalars, and the list loaded whole rows and then dropped
the heavy fields. Both now select a narrow shape. These tests pin that the
JSON is unchanged for the same rows, by comparing against the previous
full-row implementations kept below as references, and that the SQL never
names a heavy column.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from core.storage.connection import get_db_manager
from core.storage.models import LLMInteractionLog
from core.storage.schemas import LLMInteractionLogSchema
from services.api.routers.reasoning import get_session_summary, list_interactions

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

_HEAVY = (
    "request_messages",
    "system_prompt",
    "thinking_budget",
    "response_content",
    "tool_results",
    "cache_read_tokens",
    "cache_creation_tokens",
)

_T0 = datetime(2026, 1, 1, 12, 0, 0)


async def _call(fn, *args, **kwargs):
    """Await a handler whether it is async def or plain def."""
    out = fn(*args, **kwargs)
    return await out if inspect.isawaitable(out) else out


def _old_summary(session_id: str) -> dict:
    """The full-row summary this change replaced, kept as the reference."""
    with get_db_manager().session_scope() as session:
        rows = (
            session.execute(
                select(LLMInteractionLog)
                .where(LLMInteractionLog.session_id == session_id)
                .order_by(LLMInteractionLog.created_at.asc())
            )
            .scalars()
            .all()
        )
        if not rows:
            return {
                "session_id": session_id,
                "total_interactions": 0,
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "first_at": None,
                "last_at": None,
                "agents": {},
            }
        agents: dict = {}
        total_cost = None
        unpriced = total_in = total_out = 0
        for r in rows:
            total_in += int(r.input_tokens or 0)
            total_out += int(r.output_tokens or 0)
            entry = agents.setdefault(
                r.agent_id or "unknown",
                {
                    "agent_id": r.agent_id,
                    "interactions": 0,
                    "cost_usd": None,
                    "unpriced_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            entry["interactions"] += 1
            entry["input_tokens"] += int(r.input_tokens or 0)
            entry["output_tokens"] += int(r.output_tokens or 0)
            if r.cost_usd is None:
                unpriced += 1
                entry["unpriced_calls"] += 1
            else:
                amount = float(r.cost_usd)
                total_cost = (total_cost or 0) + amount
                entry["cost_usd"] = (entry["cost_usd"] or 0) + amount
        return {
            "session_id": session_id,
            "total_interactions": len(rows),
            "total_cost_usd": total_cost,
            "unpriced_calls": unpriced,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "first_at": rows[0].created_at.isoformat(),
            "last_at": rows[-1].created_at.isoformat(),
            "agents": agents,
        }


def _old_list(session_id: str, limit: int, offset: int) -> list[dict]:
    """The full-row list serialization this change replaced."""
    with get_db_manager().session_scope() as session:
        rows = (
            session.execute(
                select(LLMInteractionLog)
                .where(LLMInteractionLog.session_id == session_id)
                .order_by(LLMInteractionLog.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return [LLMInteractionLogSchema.dump_summary(r) for r in rows]


# (agent_id, cost_usd, thinking_content, tool_calls). Costs are binary
# fractions so the old float accumulation is exact and the outputs can be
# compared byte for byte; see test_summary_cost_is_the_exact_numeric_sum.
_ROWS = [
    ("triage", 0.25, "reasoned at length", [{"name": "lookup"}]),
    (None, None, None, []),
    ("hunter", 0.0, "", []),
    ("", 0.5, "x", {}),
    ("triage", None, None, [{"name": "a"}, {"name": "b"}]),
    ("unknown", 0.125, "thought", {"k": "v"}),
    ("hunter", 0.375, None, []),
    ("triage", 0.0625, "more", []),
    (None, 0.5, None, [{}]),
]


@pytest.fixture
def seeded():
    session_id = f"sess-shape-{uuid.uuid4().hex[:8]}"
    ids = []
    with get_db_manager().session_scope() as session:
        for i, (agent_id, cost, thinking, tools) in enumerate(_ROWS):
            interaction_id = str(uuid.uuid4())
            ids.append(interaction_id)
            session.add(
                LLMInteractionLog(
                    interaction_id=interaction_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    investigation_id="inv-1" if i % 2 else None,
                    created_at=_T0 + timedelta(seconds=i),
                    model="test-model",
                    request_messages=[{"role": "user", "content": "q" * 50}],
                    system_prompt="sys",
                    thinking_enabled=thinking is not None,
                    thinking_budget=1024,
                    thinking_content=thinking,
                    response_content="answer",
                    tool_calls=tools,
                    tool_results=[{"ok": True}] if tools else [],
                    stop_reason="end_turn",
                    input_tokens=10 + i,
                    output_tokens=3 * i,
                    cache_read_tokens=7,
                    cache_creation_tokens=2,
                    cost_usd=cost,
                    duration_ms=100 * i,
                    error="boom" if i == 4 else None,
                )
            )
    yield session_id
    with get_db_manager().session_scope() as session:
        session.query(LLMInteractionLog).filter(
            LLMInteractionLog.interaction_id.in_(ids)
        ).delete(synchronize_session=False)


@pytest.fixture
def statements():
    """Capture every SQL statement sent while the test runs."""
    engine = get_db_manager().engine
    seen: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    yield seen
    event.remove(engine, "before_cursor_execute", _capture)


def _dumps(value) -> str:
    return json.dumps(value)


@pytest.mark.asyncio
async def test_summary_matches_full_row_reference(seeded):
    new = await _call(get_session_summary, seeded)
    old = _old_summary(seeded)

    assert _dumps(new) == _dumps(old)
    assert list(new["agents"]) == ["triage", "unknown", "hunter"]
    assert new["agents"]["unknown"]["agent_id"] is None
    assert new["agents"]["unknown"]["interactions"] == 4


@pytest.mark.asyncio
async def test_empty_summary_matches_full_row_reference():
    session_id = f"sess-empty-{uuid.uuid4().hex[:8]}"
    new = await _call(get_session_summary, session_id)

    assert _dumps(new) == _dumps(_old_summary(session_id))


@pytest.mark.asyncio
async def test_list_matches_full_row_reference(seeded):
    for limit, offset in ((100, 0), (3, 2), (5, 7)):
        new = await _call(list_interactions, seeded, limit=limit, offset=offset)

        assert new["total"] == len(_ROWS)
        assert _dumps(new["interactions"]) == _dumps(_old_list(seeded, limit, offset))

    out = await _call(list_interactions, seeded, limit=100, offset=0)
    assert [r["has_thinking"] for r in out["interactions"]] == [
        bool(t) for _, _, t, _ in _ROWS
    ]
    assert [r["has_tools"] for r in out["interactions"]] == [
        bool(t) for _, _, _, t in _ROWS
    ]


@pytest.mark.asyncio
async def test_summary_issues_one_aggregate_and_no_heavy_column(seeded, statements):
    await _call(get_session_summary, seeded)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    assert "GROUP BY" in selects[0].upper()
    for column in _HEAVY + ("thinking_content", "tool_calls"):
        assert column not in selects[0]


@pytest.mark.asyncio
async def test_list_selects_no_heavy_column(seeded, statements):
    await _call(list_interactions, seeded, limit=100, offset=0)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert selects
    for statement in selects:
        for column in _HEAVY:
            assert column not in statement


@pytest.mark.asyncio
async def test_summary_cost_is_the_exact_numeric_sum():
    session_id = f"sess-exact-{uuid.uuid4().hex[:8]}"
    ids = [str(uuid.uuid4()) for _ in range(3)]
    with get_db_manager().session_scope() as session:
        for i, interaction_id in enumerate(ids):
            session.add(
                LLMInteractionLog(
                    interaction_id=interaction_id,
                    session_id=session_id,
                    agent_id="triage",
                    created_at=_T0 + timedelta(seconds=i),
                    model="test-model",
                    cost_usd=Decimal("0.1"),
                )
            )
    try:
        out = await _call(get_session_summary, session_id)
    finally:
        with get_db_manager().session_scope() as session:
            session.query(LLMInteractionLog).filter(
                LLMInteractionLog.interaction_id.in_(ids)
            ).delete(synchronize_session=False)

    # The old loop added floats row by row and reported 0.30000000000000004.
    assert out["total_cost_usd"] == 0.3
    assert out["agents"]["triage"]["cost_usd"] == 0.3
