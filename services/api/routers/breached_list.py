"""Importing a newer breached-password list, offline (#4175).

The import is the operator's choice, and so is the licence of whatever file they
load; Vigil looks nothing up online. It only adds entries, so a machine caller
holding ``sessions.manage`` may use it and no "Confirm it's you" is asked.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from core.auth import breached_list, password_validator, trail_events
from core.auth.current_user import get_current_user
from core.auth.permissions import missing_permission
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User
from core.storage.schemas.base import OptDateTime

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/session-policy/breached-list",
    tags=["session-policy"],
    auth=Auth.REQUIRED,
)


def _manage(user: User = Depends(get_current_user)) -> User:
    """``sessions.manage``, without "Confirm it's you": an import only adds entries."""
    if missing_permission(user.user_id, "sessions.manage") is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: sessions.manage required",
        )
    return user


class ListStatus(BaseModel):
    # "Checked: YYYY-MM-DD" in data/NOTICE: when the shipped list was last refreshed.
    shipped_date: Optional[date] = None
    # Distinct lowercased entries in the shipped file.
    shipped_count: int
    imported_count: int
    last_import_at: OptDateTime = None
    last_import_sha256: Optional[str] = None
    # The later of the shipped date and the last import.
    refreshed_at: OptDateTime = None
    stale: bool


class ImportResult(BaseModel):
    read: int
    added: int
    # Shipped, imported before, or repeated in the file.
    already_listed: int
    # Blank-free lines that were too short or long, email-shaped or not UTF-8.
    skipped: int
    sha256: str
    refreshed_at: OptDateTime = None


@router.get("", response_model=ListStatus)
def get_status(session: UnitOfWorkSession, _: User = Depends(_manage)):
    """The shipped list's date and size, what has been imported, and whether it is stale."""
    return breached_list.status(session, len(password_validator._get_blocklist()))


@router.post("", response_model=ImportResult)
def import_list(
    session: UnitOfWorkSession,
    file: UploadFile = File(...),
    user: User = Depends(_manage),
):
    """Add the entries of a plain-text file, one per line, to the breached list.

    Entries under 12 characters, email-shaped ones and blanks are dropped; the
    rest are lowercased and added, never removed. Every accepted import counts as
    a refresh, even one that adds nothing. The trail records the counts and the
    file's SHA-256, never an entry. What a file may be used for is the operator's
    licence to read, not Vigil's."""
    size = file.file.seek(0, os.SEEK_END)
    file.file.seek(0)
    if size > breached_list.MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The file is over {breached_list.MAX_BYTES // 2**20} MB",
        )
    result = breached_list.import_entries(
        session, file.file, password_validator._get_blocklist()
    )
    trail_events.record(
        session,
        "breached_list.imported",
        actor=user.principal_id,
        **{
            k: result[k]
            for k in ("read", "added", "already_listed", "skipped", "sha256")
        },
    )
    return result
