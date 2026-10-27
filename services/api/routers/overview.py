"""Overview read for the console: arrivals, outcomes, agents, and the feed."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import provide_workflows
from core.findings.overview import overview_alert, overview_payload
from core.findings.overview_drill import agent_detail, outcome_alerts
from core.routing import Auth, RouterMeta
from core.workflows.workflows_service import WorkflowsService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["overview"],
    auth=Auth.REQUIRED,
)


@router.get("/overview")
async def get_overview():
    """Arrivals, outcome nodes, agent rows, and the latest findings. Polled."""
    return overview_payload()


@router.get("/overview/alerts/{finding_id}")
async def get_overview_alert(finding_id: str):
    """One alert in the feed item's shape, including noise-marked and older ones."""
    item = overview_alert(finding_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Alert {finding_id} not found.")
    return item


# Plain defs: both reads are synchronous, so they run in the threadpool.
@router.get("/overview/outcomes/{state}")
def get_overview_outcome(
    state: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Today's alerts behind one outcome node, and the cases they sit in."""
    body = outcome_alerts(state, limit=limit, offset=offset)
    if body is None:
        raise HTTPException(status_code=404, detail=f"Unknown outcome: {state}")
    return body


@router.get("/overview/agents/{workflow_id}")
def get_overview_agent(
    workflow_id: str, service: WorkflowsService = Depends(provide_workflows)
):
    """One agent card opened: its workflow, live and recent runs, 30-day record."""
    body = agent_detail(workflow_id, service)
    if body is None:
        raise HTTPException(
            status_code=404, detail=f"Workflow not found: {workflow_id}"
        )
    return body
