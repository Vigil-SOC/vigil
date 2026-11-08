"""Operator agents — versioned contract surface (``/api/v1/agents``, #4023).

An operator agent is a plugin manifest stored on ``custom_agents``. A PUT is
checked by the parser a run loads with (agent-serve's ``POST /plugins/check``)
and, here, for needs this deployment cannot bind; either refusal comes back as a
422 with its reason. ``agent-run-<id>`` then runs it as an investigate run.

Not legacy-mounted: ``/api/agents`` is the console's agent catalogue, with
routes of its own under the same prefix.
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from core.agents.custom_agent_service import CustomAgentService
from core.agents.enablement import set_agent_enabled
from core.agents.operator_manifest import (
    CheckUnavailable,
    ManifestRefused,
    vet_manifest,
)
from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.deps import provide_mcp_registry
from core.integrations.mcp.registry import MCPRegistry
from core.routing import Auth, RouterMeta
from core.storage.models import User

logger = logging.getLogger(__name__)

router = APIRouter()
router.dependencies.append(permission_gate("automation.read"))

ROUTER_META = RouterMeta(
    prefix="/api/v1/agents",
    tags=["agents"],
    auth=Auth.REQUIRED,
)

# A saved manifest's prompt and grants are loaded into runs.
_SETTINGS_WRITE = [permission_gate("automation.write")]

service = CustomAgentService()


class AgentManifestBody(BaseModel):
    # The plugin manifest, as YAML text.
    manifest: str = Field(..., min_length=1)


class AgentManifestResponse(BaseModel):
    id: str
    description: str = ""
    needs: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    # The stored manifest, byte for byte as it was PUT.
    manifest: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AgentManifestListResponse(BaseModel):
    agents: List[AgentManifestResponse] = Field(default_factory=list)
    count: int


def _refused(reason: str) -> HTTPException:
    return HTTPException(status_code=422, detail=reason)


@router.get("", response_model=AgentManifestListResponse)
def list_agents():
    agents = service.list_manifests()
    return {"agents": agents, "count": len(agents)}


@router.get("/{agent_id}", response_model=AgentManifestResponse)
def get_agent(agent_id: str):
    row = service.get_manifest(agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
    return row


@router.put(
    "/{agent_id}",
    response_model=AgentManifestResponse,
    dependencies=_SETTINGS_WRITE,
    responses={201: {"model": AgentManifestResponse}},
)
def put_agent(
    agent_id: str,
    body: AgentManifestBody,
    response: Response,
    registry: MCPRegistry = Depends(provide_mcp_registry),
    current_user: User = Depends(get_current_user),
):
    """Create or replace the operator agent ``agent_id`` with ``manifest``.

    201 on create, 200 on replace. 422 carries why a manifest was refused.
    """
    try:
        checked = vet_manifest(body.manifest, registry)
    except ManifestRefused as exc:
        raise _refused(str(exc)) from exc
    except CheckUnavailable as exc:
        logger.warning("could not check the manifest for %s: %s", agent_id, exc)
        raise HTTPException(
            status_code=503, detail="The agent service could not check the manifest"
        ) from exc
    if checked != agent_id:
        raise _refused(f"the manifest's id is {checked}, not {agent_id}")
    try:
        row, created = service.put_manifest(
            agent_id, body.manifest, changed_by=str(current_user.user_id)
        )
    except IntegrityError as exc:
        # Another PUT created the same id first.
        raise HTTPException(
            status_code=409, detail=f"{agent_id} was created meanwhile; PUT again"
        ) from exc
    response.status_code = 201 if created else 200
    return row


@router.delete("/{agent_id}", status_code=204, dependencies=_SETTINGS_WRITE)
def delete_agent(
    agent_id: str,
    current_user: User = Depends(get_current_user),
):
    if not service.delete_agent(agent_id, changed_by=str(current_user.user_id)):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
    # An id can be PUT again; a stale off entry would switch the new one off.
    if not set_agent_enabled(agent_id, True, str(current_user.user_id)):
        logger.warning("Could not clear disabled state for deleted agent %s", agent_id)
    return Response(status_code=204)
