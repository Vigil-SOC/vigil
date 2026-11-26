"""Set one investigate-kind workflow's own tier and floor (#3624) — console wiring.
Also the rule-rollout limits: ``/rollout-limits``, and the emulators and the
hosts they may run on: ``/emulation``.

The ceiling and the tool classes are the frozen contract in
``core/api/v1/autonomy_router.py``, served at ``/api/v1/autonomy`` and here at
``/api/autonomy``.
"""

from dataclasses import replace
from types import MappingProxyType
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from core.api.v1.autonomy_router import CeilingResponse, _exposed, _http, _view
from core.auth.current_user import is_machine_request
from core.auth.permissions import deny_unless, permission_gate
from core.deps import provide_mcp_registry, provide_workflows
from core.detections.inventory import rollout_limits
from core.integrations.mcp.registry import MCPRegistry
from core.response import emulation
from core.response.ceiling import (
    ACT,
    ASSIST,
    CeilingRefusal,
    CeilingWriteError,
    ModelJudgeError,
    investigation_model,
    own_tool_names,
    preview_model_change,
    raises_workflow,
    read_ceiling,
    set_workflow_tier,
    workflow_entry,
    workflow_evidence,
)
from core.response.config import managed_widening, settle_pending
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import ToolEffectClass, User
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
    while stopped, and stores the investigation model in force and that model's
    30-day agreement on the workflow beside its floor.
    Anything else is ``ai_autonomy.stop``. A workflow a declared INTENT.md
    manages is refused (409) anything looser than its declared entry.
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
                status_code=403,
                detail="A service account or API token cannot raise autonomy",
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
            # Act names the model it is raised on, and stores that model's record
            # (blended, with no model, when the model cannot be resolved).
            model = investigation_model()
            entry = workflow_entry(
                ACT, body.floor, workflow_evidence(workflow_id, model), by, model
            )
        elif body.tier == ASSIST:
            entry = workflow_entry(ASSIST, None, None, by)
        else:
            entry = None
        # A workflow INTENT.md manages may only narrow; its declared value is the reset.
        key = f"workflows.{workflow_id}"
        requested = (
            None
            if body.tier is None
            else {
                "tier": body.tier,
                **({"floor": body.floor} if body.tier == ACT else {}),
            }
        )
        refused = managed_widening(key, requested, ceiling=current)
        if refused:
            raise HTTPException(status_code=409, detail=refused)
        set_workflow_tier(workflow_id, entry, str(current_user.user_id), raises=raises)
        settle_pending({key: requested}, str(current_user.user_id))
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


class ModelChangeOutcome(BaseModel):
    """One Act workflow under the change. ``outcome``: keep, drop, stamped,
    unchanged or unstamped; ``source`` is live or replay on a keep."""

    workflow_id: str
    outcome: str
    from_model: Optional[str] = None
    to_model: Optional[str] = None
    source: Optional[str] = None
    reason: str
    evidence: Optional[Dict[str, object]] = None


class ModelChangePreview(BaseModel):
    component: str
    provider_id: str
    model_id: str
    investigation_model_before: Optional[str] = None
    investigation_model_after: Optional[str] = None
    # Whether the investigation model would move at all.
    moves: bool
    workflows: List[ModelChangeOutcome]


@router.get("/model-change", dependencies=_READ, response_model=ModelChangePreview)
def preview_model_change_route(
    component: str, provider_id: str, model_id: str
) -> ModelChangePreview:
    """Which Act workflows would keep Act and which would drop to Assist if
    ``component`` were set to ``provider_id``/``model_id``. Computed by the
    judgement the save runs; writes nothing. Also the /mcp tool
    ``preview_model_change``."""
    try:
        return ModelChangePreview(
            **preview_model_change(component, provider_id, model_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ModelJudgeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


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


class EmulationBody(BaseModel):
    """The marked tools and the host list, each replacing what is stored.

    An omitted field stays as it is. A host is one hostname: no whitespace and
    no leading ``-``.
    """

    model_config = ConfigDict(extra="forbid")

    tools: Optional[List[str]] = Field(None, max_length=200)
    hosts: Optional[List[str]] = Field(None, max_length=500)


class EmulationResponse(BaseModel):
    """Which outside tools are emulators, the hosts they may run on, and the defaults."""

    tools: List[str]
    hosts: List[str]
    defaults: Dict[str, List[str]]


def _emulation_view(current: emulation.Emulation) -> EmulationResponse:
    return EmulationResponse(
        **emulation.dump(current), defaults=emulation.dump(emulation.DEFAULTS)
    )


@router.get("/emulation", dependencies=_READ, response_model=EmulationResponse)
def get_emulation() -> EmulationResponse:
    try:
        return _emulation_view(emulation.read_stored())
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the emulators"
        ) from exc


@router.put("/emulation", dependencies=_READ, response_model=EmulationResponse)
def put_emulation(
    body: EmulationBody,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_active_user),
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> EmulationResponse:
    """Mark or unmark emulators, and edit the hosts they may run on.

    Adding a mark or a host lets an emulator act where it could not: a
    person's ``ai_autonomy.write``, refused while stopped, as loosening a
    tool's class is. Removing either is ``ai_autonomy.stop``. One trail entry
    names who changed what.
    """
    if body.tools is None and body.hosts is None:
        raise HTTPException(status_code=422, detail="Nothing to change")
    bad = [h for h in body.hosts or [] if not emulation.valid_host(h)]
    if bad:
        raise HTTPException(
            status_code=422,
            detail="A host is one hostname, with no whitespace and no leading '-': "
            + ", ".join(repr(h) for h in bad),
        )
    try:
        current = emulation.read_stored()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the emulators"
        ) from exc
    wanted = emulation.Emulation(
        tools=current.tools if body.tools is None else tuple(body.tools),
        hosts=current.hosts if body.hosts is None else tuple(body.hosts),
    )
    change = emulation.diff(current, wanted)
    # A mark names an outside tool a connected server offers, one already
    # classed, or the Atomic tool, which ships marked.
    own = own_tool_names()
    known = set(emulation.ATOMIC_TOOLS)
    for name in change.tools_added:
        if name in own or not (
            name in known
            or session.get(ToolEffectClass, name) is not None
            or name in _exposed(registry)
        ):
            raise HTTPException(
                status_code=404, detail=f"No outside tool named {name!r}"
            )
    if change.adds:
        deny_unless(current_user.user_id, "ai_autonomy.write", session)
        if is_machine_request(current_user):
            raise HTTPException(
                status_code=403,
                detail="A service account or API token cannot mark an emulator "
                "or list a host",
            )
        try:
            stopped = read_ceiling().stopped
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Could not read the autonomy ceiling"
            ) from exc
        if stopped:
            raise HTTPException(status_code=409, detail="Stopped: resume first")
    if change.removes:
        deny_unless(current_user.user_id, "ai_autonomy.stop", session)

    def allow(locked: emulation.Change) -> None:
        # What was checked above is what is written; an edit in between is redone.
        if (locked.adds and not change.adds) or (locked.removes and not change.removes):
            raise emulation.EmulationRefusal(409, "Changed meanwhile: reload and retry")

    try:
        return _emulation_view(
            emulation.update_emulation(
                actor=current_user.principal_id,
                tools=body.tools,
                hosts=body.hosts,
                allow=allow,
            )
        )
    except emulation.EmulationRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except emulation.EmulationWriteError as exc:
        raise HTTPException(
            status_code=500, detail="Failed to save the emulators"
        ) from exc
