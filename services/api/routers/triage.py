"""Read-only Triage queue. Nothing on this router writes."""

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from core.routing import Auth, RouterMeta
from services.api.triage_read import triage_payload

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["triage"],
    auth=Auth.REQUIRED,
)


# Plain def: the read is synchronous, so FastAPI runs it in its threadpool
# instead of on the event loop.
@router.get("/triage")
def get_triage(
    kind: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
):
    """Intake rows, ranking inputs, and the strip. Polled.

    Reads the tables directly. ``_get_orchestrator()`` is a second process
    whose queue is never this one.
    """
    # Encoded here, in the threadpool: a returned dict goes through
    # jsonable_encoder on the event loop, which is slow at 200 rows.
    return JSONResponse(
        triage_payload(kind=kind or None, source=source or None, state=state or None)
    )
