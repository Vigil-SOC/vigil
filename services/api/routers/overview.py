"""Overview read for the console: arrivals, outcomes, agents, and the feed."""

from fastapi import APIRouter

from core.findings.overview import overview_payload
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
