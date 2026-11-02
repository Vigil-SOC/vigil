"""Overview read for the console: arrivals, outcomes, agents, and the feed."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth.current_user import get_current_user
from core.auth.permissions import missing_permission, permission_gate
from core.findings.overview import overview_alert, overview_payload
from core.routing import Auth, RouterMeta
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["overview"],
    auth=Auth.REQUIRED,
)


@router.get("/overview")
async def get_overview(
    range: Literal["today", "all"] = Query("today"),
    user: User = Depends(get_current_user),
):
    """Arrivals, outcome nodes, agent rows, and the latest findings. Polled.

    ``range`` bounds arrivals and outcomes to the UTC day, or not at all.

    A missing read leaves that part empty and names the permission. The keys
    stay arrays: the console calls ``feed.find``.
    """
    payload = overview_payload(range=range)
    missing = []
    if missing_permission(user.user_id, "findings.read"):
        payload["arrivals"] = []
        payload["outcomes"] = []
        payload["feed"] = []
        missing.append("findings.read")
    if missing_permission(user.user_id, "automation.read"):
        payload["agents"] = []
        missing.append("automation.read")
    if missing:
        payload["missing"] = missing
    return payload


@router.get(
    "/overview/alerts/{finding_id}",
    dependencies=[permission_gate("findings.read")],
)
async def get_overview_alert(finding_id: str):
    """One alert in the feed item's shape, including noise-marked and older ones."""
    item = overview_alert(finding_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Alert {finding_id} not found.")
    return item
