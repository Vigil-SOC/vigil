"""Overview read for the console: arrivals, outcomes, agents, and the feed."""

from fastapi import APIRouter, HTTPException

from core.findings.overview import overview_alert, overview_payload
from core.routing import Auth, RouterMeta

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
