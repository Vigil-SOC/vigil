"""The Supervisory model page's read. Nothing on this router writes."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.auth.permissions import permission_gate
from core.routing import Auth, RouterMeta
from core.supervisory.feedback import feedback

router = APIRouter()
router.dependencies.append(permission_gate("ai_autonomy.read"))

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["supervisory"],
    auth=Auth.REQUIRED,
)


# Plain def: the read is synchronous, so FastAPI runs it in its threadpool.
@router.get("/supervisory")
def get_supervisory():
    """What the supervisory model has to learn from, per function.

    Nothing checks settings on a schedule yet, so the state is ``off`` and there
    are no suggestions, held suggestions or history. Failure reasons and settings
    are not measured yet: ``null``, which the page reads differently from an
    empty list.
    """
    read = feedback()
    return JSONResponse(
        {
            "status": {
                "state": "off",
                "last_check_at": None,
                "next_check_at": None,
                "history_days": read["history_days"],
                "functions_watched": len(read["functions"]),
                "medic_open_related": 0,
            },
            "functions": read["functions"],
            "suggestions": [],
            "held": [],
            "failures": None,
            "history": [],
            "settings": None,
            "learns_from": {"days": read["history_days"], "items": read["learns_from"]},
        }
    )
