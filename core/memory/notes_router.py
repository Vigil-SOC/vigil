"""Analyst notes on an entity (``/api/memory/notes``, #3605).

Console-only, outside ``/api/v1`` like ``/api/exclusions``. Notes are
install-wide. Reading needs ``findings.read``; writing or withdrawing one needs
``findings.write``, because every run that recalls the entity reads it.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.memory.notes import NoteError, list_notes, withdraw_note, write_note
from core.memory.recall_contract import EPISODIC_PROSE_CAP
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()
router.dependencies.append(permission_gate("findings.read"))

_FINDINGS_WRITE = [permission_gate("findings.write")]

ROUTER_META = RouterMeta(
    prefix="/api/memory/notes",
    tags=["memory"],
    auth=Auth.REQUIRED,
)


class NoteOut(BaseModel):
    """The recall row: ``investigation_id`` is the note's id."""

    investigation_kind: str
    investigation_id: str
    concluded_at: str
    entity_key: str
    statement: str
    written_by: str


class NoteListResponse(BaseModel):
    notes: List[NoteOut]
    total: int


class NoteCreate(BaseModel):
    entity_key: str = Field(
        ..., max_length=512, description="`type:value`, e.g. ip:10.0.5.12."
    )
    statement: str = Field(..., min_length=1, max_length=EPISODIC_PROSE_CAP)


def _actor(user: User) -> str:
    return user.username or user.user_id


@router.get("", response_model=NoteListResponse)
def get_notes(
    session: UnitOfWorkSession,
    entity_key: Optional[List[str]] = Query(
        None, description="Repeat for several; omit for every note."
    ),
):
    """Notes on these entities, or every note, newest first."""
    try:
        notes = list_notes(session, entity_key)
    except NoteError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"notes": notes, "total": len(notes)}


@router.post(
    "",
    response_model=NoteOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_FINDINGS_WRITE,
)
def add_note(
    body: NoteCreate,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
):
    """Write a note. Several per entity are allowed; there is no edit."""
    try:
        return write_note(
            session,
            entity_key=body.entity_key,
            statement=body.statement,
            written_by=_actor(current_user),
        )
    except NoteError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/{note_id}", response_model=NoteOut, dependencies=_FINDINGS_WRITE)
def remove_note(
    session: UnitOfWorkSession,
    note_id: int = Path(..., ge=1, le=2**63 - 1),
    current_user: User = Depends(get_current_user),
):
    """Withdraw a note: the row is deleted, and the trail keeps what it said."""
    row = withdraw_note(session, note_id, withdrawn_by=_actor(current_user))
    if row is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return row
