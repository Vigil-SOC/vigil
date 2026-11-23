"""The Day 1 Report selection: jurisdictions and data or organisation kinds (#5010).

Read under ``platform.read``. Written under ``platform.write``, which already
asks "Confirm it's you". No new permission.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core.auth.permissions import require_permission
from core.reporting.selection import (
    SelectionError,
    SelectionRejected,
    SelectionView,
    SelectionWriteFailed,
    read_view,
    write_selection,
)
from core.routing import Auth, RouterMeta
from core.storage.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/day1-report/selection",
    tags=["day1-report"],
    auth=Auth.REQUIRED,
)

READ_FAILED = "The stored selection could not be read"
UNAVAILABLE = "Could not read the selection"
SAVE_FAILED = "Could not save the selection"


class SelectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    jurisdictions: list[str] = Field(max_length=50)
    kinds: list[str] = Field(max_length=50)


def _view() -> SelectionView:
    try:
        return read_view()
    except SelectionError:
        raise HTTPException(status_code=500, detail=READ_FAILED) from None
    except (
        Exception
    ) as exc:  # noqa: BLE001 - a failed read must not look like nothing selected
        logger.error("Could not read the day 1 selection: %s", exc)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None


@router.get("", response_model=SelectionView)
def get_selection(
    _user: User = Depends(require_permission("platform.read")),
) -> SelectionView:
    """The stored selection and the catalogue. A missing row is nothing selected."""
    return _view()


@router.put("", response_model=SelectionView)
def put_selection(
    body: SelectionWrite,
    user: User = Depends(require_permission("platform.write")),
) -> SelectionView:
    """Store a closed set of offered ids. Anything else is refused, unchanged."""
    try:
        write_selection(str(user.user_id), body.jurisdictions, body.kinds)
    except SelectionRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except SelectionWriteFailed:
        raise HTTPException(status_code=500, detail=SAVE_FAILED) from None
    return _view()
