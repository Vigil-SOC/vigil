"""Read and write the autonomy ceiling (#2918).

The compat Assist/Act endpoints stay on ``/api/config`` until the limits
screen moves. This is the API that screen will use: the stored flags, the
implied read/write/propose state, whether the environment has locked the act
flags, and four-eyes. No unverified-tool list.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth.auth_service import _is_service_account
from core.auth.permissions import permission_gate
from core.response.ceiling import (
    CeilingRefusal,
    CeilingWriteError,
    ceiling_view,
    read_ceiling,
    resume_ceiling,
    update_ceiling,
)
from core.routing import Auth, RouterMeta
from core.storage.models import User
from services.api.middleware.auth import get_current_active_user

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/autonomy",
    tags=["autonomy"],
    auth=Auth.REQUIRED,
)

_READ = [permission_gate("ai_autonomy.read")]
_WRITE = [permission_gate("ai_autonomy.write")]


class CeilingBody(BaseModel):
    """A partial edit of the stored ceiling. Omitted flags stay as they are.

    ``stopped`` is not a field here. Stop is ``POST /api/orchestrator/kill``
    and resume is ``POST /api/autonomy/resume``.
    """

    act_reversible: Optional[bool] = None
    act_irreversible: Optional[bool] = None
    four_eyes: Optional[bool] = None


class CeilingResponse(BaseModel):
    act_reversible: bool
    act_irreversible: bool
    stopped: bool
    read: bool
    write: bool
    propose: bool
    environment_locked: bool
    four_eyes: bool


def _view() -> CeilingResponse:
    view = ceiling_view(read_ceiling())
    return CeilingResponse(
        act_reversible=view.act_reversible,
        act_irreversible=view.act_irreversible,
        stopped=view.stopped,
        read=view.read,
        write=view.write,
        propose=view.propose,
        environment_locked=view.environment_locked,
        four_eyes=view.four_eyes,
    )


def _http(exc: CeilingRefusal) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("", dependencies=_READ, response_model=CeilingResponse)
def get_ceiling() -> CeilingResponse:
    try:
        return _view()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the autonomy ceiling"
        ) from exc


@router.put("", dependencies=_WRITE, response_model=CeilingResponse)
def put_ceiling(
    body: CeilingBody,
    current_user: User = Depends(get_current_active_user),
) -> CeilingResponse:
    try:
        update_ceiling(
            user_id=str(current_user.user_id),
            service_account=_is_service_account(current_user),
            act_reversible=body.act_reversible,
            act_irreversible=body.act_irreversible,
            four_eyes=body.four_eyes,
        )
        return _view()
    except CeilingRefusal as exc:
        raise _http(exc) from exc
    except CeilingWriteError as exc:
        raise HTTPException(
            status_code=500, detail="Failed to save the autonomy ceiling"
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the autonomy ceiling"
        ) from exc


@router.post("/resume", dependencies=_WRITE, response_model=CeilingResponse)
def post_resume(
    current_user: User = Depends(get_current_active_user),
) -> CeilingResponse:
    """Restore the act flags minus anything lowered during the stop.

    Executes nothing.
    """
    try:
        resume_ceiling(
            user_id=str(current_user.user_id),
            service_account=_is_service_account(current_user),
        )
        return _view()
    except CeilingRefusal as exc:
        raise _http(exc) from exc
    except CeilingWriteError as exc:
        raise HTTPException(
            status_code=500, detail="Failed to save the autonomy ceiling"
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the autonomy ceiling"
        ) from exc
