"""Imported history (``/api/memory/imports``, #3965).

An import is its row in ``history_imports`` (#4886); uploading one is
``POST /api/ingest/upload`` with ``data_type=history``. Listing needs
``findings.read``; deleting one needs ``findings.write``, as notes do.
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.memory.imported_history import ImportRunning, delete_import, list_imports
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()
router.dependencies.append(permission_gate("findings.read"))

ROUTER_META = RouterMeta(
    prefix="/api/memory/imports",
    tags=["memory"],
    auth=Auth.REQUIRED,
)


class UnmappedValue(BaseModel):
    value: str
    count: int


class DroppedColumn(BaseModel):
    name: str
    count: int


class ImportOut(BaseModel):
    import_id: str
    source_system: str
    # ``incomplete``: a run that stopped before it finished ("didn't complete").
    status: Literal["running", "done", "failed", "incomplete"]
    error: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]
    rows_read: int
    # Kept in the import's ticket record, bulk-closed included.
    tickets_kept: int
    verdicts_written: int
    sightings_written: int
    # {"tickets" | "alerts" | "rows": {reason: count}}, left out of memory.
    left_out: Dict[str, Dict[str, int]]
    # Tickets with no closer field, so not tested for bulk closes.
    bulk_not_tested: int
    severity_unknown: int
    # Disposition values no listed value matches, most common first.
    unmapped: List[UnmappedValue]
    # Alert-column names whose values were not stored, most common first.
    dropped_columns: List[DroppedColumn]
    # A connected pull's window.
    pull_days: Optional[int]
    pulled_at: Optional[datetime]
    # False for an import made before ticket detail was recorded.
    has_ticket_detail: bool
    # Latest close or alert in the import, and when it was (last) run.
    concluded_at: Optional[datetime]
    imported_at: datetime


class ImportListResponse(BaseModel):
    imports: List[ImportOut]
    total: int


@router.get("", response_model=ImportListResponse)
def get_imports(session: UnitOfWorkSession):
    """Every import, newest upload first."""
    imports = list_imports(session)
    return {"imports": imports, "total": len(imports)}


@router.delete(
    "/{import_id:path}",
    response_model=ImportOut,
    dependencies=[permission_gate("findings.write")],
)
def remove_import(
    session: UnitOfWorkSession,
    import_id: str = Path(..., min_length=1, max_length=512),
    current_user: User = Depends(get_current_user),
):
    """Delete one whole import: its Verdicts, Sightings, tickets, alerts and record."""
    try:
        row = delete_import(session, import_id, actor=current_user.principal_id)
    except ImportRunning:
        raise HTTPException(
            status_code=409,
            detail="A history import is running. Wait for it to finish.",
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return row
