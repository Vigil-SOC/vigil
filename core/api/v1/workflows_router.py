"""Workflows — versioned contract surface (``/api/v1/workflows``).

The catalog: list the available playbooks and read one. That is the whole
external contract for workflows — "what workflows exist and what does this one
do". Everything else on the unversioned router in
``core/workflows/workflows_router.py`` is console/authoring/control:

* custom-workflow CRUD and AI generation (authoring UI),
* ``reload`` (a dev button),
* ``execute`` / ``resume`` / ``cancel`` / ``narrate`` / ``delete`` and the
  workflow-run reads — these operate on *workflow runs*, which are the console
  history view of an execution. The frozen **run** contract is the agent run
  (``/api/agent-runs``), the engine record; a hunt is both an agent run and a
  workflow run joined by ``run_id``, so freezing the workflow-run surface too
  would promise two overlapping shapes for one hunt.

The reads themselves are ``core.workflows.catalog``; this module is the frozen
HTTP shape over them, and the console router is a second, unversioned shape over
the same functions.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.deps import provide_workflows
from core.routing import Auth, RouterMeta
from core.workflows import catalog
from core.workflows.workflows_service import WorkflowsService


class WorkflowListResponse(BaseModel):
    workflows: List[Dict[str, Any]] = Field(default_factory=list)
    count: int


# ``WorkflowDefinition.to_dict(include_body=True)``: the same keys for every kind.
class WorkflowDetailResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    agents: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    # Nullable on a custom workflow row.
    use_case: Optional[str] = None
    trigger_examples: List[str] = Field(default_factory=list)
    source: str
    run_kind: str
    hunt_like: bool
    body: str
    # Structured phases, carried by custom workflows for the builder UI.
    phases: Optional[List[Dict[str, Any]]] = None


router = APIRouter()

ROUTER_META = RouterMeta(
    # Additive, NOT legacy-mounted. The catalog read is GET /{workflow_id}, and
    # the console router keeps sibling literals under /api/workflows (notably
    # GET /workflows/custom, which looks like a {workflow_id}). A parameterised
    # route and a literal are only order-safe inside ONE router; mounting this
    # router's /{workflow_id} at /api/workflows as well would put them in two
    # routers and make first-match depend on mount order — exactly what
    # test_no_cross_router_path_shadowing forbids. So the frozen catalog lives
    # only at /api/v1/workflows here, and the console's own GET /workflows and
    # GET /workflows/{id} read the same catalog functions, staying in-router
    # with /workflows/custom.
    prefix="/api/v1/workflows",
    tags=["workflows"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(service: WorkflowsService = Depends(provide_workflows)):
    """
    List all available workflows (file-based + database-backed custom).

    Returns:
        { workflows: [...], count: int }
    """
    return catalog.listing(service)


# exclude_unset keeps ``phases`` absent, not null, for a definition without them.
@router.get(
    "/{workflow_id}",
    response_model=WorkflowDetailResponse,
    response_model_exclude_unset=True,
)
async def get_workflow(
    workflow_id: str,
    service: WorkflowsService = Depends(provide_workflows),
):
    """
    Get full details for a specific workflow (custom or file-based).
    """
    workflow = catalog.detail(service, workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow not found: {workflow_id}",
        )
    return workflow
