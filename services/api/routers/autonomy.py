"""Set one investigate-kind workflow's own tier and floor (#3624) — console wiring.
Also the rule-rollout limits: ``/rollout-limits``.

The ceiling and the tool classes are the frozen contract in
``core/api/v1/autonomy_router.py``, served at ``/api/v1/autonomy`` and here at
``/api/autonomy``.
"""

from dataclasses import replace
from types import MappingProxyType
from typing import Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from core.api.v1.autonomy_router import CeilingResponse, _http, _view
from core.auth.current_user import is_machine_request
from core.auth.permissions import deny_unless, permission_gate
from core.deps import provide_workflows
from core.detections.inventory import rollout_limits
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
        if is_machine_request(current_user):
            raise HTTPException(
                status_code=403, detail="A service account or API token cannot raise autonomy"
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


class RolloutLimitsBody(BaseModel):
    """A partial edit of the rollout limits. Omitted fields stay as they are.

    Whole numbers only; an unknown key is a 422. ``reason`` goes on the audit entry.
    """

    model_config = ConfigDict(extra="forbid")

    live_per_day: Optional[StrictInt] = Field(None, ge=0, le=1000)
    shadow_days: Optional[StrictInt] = Field(None, ge=1, le=90)
    max_hits_per_day: Optional[StrictInt] = Field(None, ge=1, le=100000)
    reason: Optional[str] = Field(None, max_length=500)


class RolloutLimitsResponse(BaseModel):
    """The limits in force, the defaults and bounds, and (read only) each SIEM
    integration's Quiet, Noisy and Silent thresholds, which are edited on it."""

    limits: Dict[str, int]
    defaults: Dict[str, int]
    bounds: Dict[str, Dict[str, int]]
    siem_thresholds: Dict[str, Optional[Dict[str, int]]]


def _limits_view() -> RolloutLimitsResponse:
    return RolloutLimitsResponse(
        limits=rollout_limits.dump(rollout_limits.read_stored_limits()),
        defaults=rollout_limits.dump(rollout_limits.DEFAULTS),
        bounds={
            n: {"min": low, "max": high}
            for n, (low, high) in rollout_limits.BOUNDS.items()
        },
        siem_thresholds=rollout_limits.siem_thresholds(),
    )


@router.get("/rollout-limits", dependencies=_READ, response_model=RolloutLimitsResponse)
def get_rollout_limits() -> RolloutLimitsResponse:
    try:
        return _limits_view()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the rollout limits"
        ) from exc


@router.put("/rollout-limits", dependencies=_READ, response_model=RolloutLimitsResponse)
def put_rollout_limits(
    body: RolloutLimitsBody,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_active_user),
) -> RolloutLimitsResponse:
    """Change the promotion cap, the days in shadow or the daily hit ceiling.

    ``ai_autonomy.write``, as raising the ceiling is: these decide how fast a
    rule reaches live. A service account may only tighten (a lower cap, more
    days, a lower hit ceiling). The change and who made it go on the audit trail.
    """
    deny_unless(current_user.user_id, "ai_autonomy.write", session)
    changes = body.model_dump(exclude={"reason"}, exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change")
    try:
        rollout_limits.update_limits(
            user_id=str(current_user.user_id),
            caller_is_service=is_machine_request(current_user),
            changes=changes,
            reason=body.reason,
        )
        return _limits_view()
    except rollout_limits.LimitsRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except rollout_limits.LimitsWriteError as exc:
        raise HTTPException(
            status_code=500, detail="Failed to save the rollout limits"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the rollout limits"
        ) from exc
