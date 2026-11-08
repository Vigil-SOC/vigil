"""The Agent Builder's helpers beside /api/v1/agents (#4024).

An operator agent is read, saved and deleted through /api/v1/agents. Here: the
tool catalogue the drawer's needs picker reads, drafting a manifest from plain
English, and forking a built-in's manifest into one of its own.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.agents.agent_ai_generator import AgentAIGenerator
from core.agents.builtins import builtin_manifests
from core.agents.custom_agent_service import CustomAgentService
from core.auth.permissions import permission_gate
from core.deps import provide_agent_ai, provide_mcp_registry
from core.integrations.mcp.registry import MCPRegistry, refresh_from_client
from core.integrations.mcp.surface import VIGIL_SERVER
from core.llm.tool_schemas import ALL_TOOLS
from core.response.ceiling import asks_first
from core.routing import Auth, RouterMeta
from core.storage.models import User
from core.workflows.playbook_resolver import CAPABILITIES
from services.api.middleware.auth import get_current_active_user

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["custom-agents"],
    auth=Auth.REQUIRED,
)
service = CustomAgentService()

# A fork stores a manifest that runs load. Drafting does not save; both are automation.
_SETTINGS_WRITE = [permission_gate("automation.write")]
router.dependencies.append(permission_gate("automation.read"))


class GenerateAgentRequest(BaseModel):
    """First call: ``description`` only. Refinement: also the ``current_draft``
    manifest and ``feedback``."""

    description: str = Field(..., min_length=1)
    current_draft: Optional[str] = None
    feedback: Optional[str] = None


def _first_sentence(text: Optional[str]) -> str:
    text = " ".join((text or "").split())
    end = text.find(". ")
    return text if end == -1 else text[: end + 1]


@router.get("/agents/custom/_meta/tools")
def list_available_tools(
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> Dict[str, Any]:
    """Every tool an agent may be given, for the agent drawer's tool picker.

    ``catalog`` has one row per tool: its name, the server that serves it
    ("vigil" for Vigil's own) and the first sentence of its description.
    ``changes`` marks each of them read_only or asks_first from its stored
    effect class; a name missing there is not connected. ``tools`` lists the
    connected MCP tool names. ``capabilities`` are the other names a manifest's
    needs may hold, each with the tools that can answer it.

    Sources, in order of preference:
      1. Live MCP registry (populated when MCP servers connect at startup).
      2. Persistent tools cache file at ``data/mcp_tools_cache.json`` written
         on prior successful boots — covers the common dev case where not
         every MCP server happens to be reachable right now.
    """
    rows: Dict[str, Dict[str, str]] = {}
    try:
        # Boot only warms the registry from the on-disk cache, which a new install does
        # not have, so Vigil's own tools and any server connected since are added here.
        refresh_from_client(registry)
        servers = registry.tool_servers()
        for t in registry.get_all_tools():
            server = servers.get(t["name"], "other")
            # The registry prefixes each description with its server.
            description = (t.get("description") or "").removeprefix(f"[{server}] ")
            rows[t["name"]] = {
                "name": t["name"],
                "server": server,
                "description": _first_sentence(description),
            }
    except Exception as e:
        logger.warning(f"Could not load MCP tools from registry: {e}")

    # Fallback: read the persisted tools cache (same file ClaudeService loads).
    if not rows:
        try:
            import json
            from pathlib import Path

            cache_file = (
                Path(__file__).parent.parent.parent / "data" / "mcp_tools_cache.json"
            )
            if cache_file.exists():
                with open(cache_file, "r") as f:
                    tools_dict = json.load(f)
                for server_name, server_tools in (tools_dict or {}).items():
                    for t in server_tools or []:
                        name = t.get("name") if isinstance(t, dict) else None
                        if not name:
                            continue
                        name = f"{server_name}_{name}" if server_name else name
                        rows[name] = {
                            "name": name,
                            "server": server_name or "other",
                            "description": _first_sentence(t.get("description")),
                        }
                if rows:
                    logger.info(
                        f"Loaded {len(rows)} tool names from cache file (registry empty)"
                    )
        except Exception as e:
            logger.warning(f"Could not load MCP tool names from cache: {e}")
    tools = sorted(rows)

    # Vigil's built-in tools are always there, connected or not.
    for t in ALL_TOOLS:
        if t.get("name"):
            rows.setdefault(
                t["name"],
                {
                    "name": t["name"],
                    "server": VIGIL_SERVER,
                    "description": _first_sentence(t.get("description")),
                },
            )

    # What each tool does to the outside world, from its stored effect class.
    # A name missing here is not connected.
    asking = asks_first(rows)
    return {
        "capabilities": [
            {"name": name, "tools": [t for c in candidates for t in c.tools]}
            for name, candidates in CAPABILITIES.items()
        ],
        "tools": tools,
        "changes": {
            n: "asks_first" if n in asking else "read_only" for n in sorted(rows)
        },
        "catalog": sorted(
            rows.values(),
            key=lambda r: (r["server"] != VIGIL_SERVER, r["server"], r["name"]),
        ),
    }


@router.post("/agents/custom/generate", dependencies=_SETTINGS_WRITE)
async def generate_custom_agent(
    payload: GenerateAgentRequest,
    generator: AgentAIGenerator = Depends(provide_agent_ai),
) -> Dict[str, Any]:
    """Draft (or refine) an operator manifest. Does not save.

    ``{"manifest": <yaml>}``, checked as a PUT /api/v1/agents/{id} would be. A
    draft the check refuses is a 422 carrying the reason; it is not returned.
    """
    result = await generator.generate(
        description=payload.description,
        current_draft=payload.current_draft,
        feedback=payload.feedback,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=422 if result.get("refused") else 502,
            detail=result.get("error") or "Agent generation failed",
        )
    return {"manifest": result["manifest"]}


@router.post(
    "/agents/{source_agent_id}/fork", status_code=201, dependencies=_SETTINGS_WRITE
)
def fork_agent(
    source_agent_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Fork a built-in into an operator manifest under ``custom-<id>``.

    The built-in's manifest text, with only its id rewritten: placeholders and
    comments survive. The built-in itself never changes.
    """
    text = builtin_manifests().get(source_agent_id)
    if text is None:
        raise HTTPException(
            status_code=404, detail=f"Built-in agent not found: {source_agent_id}"
        )
    return service.fork_builtin(
        source_agent_id, text, changed_by=str(current_user.user_id)
    )
