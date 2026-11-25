"""The Supervisory model page: its read, and a person dismissing a suggestion."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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
    reason: Literal[store.DISMISS_REASONS]  # type: ignore[valid-type]


class HistoryEntryResponse(BaseModel):
    id: str
    at: str
    key: str
    # "from" is a Python keyword.
    from_: str = Field(alias="from")
    to: str
    outcome: Literal["applied", "dismissed", "undone"]
    note: str
    improved: bool | None = None
    undoable: bool = False

    model_config = ConfigDict(populate_by_name=True)


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


@router.post(
    "/supervisory/suggestions/{suggestion_id}/dismiss",
    dependencies=_WRITE,
    response_model=HistoryEntryResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
)
def dismiss_suggestion(
    suggestion_id: str,
    body: DismissBody,
    current_user: Annotated[User, Depends(get_current_user)],
) -> HistoryEntryResponse:
    """Dismiss a suggestion with one of the page's reasons. Returns its history entry.

    The same change is not suggested again: for good when too risky or the
    evidence looks wrong, for 30 days otherwise.
    """
    principal_id = getattr(current_user, "principal_id", None) or getattr(
        current_user, "user_id", None
    )
    try:
        entry = store.dismiss(
            suggestion_id,
            body.reason,
            current_user.username,
            principal_id and str(principal_id),
        )
        return HistoryEntryResponse.model_validate(entry)
    except store.StoreRefusal as refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail) from None
