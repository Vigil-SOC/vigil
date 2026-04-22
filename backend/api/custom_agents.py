"""Custom SOC Agent CRUD endpoints (Agent Builder).

Built-in agents remain hardcoded in services/soc_agents.py. This module only
manages the DB-backed custom agents, prefixed with "custom-".
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.services.custom_agent_service import (
    CustomAgentAlreadyExists,
    CustomAgentNotFound,
    CustomAgentService,
)
from services.soc_agents import CUSTOM_AGENT_ID_PREFIX

logger = logging.getLogger(__name__)

router = APIRouter()
service = CustomAgentService()


def _refresh_manager() -> None:
    """Refresh the global AgentManager so changes are visible to /agents/* routes."""
    try:
        from backend.api.agents import agent_manager

        agent_manager.refresh_custom_agents()
    except Exception as e:
        logger.warning(f"Failed to refresh AgentManager after custom agent change: {e}")


class CustomAgentCreate(BaseModel):
    """Request body for creating a custom agent."""

    name: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    specialization: Optional[str] = None
    extra_principles: Optional[str] = ""
    methodology: Optional[str] = ""
    system_prompt_override: Optional[str] = None
    recommended_tools: List[str] = Field(default_factory=list)
    max_tokens: int = 4096
    enable_thinking: bool = False
    model: Optional[str] = None


class CustomAgentUpdate(BaseModel):
    """Partial update for an existing custom agent. All fields optional."""

    name: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    specialization: Optional[str] = None
    extra_principles: Optional[str] = None
    methodology: Optional[str] = None
    system_prompt_override: Optional[str] = None
    recommended_tools: Optional[List[str]] = None
    max_tokens: Optional[int] = None
    enable_thinking: Optional[bool] = None
    model: Optional[str] = None


class GenerateAgentRequest(BaseModel):
    """Request body for AI-assisted agent generation (issue #80 Phase 2).

    First call: pass ``description`` only.
    Refinement: also pass ``current_draft`` and ``feedback``.
    """

    description: str = Field(..., min_length=1)
    current_draft: Optional[Dict[str, Any]] = None
    feedback: Optional[str] = None


def _with_effective_prompt(row: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the rendered effective prompt to an agent row dict."""
    row = dict(row)
    row["effective_prompt"] = service.get_effective_prompt(row)
    return row


@router.get("/agents/custom")
async def list_custom_agents() -> Dict[str, Any]:
    try:
        agents = service.list_agents()
        return {"agents": agents}
    except Exception as e:
        logger.error(f"Error listing custom agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/custom/_meta/tools")
async def list_available_tools() -> Dict[str, Any]:
    """Return MCP tool names grouped by server prefix for the UI multiselect."""
    tools: List[str] = []
    try:
        from services.mcp_registry import get_mcp_registry

        registry = get_mcp_registry()
        names = registry.get_tool_names() or []
        tools = sorted(set(names))
    except Exception as e:
        logger.warning(f"Could not load MCP tool names from registry: {e}")

    grouped: Dict[str, List[str]] = {}
    for name in tools:
        # Names follow "{server}_{tool}"; group by prefix before the first underscore
        if "_" in name:
            server, _rest = name.split("_", 1)
        else:
            server = "other"
        grouped.setdefault(server, []).append(name)

    return {
        "tools": tools,
        "grouped": grouped,
    }


@router.post("/agents/custom/generate")
async def generate_custom_agent(payload: GenerateAgentRequest) -> Dict[str, Any]:
    """
    AI-assisted agent generation / refinement (issue #80 Phase 2).

    Does NOT save. Frontend takes the returned draft, lets the user tweak it,
    and POSTs to /agents/custom to create. Pass ``current_draft`` + ``feedback``
    to iteratively refine a prior draft.
    """
    try:
        from services.agent_ai_generator import get_agent_ai_generator

        result = await get_agent_ai_generator().generate(
            description=payload.description,
            current_draft=payload.current_draft,
            feedback=payload.feedback,
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=502,
                detail=result.get("error") or "Agent generation failed",
            )
        return {"draft": result["draft"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error generating custom agent")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/custom/{agent_id}")
async def get_custom_agent(agent_id: str) -> Dict[str, Any]:
    try:
        row = service.get_agent(agent_id)
        if not row:
            raise HTTPException(
                status_code=404, detail=f"Custom agent not found: {agent_id}"
            )
        return _with_effective_prompt(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting custom agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/custom", status_code=201)
async def create_custom_agent(request: CustomAgentCreate) -> Dict[str, Any]:
    try:
        row = service.create_agent(request.model_dump(exclude_unset=False))
        _refresh_manager()
        return _with_effective_prompt(row)
    except CustomAgentAlreadyExists as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating custom agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/agents/custom/{agent_id}")
async def update_custom_agent(
    agent_id: str, request: CustomAgentUpdate
) -> Dict[str, Any]:
    if not agent_id.startswith(CUSTOM_AGENT_ID_PREFIX):
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to update built-in agent: {agent_id}",
        )
    try:
        updates = request.model_dump(exclude_unset=True)
        row = service.update_agent(agent_id, updates)
        _refresh_manager()
        return _with_effective_prompt(row)
    except CustomAgentNotFound:
        raise HTTPException(
            status_code=404, detail=f"Custom agent not found: {agent_id}"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating custom agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agents/custom/{agent_id}", status_code=204)
async def delete_custom_agent(agent_id: str):
    if not agent_id.startswith(CUSTOM_AGENT_ID_PREFIX):
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to delete built-in agent: {agent_id}",
        )
    try:
        deleted = service.delete_agent(agent_id)
        if not deleted:
            raise HTTPException(
                status_code=404, detail=f"Custom agent not found: {agent_id}"
            )
        _refresh_manager()
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting custom agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
