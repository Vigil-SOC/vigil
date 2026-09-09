# How the TypeScript agent layer reads a Playbook. It answers with the two layer
# documents as text, so that side parses them with the readers it already has.

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from core.agents.internal_auth import authorise
from core.deps import provide_mcp_registry, provide_workflows
from core.integrations.mcp.registry import MCPRegistry
from core.llm import target
from core.routing import Auth, RouterMeta
from core.workflows.playbook_resolver import UnknownPlaybook, resolve, resolve_hunt
from core.workflows.workflows_service import WorkflowsService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/internal/playbooks",
    tags=["internal-playbooks"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "A shared secret: the caller is the agent layer, not a session. Reachability\n"
        "is the NetworkPolicy's job since ADR 0014, not a loopback check."
    ),
)
logger = logging.getLogger(__name__)


class ResolvedPlaybook(BaseModel):
    playbook: str
    config: str


def _resolver_for(workflows: WorkflowsService, workflow_id: str):
    from core.workflows.workflows_service import is_hunt_like

    definition = workflows.get_workflow(workflow_id)
    if definition is None:
        raise UnknownPlaybook(f"no such workflow: {workflow_id}")
    # Both hunt and root-cause resolve through resolve_hunt so root-cause inherits
    # HUNT_CAPABILITIES (telemetry_search et al.) and the hypothesis-loop config.
    return resolve_hunt if is_hunt_like(definition.run_kind) else resolve


@router.get("/{workflow_id}", response_model=ResolvedPlaybook)
def get_playbook(
    workflow_id: str,
    authorization: Optional[str] = Header(default=None),
    workflows: WorkflowsService = Depends(provide_workflows),
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> ResolvedPlaybook:
    authorise(authorization, "playbook resolution")

    # Which provider and model this run gets, resolved here rather than left to
    # the config layer's DEFAULT_MODEL floor. That floor is a Claude id, so a
    # deployment whose only provider is Ollama ran every hunt against a model it
    # does not have; and a model with no provider beside it is routed by the
    # gateway to whichever provider claims the bare name first.
    #
    # ``investigation`` is the assignment row this already belongs to — the
    # console labels it "Investigation Agents: Investigator, Threat Hunter,
    # Correlator" — and the registry falls it back to chat_default when unset,
    # so nothing new has to be configured.
    resolved = target.resolve_component("investigation")
    if resolved is None:
        logger.info(
            "%s: no model assignment resolved; the config layer's default stands",
            workflow_id,
        )
    provider, model = resolved or (None, None)

    # The definition says which loop drives it, and the two loops read different
    # sections: a compose run wants phases, a hunt wants beliefs to test.
    try:
        playbook, config = _resolver_for(workflows, workflow_id)(
            workflow_id,
            model=model,
            workflows=workflows,
            registry=registry,
            provider=provider,
        )
    except UnknownPlaybook as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    return ResolvedPlaybook(playbook=playbook, config=config)
