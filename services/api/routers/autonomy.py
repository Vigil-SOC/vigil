"""Read and write the autonomy ceiling (#2918).

The one ceiling API: the stored flags, the implied read/write/propose state,
whether the environment has locked the act flags, and four-eyes. ``/tools``
lists and sets each outside tool's effect class (#3304).
"""

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth.auth_service import _is_service_account
from core.auth.permissions import deny_unless, permission_gate
from core.deps import provide_mcp_registry
from core.integrations.mcp.registry import MCPRegistry, live_mcp_tools
from core.response.ceiling import (
    CeilingRefusal,
    CeilingWriteError,
    _raises,
    ceiling_view,
    loosens,
    own_tool_names,
    read_ceiling,
    resume_ceiling,
    store_tool_class,
    update_ceiling,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import ToolEffectClass, User
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


@router.put("", dependencies=_READ, response_model=CeilingResponse)
def put_ceiling(
    body: CeilingBody,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_active_user),
) -> CeilingResponse:
    """Edit the ceiling. The permission depends on the direction.

    Only lowering act flags is ``ai_autonomy.stop``. Raising one, or changing
    four-eyes, is ``ai_autonomy.write``.
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
    )
    perm = "ai_autonomy.stop" if lowers else "ai_autonomy.write"
    deny_unless(current_user.user_id, perm, session)
    edit = body.model_dump()
    if lowers:
        # A restated "on" is dropped, so a flag switched off since the read
        # above is not turned back on under ai_autonomy.stop.
        edit = {k: (None if v is True else v) for k, v in edit.items()}
    try:
        update_ceiling(
            user_id=str(current_user.user_id),
            caller_is_service=_is_service_account(current_user),
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
    current_user: User = Depends(get_current_active_user),
) -> CeilingResponse:
    """Restore the act flags minus anything lowered during the stop.

    Executes nothing.
    """
    try:
        resume_ceiling(
            user_id=str(current_user.user_id),
            caller_is_service=_is_service_account(current_user),
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
    current_user: User = Depends(get_current_active_user),
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
        if _is_service_account(current_user):
            raise HTTPException(
                status_code=403,
                detail="A service account cannot loosen a tool's class",
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
