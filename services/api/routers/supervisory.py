"""The Supervisory model page: its read, and a person applying, undoing or dismissing a suggestion.

Apply and undo write through the same endpoints Settings uses (the autonomy
ceiling, the orchestrator settings), so every check those make still holds:
the permission for the direction, the environment lock, a stop, INTENT.md
managed keys, and the bounds.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.api.v1.autonomy_router import CeilingBody, put_ceiling
from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.daemon_config import DaemonConfig
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work
from core.supervisory import store
from core.supervisory.feedback import feedback
from core.supervisory.settings import settings
from services.api.routers.config import (
    OrchestratorSettingsConfig,
    set_orchestrator_config,
)

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
    undoable: bool | None = None

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
    watched = settings()
    stored = store.read({row["key"]: row for row in watched})
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
            "settings": watched,
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


@router.post(
    "/supervisory/suggestions/{suggestion_id}/apply",
    dependencies=_WRITE,
    response_model=HistoryEntryResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
)
def apply_suggestion(
    suggestion_id: str,
    session: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> HistoryEntryResponse:
    """Change the setting to the suggested value, when it still has the value the
    suggestion started from. Returns the history entry."""
    with unit_of_work() as locked:
        row = _refusing(store.locked_live, locked, suggestion_id)
        _change(row.setting_key, row.from_value, row.to_value, session, current_user)
        return _record(locked, row, "applied", current_user)


@router.post(
    "/supervisory/history/{history_id}/undo",
    dependencies=_WRITE,
    response_model=HistoryEntryResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
)
def undo_apply(
    history_id: str,
    session: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> HistoryEntryResponse:
    """Put the setting back to its value before an apply, when nothing has
    changed it since. Returns the new history entry."""
    with unit_of_work() as locked:
        _entry, row = _refusing(store.locked_applied, locked, history_id)
        _change(row.setting_key, row.to_value, row.from_value, session, current_user)
        return _record(locked, row, "undone", current_user)


def _refusing(fn, *args):
    try:
        return fn(*args)
    except store.StoreRefusal as refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail) from None


def _record(locked, row, outcome: str, user: User) -> HistoryEntryResponse:
    principal_id = getattr(user, "principal_id", None) or getattr(user, "user_id", None)
    entry = store.record(
        locked, row, outcome, user.username, principal_id and str(principal_id)
    )
    return HistoryEntryResponse.model_validate(entry)


_CEILING_FIELDS = {
    "act.reversible": "act_reversible",
    "act.irreversible": "act_irreversible",
    "act.four_eyes": "four_eyes",
}


def _change(key: str, expected: str, value: str, session, user: User) -> None:
    """Write ``value`` to ``key`` through Settings' own endpoint, if it is ``expected`` now."""
    if key not in store.APPLICABLE:
        raise HTTPException(status_code=409, detail=f"Vigil can't change {key}")
    now = next((r for r in settings() if r["key"] == key), None)
    if now is None or not store.same_value(now["value"], expected):
        shown = now["value"] if now else "unknown"
        raise HTTPException(
            status_code=409,
            detail=f"{key} is {shown} now, not {expected}: it changed since",
        )
    if key in _CEILING_FIELDS:
        put_ceiling(CeilingBody(**{_CEILING_FIELDS[key]: _flag(value)}), session, user)
        return
    field = key.removeprefix("investigate.")
    effective = DaemonConfig.from_env().orchestrator
    # Every field from the effective values, so the write changes only this one:
    # a saved row overrides env, and model defaults would overwrite env-set values.
    data = {
        name: getattr(effective, name)
        for name in OrchestratorSettingsConfig.model_fields
        if hasattr(effective, name)
    }
    data[field] = _flag(value) if field == "enabled" else value
    try:
        body = OrchestratorSettingsConfig(**data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    set_orchestrator_config(body, user)


def _flag(value: str) -> bool:
    if value in ("on", "true"):
        return True
    if value in ("off", "false"):
        return False
    raise HTTPException(status_code=422, detail=f"{value} is not on or off")
