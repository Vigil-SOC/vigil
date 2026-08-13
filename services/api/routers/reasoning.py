"""Reasoning trace API — exposes persisted LLM chain-of-thought (GH #79)."""

import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func

from core.storage.connection import get_db_manager
from core.storage.models import LLMInteractionLog
from core.storage.schemas import LLMInteractionLogSchema
from core.routing import Auth, RouterMeta

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/reasoning",
    tags=["reasoning"],
    auth=Auth.REQUIRED,
)


@router.get("/{session_id}")
async def get_session_summary(session_id: str):
    """Summary rollup for a chat session or agent session.

    Returns total interactions, cumulative cost, token totals, time range,
    and per-agent breakdown so UIs can render a session-level header.
    """
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
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
        total_cost = 0.0
        total_in = 0
        total_out = 0
        for r in rows:
            total_cost += float(r.cost_usd or 0)
            total_in += int(r.input_tokens or 0)
            total_out += int(r.output_tokens or 0)
            key = r.agent_id or "unknown"
            entry = agents.setdefault(
                key,
                {
                    "agent_id": r.agent_id,
                    "interactions": 0,
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            entry["interactions"] += 1
            entry["cost_usd"] += float(r.cost_usd or 0)
            entry["input_tokens"] += int(r.input_tokens or 0)
            entry["output_tokens"] += int(r.output_tokens or 0)

        return {
            "session_id": session_id,
            "total_interactions": len(rows),
            "total_cost_usd": total_cost,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "first_at": (
                rows[0].created_at.isoformat() if rows[0].created_at else None
            ),
            "last_at": (
                rows[-1].created_at.isoformat() if rows[-1].created_at else None
            ),
            "agents": agents,
        }


@router.get("/{session_id}/interactions")
async def list_interactions(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated list of interactions in a session. Excludes heavy text fields."""
    db_manager = get_db_manager()
    with db_manager.session_scope() as session:
        stmt = (
            select(LLMInteractionLog)
            .where(LLMInteractionLog.session_id == session_id)
            .order_by(LLMInteractionLog.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        rows = session.execute(stmt).scalars().all()
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
async def get_interaction(session_id: str, interaction_id: str):
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
async def list_investigation_interactions(
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
