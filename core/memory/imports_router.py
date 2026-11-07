"""Imported history (``/api/memory/imports``, #3965).

An import is its marker in ``episodic_distil_markers``; uploading one is
``POST /api/ingest/upload`` with ``data_type=history``. Listing needs
``findings.read``; deleting one needs ``findings.write``, as notes do.
"""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.memory.imported_history import delete_import, list_imports
from core.memory.notes_router import _actor
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()
router.dependencies.append(permission_gate("findings.read"))

ROUTER_META = RouterMeta(
    prefix="/api/memory/imports",
    tags=["memory"],
    auth=Auth.REQUIRED,
)


class ImportOut(BaseModel):
    import_id: str
    source_system: str
    verdicts_written: int
    sightings_written: int
    # Latest close or alert in the file, and when it was (last) uploaded.
    concluded_at: datetime
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
    """Delete one whole import: its Verdicts, Sightings and marker."""
    row = delete_import(session, import_id, actor=_actor(current_user))
    if row is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return row
