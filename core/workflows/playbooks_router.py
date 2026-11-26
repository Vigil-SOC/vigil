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
from core.workflows.playbook_resolver import (
    UnknownPlaybook,
    resolve,
    resolve_hunt,
    resolve_root_cause,
)
from core.workflows.workflows_service import (
    INVESTIGATE_RUN_KIND,
    ROOT_CAUSE_RUN_KIND,
    WorkflowsService,
)

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
    # root_cause binds telemetry_search on its own. The hunt resolver would also
    # grant findings_search and indicator_lookup, which this trace does not hold.
    if definition.run_kind == ROOT_CAUSE_RUN_KIND:
        return resolve_root_cause
    return resolve_hunt if is_hunt_like(definition.run_kind) else resolve


def _judge_handoff(
    workflows: WorkflowsService, workflow_id: str, model: Optional[str]
) -> None:
    """Judge an Act investigate workflow against the model it is about to be handed.

    This covers every way the model moves (provider default, environment,
    catalogue fallback). An unstamped entry is stamped, not judged. When it
    cannot be judged the run does not start: the agent retries a 503.
    """
    from core.response.ceiling import ModelJudgeError, judge_model_change

    definition = workflows.get_workflow(workflow_id)
    if definition is None or definition.run_kind != INVESTIGATE_RUN_KIND:
        return
    try:
        judge_model_change(model, stamp=model, only=workflow_id, write=True)
    except ModelJudgeError as exc:
        logger.warning("%s: run refused, model change not judged: %s", workflow_id, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/{workflow_id}", response_model=ResolvedPlaybook)
def get_playbook(
    workflow_id: str,
    authorization: Optional[str] = Header(default=None),
    workflows: WorkflowsService = Depends(provide_workflows),
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> ResolvedPlaybook:
    authorise(authorization, "playbook resolution")

    # Not the config layer's DEFAULT_MODEL floor, which is a Claude id whatever
    # the provider. ``investigation`` is the row the console files hunts under.
    resolved = target.resolve_component("investigation")
    if resolved is None:
        logger.info(
            "%s: no model assignment resolved; the config layer's default stands",
            workflow_id,
        )
    provider, model = resolved or (None, None)
    _judge_handoff(workflows, workflow_id, model)

    # The definition says which loop drives it, and the two loops read different
    # sections: a compose run wants phases, a hunt wants beliefs to test.
    try:
        playbook, config = _resolver_for(workflows, workflow_id)(
            workflow_id,
            model=model,
            workflows=workflows,
            registry=registry,
            provider=provider,
            effort=target.resolve_effort("investigation"),
        )
    except UnknownPlaybook as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    return ResolvedPlaybook(playbook=playbook, config=config)
