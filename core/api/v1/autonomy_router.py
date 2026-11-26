"""Read and write the autonomy ceiling (#2918) — the frozen part (#4018).

The one ceiling API: the stored flags, the implied read/write/propose state,
whether the environment has locked the act flags, and four-eyes. ``/tools``
lists and sets each outside tool's effect class (#3304). Per-workflow tiers
(``PUT /api/autonomy/workflows/{id}``, #3624) stay console wiring in
``services/api/routers/autonomy.py``; ``CeilingResponse.workflows`` is an open
map, not frozen entry by entry.

Inactive accounts are refused by the auth dependency the mount adds to every
route, so the handlers read the caller with ``get_current_user``.
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth.current_user import get_current_user, is_machine_request
from core.auth.permissions import deny_unless, permission_gate
from core.deps import provide_approvals, provide_mcp_registry
from core.integrations.mcp.registry import MCPRegistry, live_mcp_tools
from core.response.approval_service import (
    ActionNotFound,
    ApprovalService,
    NotHeld,
)
from core.response.ceiling import (
    RELEASED_PER_HOUR_MAX,
    CeilingRefusal,
    CeilingWriteError,
    _raises,
    ceiling_read,
    loosens,
    own_tool_names,
    raises_cap,
    read_ceiling,
    resume_ceiling,
    store_tool_class,
    update_ceiling,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import ToolEffectClass, User

router = APIRouter()
logger = logging.getLogger(__name__)

ROUTER_META = RouterMeta(
    prefix="/api/v1/autonomy",
    tags=["autonomy"],
    auth=Auth.REQUIRED,
    legacy_prefixes=("/api/autonomy",),
)

_READ = [permission_gate("ai_autonomy.read")]
_WRITE = [permission_gate("ai_autonomy.write")]
_DECIDE = [permission_gate("ai_decisions.approve")]


class CeilingBody(BaseModel):
    """A partial edit of the stored ceiling. Omitted flags stay as they are.

    ``stopped`` is not a field here. Stop is ``POST /api/orchestrator/kill``
    and resume is ``POST /api/autonomy/resume``.
    """

    act_reversible: Optional[bool] = None
    act_irreversible: Optional[bool] = None
    four_eyes: Optional[bool] = None
    # Declared crown jewels left for a person (#5227). Off is a loosening.
    hold_crown_jewels: Optional[bool] = None
    # Proposals the ceiling releases per hour (#5274). A higher number is a raise.
    released_per_hour: Optional[int] = Field(
        default=None, ge=0, le=RELEASED_PER_HOUR_MAX, strict=True
    )


class HeldAction(BaseModel):
    """One approved outside action a stop is still holding."""

    action_id: str
    action_type: str = ""
    title: str = ""
    target: str = ""
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    restricted: bool = False


class CeilingResponse(BaseModel):
    act_reversible: bool
    act_irreversible: bool
    stopped: bool
    read: bool
    write: bool
    propose: bool
    environment_locked: bool
    four_eyes: bool
    # Latest stop. Null when autonomy is not stopped.
    stopped_by: Optional[str] = None
    stopped_at: Optional[str] = None
    stopped_by_label: Optional[str] = None
    # Workflow id to its own entry: tier, floor, evidence, by, at (#3624),
    # plus by_label, the principal's name read now (#3642).
    workflows: Dict[str, Dict[str, Any]] = {}
    # Approved outside actions a stop is holding, including after resume.
    held: List[HeldAction] = []
    # On holds a model's act on a declared crown jewel for a person (#5227).
    hold_crown_jewels: bool
    # Crown jewels in the applied site revision; 0 when none is declared.
    crown_jewels: int
    # Proposals the ceiling releases in any 60 minutes; 0 releases none (#5274).
    released_per_hour: int


def _view() -> CeilingResponse:
    return CeilingResponse(**ceiling_read())


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


@router.put("", dependencies=_READ, response_model=CeilingResponse)
def put_ceiling(
    body: CeilingBody,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
) -> CeilingResponse:
    """Edit the ceiling. The permission depends on the direction.

    Only lowering act flags or the hourly cap, or turning the crown-jewel hold
    on, is ``ai_autonomy.stop``. Raising a flag or the cap, changing four-eyes,
    or turning the hold off is ``ai_autonomy.write``.
    """
    try:
        current = read_ceiling()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the autonomy ceiling"
        ) from exc
    lowers = (
        not _raises(current.act_reversible, body.act_reversible)
        and not _raises(current.act_irreversible, body.act_irreversible)
        and body.four_eyes in (None, current.four_eyes)
        and body.hold_crown_jewels in (None, True, current.hold_crown_jewels)
        and not raises_cap(current.released_per_hour, body.released_per_hour)
    )
    perm = "ai_autonomy.stop" if lowers else "ai_autonomy.write"
    deny_unless(current_user.user_id, perm, session)
    edit = body.model_dump()
    if lowers:
        # A restated "on" is dropped, so a flag switched off since the read
        # above is not turned back on under ai_autonomy.stop.
        # The hold is the other way round: on is the lower, a restated off drops.
        # The cap passes as it is; update_ceiling drops it if a lower since
        # the read above makes it a raise.
        edit = {
            k: (
                (None if v is False else v)
                if k == "hold_crown_jewels"
                else v if k == "released_per_hour" else (None if v is True else v)
            )
            for k, v in edit.items()
        }
    try:
        update_ceiling(
            user_id=str(current_user.user_id),
            caller_is_service=is_machine_request(current_user),
            lowers_only=lowers,
            **edit,
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
    current_user: User = Depends(get_current_user),
) -> CeilingResponse:
    """Restore the act flags minus anything lowered during the stop.

    Executes nothing.
    """
    try:
        resume_ceiling(
            user_id=str(current_user.user_id),
            caller_is_service=is_machine_request(current_user),
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


def _principal(user: User) -> Optional[str]:
    return getattr(user, "principal_id", None) or getattr(user, "user_id", None)


# A released row runs at once, as the person who released it (#5059).
async def _run_released(actions, user: User, registry: MCPRegistry) -> None:
    from core.auth.tool_principal import Claims
    from core.response import containment

    principal = _principal(user)
    claims = Claims(
        username=user.username,
        person_id=principal and str(principal),
        agent_id=None,
        run_id=None,
        origin="person",
    )
    registry = await containment.live_registry(registry)
    for action in actions:
        try:
            await containment.carry_out(action, claims, registry)
        except Exception:  # noqa: BLE001 — the rest still run
            logger.exception("released action %s not run", action.action_id)


@router.post("/held/release", dependencies=_DECIDE, response_model=CeilingResponse)
async def post_release_held(
    service: ApprovalService = Depends(provide_approvals),
    current_user: User = Depends(get_current_user),
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> CeilingResponse:
    """Clear the hold on every held action this person may decide, and run them.

    Each released row writes one trail entry.
    """
    try:
        released = service.release_held(current_user.username, _principal(current_user))
        await _run_released(released, current_user, registry)
        return _view()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to release the held actions"
        ) from exc


@router.post(
    "/held/{action_id}/release",
    dependencies=_DECIDE,
    response_model=CeilingResponse,
)
async def post_release_action(
    action_id: str,
    service: ApprovalService = Depends(provide_approvals),
    current_user: User = Depends(get_current_user),
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> CeilingResponse:
    """Clear the hold on one action and run it, as the person releasing it."""
    try:
        updated = service.release_action(
            action_id, current_user.username, _principal(current_user)
        )
    except NotHeld as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ActionNotFound:
        raise HTTPException(
            status_code=404, detail=f"Approval not found: {action_id}"
        ) from None
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")
    await _run_released([updated], current_user, registry)
    try:
        return _view()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not read the autonomy ceiling"
        ) from exc


EffectClass = Literal["read", "read_unverified", "reversible", "irreversible"]


class ToolClassBody(BaseModel):
    effect_class: EffectClass


class ToolClassEntry(BaseModel):
    """One outside tool. ``effect_class`` is None while it is unclassified."""

    name: str
    effect_class: Optional[EffectClass] = None
    classified_by: Optional[str] = None
    classified_at: Optional[str] = None


def _entry(row: ToolEffectClass) -> ToolClassEntry:
    return ToolClassEntry(
        name=row.tool_name,
        effect_class=row.effect_class,
        classified_by=row.classified_by,
        classified_at=row.classified_at.isoformat() if row.classified_at else None,
    )


def _exposed(registry: MCPRegistry) -> set[str]:
    """Outside tool names the connected servers offer now."""
    own = own_tool_names()
    return {t["name"] for t in live_mcp_tools(registry) if t["name"] not in own}


@router.get("/tools", dependencies=_READ, response_model=List[ToolClassEntry])
def list_tool_classes(
    session: UnitOfWorkSession,
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> List[ToolClassEntry]:
    """Every stored class, and each unclassified name a connected server exposes."""
    rows = {row.tool_name: _entry(row) for row in session.query(ToolEffectClass).all()}
    for name in _exposed(registry) - rows.keys():
        rows[name] = ToolClassEntry(name=name)
    return [rows[name] for name in sorted(rows)]


@router.put("/tools/{name}", dependencies=_READ, response_model=ToolClassEntry)
def put_tool_class(
    name: str,
    body: ToolClassBody,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
    registry: MCPRegistry = Depends(provide_mcp_registry),
) -> ToolClassEntry:
    """Set one tool's class. The permission depends on the direction.

    Loosening, or classifying an unclassified name, is a person's
    ``ai_autonomy.write`` and is refused while stopped. Tightening is
    ``ai_autonomy.stop``, which a service account may hold.
    """
    own = name in own_tool_names()
    row = None if own else session.get(ToolEffectClass, name)
    if row is None and (own or name not in _exposed(registry)):
        raise HTTPException(status_code=404, detail="No outside tool by that name")
    before = row.effect_class if row is not None else None
    after = body.effect_class
    if after == before:
        return _entry(row)
    if loosens(before, after):
        deny_unless(current_user.user_id, "ai_autonomy.write", session)
        if is_machine_request(current_user):
            raise HTTPException(
                status_code=403,
                detail="A service account or API token cannot loosen a tool's class",
            )
        try:
            stopped = read_ceiling().stopped
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Could not read the autonomy ceiling"
            ) from exc
        if stopped:
            raise HTTPException(status_code=409, detail="Stopped: resume first")
    else:
        deny_unless(current_user.user_id, "ai_autonomy.stop", session)
    store_tool_class(session, name, before, after, current_user.principal_id)
    return _entry(session.get(ToolEffectClass, name))
