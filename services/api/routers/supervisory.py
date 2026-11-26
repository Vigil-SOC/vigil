"""The Supervisory model page: its read, and a person applying, undoing or dismissing a suggestion.

Apply and undo write through the same endpoints Settings uses (the autonomy
ceiling, the orchestrator settings), so every check those make still holds:
the permission for the direction, the environment lock, a stop, INTENT.md
managed keys, and the bounds.
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.api.v1.autonomy_router import CeilingBody, put_ceiling
from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.daemon_config import DaemonConfig
from core.response.config import applied_intent
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work
from core.supervisory import medic_hold, store, worker
from core.supervisory.failures import failures
from core.supervisory.feedback import feedback
from core.supervisory.settings import settings
from services.api.middleware.auth import get_current_active_user
from services.api.routers.config import (
    OrchestratorSettingsConfig,
    set_orchestrator_config,
)

logger = logging.getLogger(__name__)

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
    """What the supervisory model has to learn from, its suggestions, the settings
    it watches, and why agent runs stopped early.

    The state and the check times come from the daemon's scheduled check
    (``core.supervisory.worker``).
    """
    read = feedback()
    # One INTENT.md read for the settings list and every card and undo checked against it.
    applied = applied_intent(fresh=True)
    config = DaemonConfig.from_env()
    watched = settings(applied, config)
    stored = store.read(
        {row["key"]: row for row in watched},
        applied,
        _orchestrator_validator(config.orchestrator),
    )
    held_by = medic_hold.open_related()
    return JSONResponse(
        {
            "status": {
                **worker.status(held_by),
                "history_days": read["history_days"],
                "functions_watched": len(read["functions"]),
                "medic_open_related": held_by,
            },
            "functions": read["functions"],
            "suggestions": stored["suggestions"],
            "held": stored["held"],
            "failures": failures(),
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

    The same change is not suggested again for 30 days, whatever the reason.
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
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> HistoryEntryResponse:
    """Change the setting to the suggested value, when it still has the value the
    suggestion started from. Returns the history entry."""
    written = None
    try:
        with unit_of_work() as locked:
            row = _refusing(store.locked_live, locked, suggestion_id)
            if row.setting_key.startswith("investigate."):
                store.lock_orchestrator_row(locked)
            _change(
                row.setting_key, row.from_value, row.to_value, session, current_user
            )
            written = (row.setting_key, row.to_value)
            entry = _record(locked, row, "applied", current_user)
    except Exception:
        _unrecorded(written, "apply", suggestion_id)
        raise
    entry["undoable"] = _undo_possible(entry)
    # Shaped after the commit, so nothing here can roll back a recorded change.
    return HistoryEntryResponse.model_validate(entry)


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
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> HistoryEntryResponse:
    """Put the setting back to its value before an apply, when nothing has
    changed it since. Returns the new history entry.

    The same change is not suggested again for 30 days.
    """
    written = None
    try:
        with unit_of_work() as locked:
            _entry, row = _refusing(store.locked_applied, locked, history_id)
            if row.setting_key.startswith("investigate."):
                store.lock_orchestrator_row(locked)
            _change(
                row.setting_key, row.to_value, row.from_value, session, current_user
            )
            written = (row.setting_key, row.from_value)
            entry = _record(locked, row, "undone", current_user)
    except Exception:
        _unrecorded(written, "undo", history_id)
        raise
    return HistoryEntryResponse.model_validate(entry)


def _undo_possible(entry: dict) -> bool:
    """Whether the apply just recorded can be put back, read fresh after it landed."""
    try:
        applied = applied_intent(fresh=True)
        config = DaemonConfig.from_env()
        current = {r["key"]: r for r in settings(applied, config)}
        return store.can_undo(
            entry["key"],
            entry["to"],
            entry["from"],
            current,
            applied,
            _orchestrator_validator(config.orchestrator),
        )
    except Exception:  # noqa: BLE001
        # The apply is recorded; the page's next read says whether it can be undone.
        logger.warning("could not tell whether %s can be undone", entry["key"])
        return False


def _unrecorded(written: tuple[str, str] | None, what: str, ref: str) -> None:
    """The setting write and its record are two transactions. When the record
    fails after the write, say so loudly: the change happened, unrecorded."""
    if written is not None:
        logger.error(
            "supervisory %s %s changed %s to %s but its history was not recorded",
            what,
            ref,
            *written,
        )


def _refusing(fn, *args):
    try:
        return fn(*args)
    except store.StoreRefusal as refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail) from None


def _record(locked, row, outcome: str, user: User) -> dict:
    principal_id = getattr(user, "principal_id", None) or getattr(user, "user_id", None)
    return store.record(
        locked, row, outcome, user.username, principal_id and str(principal_id)
    )


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
    try:
        body = _orchestrator_body(key, value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_first_error(exc)) from None
    set_orchestrator_config(body, user)


def _orchestrator_body(key: str, value: str) -> OrchestratorSettingsConfig:
    """The orchestrator row a one-click write sends: every field from the
    effective values, so the write changes only ``key``; ValueError when the
    model refuses it."""
    field = key.removeprefix("investigate.")
    effective = DaemonConfig.from_env().orchestrator
    # A saved row overrides env, and model defaults would overwrite env-set values.
    data = {
        name: getattr(effective, name)
        for name in OrchestratorSettingsConfig.model_fields
        if hasattr(effective, name)
    }
    data[field] = _flag(value) if field == "enabled" else value
    return OrchestratorSettingsConfig(**data)


def _orchestrator_validator(effective=None):
    """A check, for this page read, of whether the orchestrator model would take
    a value: its bounds and types, with every other field as it is now."""
    effective = effective or DaemonConfig.from_env().orchestrator
    base = {
        name: getattr(effective, name)
        for name in OrchestratorSettingsConfig.model_fields
        if hasattr(effective, name)
    }

    def validate(key: str, value: str) -> str | None:
        if not key.startswith("investigate."):
            return None
        field = key.removeprefix("investigate.")
        data = dict(base)
        try:
            data[field] = _flag(value) if field == "enabled" else value
            OrchestratorSettingsConfig(**data)
        except HTTPException as exc:
            return str(exc.detail)
        except ValueError as exc:
            return _first_error(exc)
        return None

    return validate


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        first = (errors() or [{}])[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return (
            f"{where}: {first.get('msg', exc)}" if where else str(first.get("msg", exc))
        )
    return str(exc)


def _flag(value: str) -> bool:
    if value in ("on", "true"):
        return True
    if value in ("off", "false"):
        return False
    raise HTTPException(status_code=422, detail=f"{value} is not on or off")
