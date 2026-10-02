"""Reasoning trace API — exposes persisted LLM chain-of-thought (GH #79)."""

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

from core.routing import Auth, RouterMeta
from core.storage.connection import get_db_manager
from core.storage.models import LLMInteractionLog
from core.storage.schemas import LLMInteractionLogSchema

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/reasoning",
    tags=["reasoning"],
    auth=Auth.REQUIRED,
)


# Columns dump_summary emits, selected so the list never reads the prompt and
# response bodies (#1440). The schema derives has_thinking and has_tools with
# bool() from thinking_content and tool_calls, so those two are computed here
# as the same truthiness test in SQL and handed over under the field names
# (the schema sets populate_by_name), leaving the heavy columns unread.
_EMPTY_JSON = ("[]", "{}", '""', "0", "false", "null")
_SUMMARY_COLUMNS = (
    LLMInteractionLog.id,
    LLMInteractionLog.interaction_id,
    LLMInteractionLog.session_id,
    LLMInteractionLog.agent_id,
    LLMInteractionLog.investigation_id,
    LLMInteractionLog.created_at,
    LLMInteractionLog.model,
    LLMInteractionLog.thinking_enabled,
    and_(
        LLMInteractionLog.thinking_content.is_not(None),
        LLMInteractionLog.thinking_content != "",
    ).label("has_thinking"),
    and_(
        LLMInteractionLog.tool_calls.is_not(None),
        LLMInteractionLog.tool_calls.not_in(
            [cast(literal(v), JSONB) for v in _EMPTY_JSON]
        ),
    ).label("has_tools"),
    LLMInteractionLog.stop_reason,
    LLMInteractionLog.input_tokens,
    LLMInteractionLog.output_tokens,
    LLMInteractionLog.cost_usd,
    LLMInteractionLog.input_cost_per_token,
    LLMInteractionLog.output_cost_per_token,
    LLMInteractionLog.cache_read_cost_per_token,
    LLMInteractionLog.cache_write_cost_per_token,
    LLMInteractionLog.rates_fetched_at,
    LLMInteractionLog.duration_ms,
    LLMInteractionLog.error,
)


@router.get("/{session_id}")
def get_session_summary(session_id: str):
    """Summary rollup for a chat session or agent session.

    Returns total interactions, cumulative cost of priced calls, how many
    calls had no price, token totals, time range, and a per-agent breakdown.
    A session with no rows stays at zero; a session whose rows are all
    unpriced reports a null cost.
    """
    log = LLMInteractionLog
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        # One row per agent_id, in order of each agent's first call, so the
        # agents dict keeps the insertion order the old row loop produced.
        # Only scalar aggregates come back: the prompt and response columns
        # are never read (#1440).
        groups = session.execute(
            select(
                log.agent_id,
                func.count().label("interactions"),
                func.count(log.cost_usd).label("priced"),
                func.sum(log.cost_usd).label("cost"),
                func.coalesce(func.sum(log.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(log.output_tokens), 0).label("output_tokens"),
                func.min(log.created_at).label("first_at"),
                func.max(log.created_at).label("last_at"),
            )
            .where(log.session_id == session_id)
            .group_by(log.agent_id)
            .order_by(func.min(log.created_at).asc(), func.min(log.id).asc())
        ).all()

    if not groups:
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

    # Costs stay Decimal until the end, so each total is the exact Numeric
    # sum rounded once to float. NULL cost_usd is unpriced (#1115): a slice
    # with no priced row stays null rather than reading as free.
    agents: dict = {}
    agent_costs: dict = {}
    total_cost: Optional[Decimal] = None
    total = unpriced = total_in = total_out = 0
    for g in groups:
        # NULL and "" both fold into "unknown", as the per-row loop did.
        key = g.agent_id or "unknown"
        entry = agents.setdefault(
            key,
            {
                "agent_id": g.agent_id,
                "interactions": 0,
                "cost_usd": None,
                "unpriced_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        group_unpriced = int(g.interactions) - int(g.priced)
        entry["interactions"] += int(g.interactions)
        entry["unpriced_calls"] += group_unpriced
        entry["input_tokens"] += int(g.input_tokens)
        entry["output_tokens"] += int(g.output_tokens)
        if g.cost is not None:
            agent_costs[key] = agent_costs.get(key, Decimal(0)) + Decimal(g.cost)
            total_cost = (total_cost or Decimal(0)) + Decimal(g.cost)
        total += int(g.interactions)
        unpriced += group_unpriced
        total_in += int(g.input_tokens)
        total_out += int(g.output_tokens)

    for key, cost in agent_costs.items():
        agents[key]["cost_usd"] = float(cost)

    first_at = min(g.first_at for g in groups)
    last_at = max(g.last_at for g in groups)
    return {
        "session_id": session_id,
        "total_interactions": total,
        "total_cost_usd": float(total_cost) if total_cost is not None else None,
        "unpriced_calls": unpriced,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "first_at": first_at.isoformat() if first_at else None,
        "last_at": last_at.isoformat() if last_at else None,
        "agents": agents,
    }


@router.get("/{session_id}/interactions")
def list_interactions(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated list of interactions in a session. Excludes heavy text fields."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        stmt = (
            select(*_SUMMARY_COLUMNS)
            .where(LLMInteractionLog.session_id == session_id)
            .order_by(LLMInteractionLog.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        rows = session.execute(stmt).all()
        total = (
            session.execute(
                select(func.count(LLMInteractionLog.id)).where(
                    LLMInteractionLog.session_id == session_id
                )
            ).scalar()
            or 0
        )

        return {
            "session_id": session_id,
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "interactions": [LLMInteractionLogSchema.dump_summary(r) for r in rows],
        }


@router.get("/{session_id}/interactions/{interaction_id}")
def get_interaction(session_id: str, interaction_id: str):
    """Full detail for a single interaction (thinking, tools, messages)."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        row = (
            session.execute(
                select(LLMInteractionLog)
                .where(LLMInteractionLog.interaction_id == interaction_id)
                .where(LLMInteractionLog.session_id == session_id)
            )
            .scalars()
            .first()
        )

        if row is None:
            raise HTTPException(status_code=404, detail="Interaction not found")
        return LLMInteractionLogSchema.dump(row)


@router.get("/investigation/{investigation_id}/interactions")
def list_investigation_interactions(
    investigation_id: str,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """List interactions for an investigation (orchestrator detail view)."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        stmt = (
            select(LLMInteractionLog)
            .where(LLMInteractionLog.investigation_id == investigation_id)
            .order_by(LLMInteractionLog.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        rows = session.execute(stmt).scalars().all()
        total = (
            session.execute(
                select(func.count(LLMInteractionLog.id)).where(
                    LLMInteractionLog.investigation_id == investigation_id
                )
            ).scalar()
            or 0
        )

        return {
            "investigation_id": investigation_id,
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "interactions": LLMInteractionLogSchema.dump_many(rows),
        }
