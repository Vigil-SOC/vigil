"""Set one investigate-kind workflow's own tier and floor (#3624) — console wiring.

The ceiling and the tool classes are the frozen contract in
``core/api/v1/autonomy_router.py``, served at ``/api/v1/autonomy`` and here at
``/api/autonomy``.
"""

from dataclasses import replace
from types import MappingProxyType
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.api.v1.autonomy_router import CeilingResponse, _http, _view
from core.auth.auth_service import _is_service_account
from core.auth.permissions import deny_unless, permission_gate
from core.deps import provide_workflows
from core.response.ceiling import (
    ACT,
    ASSIST,
    CeilingRefusal,
    CeilingWriteError,
    raises_workflow,
    read_ceiling,
    set_workflow_tier,
    workflow_entry,
    workflow_evidence,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User
from core.workflows.workflows_service import INVESTIGATE_RUN_KIND, WorkflowsService
from services.api.middleware.auth import get_current_active_user

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/autonomy",
    tags=["autonomy"],
    auth=Auth.REQUIRED,
)

_READ = [permission_gate("ai_autonomy.read")]


class WorkflowAutonomyBody(BaseModel):
    """One workflow's tier. No tier returns it to the fleet flags.

    ``floor`` is the whole-percent agreement under which Vigil drops an Act
    workflow to Assist. There is no evidence field: the server computes it.
    """

    tier: Optional[Literal["act", "assist"]] = None
    floor: Optional[int] = None


@router.put(
    "/workflows/{workflow_id}", dependencies=_READ, response_model=CeilingResponse
)
def put_workflow_autonomy(
    workflow_id: str,
    body: WorkflowAutonomyBody,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_active_user),
    workflows: WorkflowsService = Depends(provide_workflows),
) -> CeilingResponse:
    """Set one investigate-kind workflow's tier. The permission depends on the direction.

    Act is always a raise: it holds after the fleet is lowered. Anything else that
    lets the workflow act where it could not, such as clearing Assist under a fleet
    on Act, is a raise too. A raise is a person's ``ai_autonomy.write``, refused
    while stopped, and stores the workflow's 30-day agreement beside its floor.
    Anything else is ``ai_autonomy.stop``.
    """
    workflow = workflows.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="No workflow by that id")
    if workflow.run_kind != INVESTIGATE_RUN_KIND:
        raise HTTPException(
            status_code=422,
            detail="Only an investigate-kind workflow has its own autonomy",
        )
    try:
        current = read_ceiling()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the autonomy ceiling"
        ) from exc
    candidate = dict(current.workflows)
    candidate.pop(workflow_id, None)
    if body.tier is not None:
        candidate[workflow_id] = {"tier": body.tier}
    after = replace(current, workflows=MappingProxyType(candidate))
    raises = body.tier == ACT or raises_workflow(current, workflow_id, after)
    by = current_user.principal_id
    if raises:
        deny_unless(current_user.user_id, "ai_autonomy.write", session)
        if _is_service_account(current_user):
            raise HTTPException(
                status_code=403, detail="A service account cannot raise autonomy"
            )
        if current.stopped:
            raise HTTPException(status_code=409, detail="Stopped: resume first")
    else:
        deny_unless(current_user.user_id, "ai_autonomy.stop", session)
    try:
        if body.tier == ACT:
            if body.floor is None or not 1 <= body.floor <= 100:
                raise HTTPException(
                    status_code=422, detail="A raise needs a floor from 1 to 100"
                )
            entry = workflow_entry(ACT, body.floor, workflow_evidence(workflow_id), by)
        elif body.tier == ASSIST:
            entry = workflow_entry(ASSIST, None, None, by)
        else:
            entry = None
        set_workflow_tier(workflow_id, entry, str(current_user.user_id), raises=raises)
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
