"""Imported history (``/api/memory/imports``, #3965).

An import is its row in ``history_imports`` (#4886); uploading one is
``POST /api/ingest/upload`` with ``data_type=history``. Listing needs
``findings.read``; deleting one needs ``findings.write``, as notes do.
That delete keeps re-decision ledgers. Erasing them needs ``ledgers.erase``,
a person's own act, after the import is gone (#5096).
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from core.auth.current_user import get_current_user
from core.auth.permissions import (
    LEDGERS_ERASE,
    permission_gate,
    require_person_permission,
)
from core.memory.imported_history import ImportRunning, delete_import, list_imports
from core.memory.ledger_erasure import (
    ImportStillThere,
    RunNotMarked,
    erase_ledgers,
    marked_run_ids,
)
from core.reporting.recheck_review import queued_count
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


class ImportDeleteOut(ImportOut):
    """The import is gone. Its marked ledgers were kept."""

    ledgers_kept: int
    run_ids: List[str]
    erase_permission: str


class LedgerCountOut(BaseModel):
    count: int
    run_ids: List[str]
    erase_permission: str
    # Queued reviews this import's delete takes off the review queue.
    queued_reviews: int


class LedgerEraseIn(BaseModel):
    run_ids: List[str] = Field(min_length=1)


class SparedCase(BaseModel):
    """A Case a run was opened from. That run is not erased."""

    case_id: str


class LedgerEraseOut(BaseModel):
    erased: int
    # Cases a run was opened from. Those runs are not erased.
    cases: List[SparedCase]


@router.get("", response_model=ImportListResponse)
def get_imports(session: UnitOfWorkSession):
    """Every import, newest upload first."""
    imports = list_imports(session)
    return {"imports": imports, "total": len(imports)}


@router.get("/ledgers/{import_id:path}", response_model=LedgerCountOut)
def count_ledgers(
    session: UnitOfWorkSession,
    import_id: str = Path(..., min_length=1, max_length=512),
):
    """How many marked re-decision ledgers this import would keep."""
    run_ids = marked_run_ids(session, import_id)
    return {
        "count": len(run_ids),
        "run_ids": run_ids,
        "erase_permission": LEDGERS_ERASE,
        "queued_reviews": queued_count(session, import_id),
    }


@router.post(
    "/ledgers/{import_id:path}",
    response_model=LedgerEraseOut,
)
def remove_ledgers(
    body: LedgerEraseIn,
    session: UnitOfWorkSession,
    import_id: str = Path(..., min_length=1, max_length=512),
    current_user: User = Depends(
        require_person_permission(LEDGERS_ERASE, refuse_machine=True)
    ),
):
    """Erase marked re-decision ledgers once the import is gone.

    A run a Case was opened from is not passed. ``cases`` names those.
    Any other id erases nothing.
    """
    try:
        return erase_ledgers(
            session,
            import_id,
            body.run_ids,
            actor=current_user.principal_id,
        )
    except ImportStillThere:
        raise HTTPException(
            status_code=409,
            detail="Delete the import before erasing its re-decision ledgers.",
        )
    except RunNotMarked:
        raise HTTPException(
            status_code=400,
            detail="A run is not a re-decision of this import. Nothing was erased.",
        )


@router.delete(
    "/{import_id:path}",
    response_model=ImportDeleteOut,
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
