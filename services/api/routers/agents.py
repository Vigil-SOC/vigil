"""Agents API endpoints for SOC agent management."""

from fastapi import APIRouter, HTTPException

from core.agents.manager import CUSTOM_AGENT_ID_PREFIX, AgentManager
from core.routing import Auth, RouterMeta

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/agents",
    tags=["agents"],
    auth=Auth.REQUIRED,
)

# Global agent manager instance
agent_manager = AgentManager()


def _resolve_agent(agent_id: str):
    """Resolve an agent_id to an AgentProfile, lazy-loading custom agents on miss.

    Built-in agents are served from the in-memory dict with zero DB calls.
    Only misses for IDs prefixed with custom- trigger a refresh and retry.
    """
    agent = agent_manager.agents.get(agent_id)
    if agent is not None:
        return agent
    if agent_id and agent_id.startswith(CUSTOM_AGENT_ID_PREFIX):
        agent_manager.refresh_custom_agents()
        return agent_manager.agents.get(agent_id)
    return None


@router.get("/agents")
async def list_agents():
    """Get list of all available SOC agents (built-ins + DB-backed customs).

    Always refreshes the custom-agent side of the cache from the DB so
    callers see rows created by other worker processes or external
    tooling without having to restart. Built-ins are code-defined and
    cached in-process.
    """
    # Cheap best-effort refresh. Failures leave the existing cache in
    # place — you'd still get the built-in list back.
    agent_manager.refresh_custom_agents()
    return {"agents": agent_manager.get_agent_list()}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    """Get details for a specific agent."""
    agent = _resolve_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "icon": agent.icon,
        "color": agent.color,
        "specialization": agent.specialization,
        "recommended_tools": agent.recommended_tools,
        "max_tokens": agent.max_tokens,
        "enable_thinking": agent.enable_thinking,
    }
