"""The Supervisory model page: its read, and a person dismissing a suggestion."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.routing import Auth, RouterMeta
from core.storage.models import User
from core.supervisory import store
from core.supervisory.feedback import feedback
from core.supervisory.settings import settings

router = APIRouter()
router.dependencies.append(permission_gate("ai_autonomy.read"))

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["supervisory"],
    auth=Auth.REQUIRED,
)

_WRITE = [permission_gate("ai_autonomy.write")]


class DismissBody(BaseModel):
    reason: str


# Plain def: the read is synchronous, so FastAPI runs it in its threadpool.
@router.get("/supervisory")
def get_supervisory():
    """What the supervisory model has to learn from, its suggestions, and the settings it watches.

    Nothing checks settings on a schedule yet, so the state is ``off``. Failure
    reasons are not measured yet: ``null``, which the page reads differently from
    an empty list.
    """
    read = feedback()
    stored = store.read()
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
            "suggestions": stored["suggestions"],
            "held": stored["held"],
            "failures": None,
            "history": stored["history"],
            "settings": settings(),
            "learns_from": {"days": read["history_days"], "items": read["learns_from"]},
        }
    )


@router.post("/supervisory/suggestions/{suggestion_id}/dismiss", dependencies=_WRITE)
def dismiss_suggestion(
    suggestion_id: str,
    body: DismissBody,
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Dismiss a suggestion with one of the page's reasons. Returns its history entry.

    The same change (setting and target value) is not suggested again.
    """
    principal_id = getattr(current_user, "principal_id", None) or getattr(
        current_user, "user_id", None
    )
    try:
        return store.dismiss(
            suggestion_id,
            body.reason,
            current_user.username,
            principal_id and str(principal_id),
        )
    except store.StoreRefusal as refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail) from None
