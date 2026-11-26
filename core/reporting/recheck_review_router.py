"""Blind review of a finished capped re-check, and the Case opened from it (#5095).

Confirm and Case open take a signed-in person with ``cases.write``. A token
or a service account is refused. Ticket text stays on ``automation.read``.
Ledger evidence stays on ``cases.read``. There is no review permission.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth.permissions import missing_permission, require_person_permission
from core.reporting.recheck_review import (
    CALLS,
    CannotOpen,
    NotQueued,
    Removed,
    confirm,
    open_reviewed_case,
    queue,
    ticket_view,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/recheck-review",
    tags=["recheck-review"],
    auth=Auth.REQUIRED,
)

_WRITE = require_person_permission("cases.write", refuse_machine=True)
CallName = Literal["real", "not real", "can't tell"]


class QueueItem(BaseModel):
    id: int
    batch_id: str
    source: str
    closed_at: str
    ticket_id: str
    title: Optional[str] = None
    recorded: bool
    call: Optional[CallName] = None
    reviewers_differ: bool
    can_open: bool
    case_id: Optional[str] = None


class QueueOut(BaseModel):
    pending: List[QueueItem]
    reviewed: List[QueueItem]


class AlertOut(BaseModel):
    time: str
    title: str
    rule: str
    severity: str
    entity_keys: List[str]
    description: str


class EvidenceOut(BaseModel):
    tool: str
    result: str


class ActionOut(BaseModel):
    action_type: str = ""
    target: str = ""
    reason: str = ""
    confidence: Optional[float] = None


class TicketOut(QueueItem):
    removed: bool = False
    notice: Optional[str] = None
    alerts: Optional[List[AlertOut]] = None
    alerts_hidden: bool = False
    evidence: Optional[List[EvidenceOut]] = None
    evidence_hidden: bool = False
    closure_verdict: Optional[str] = None
    vigil_verdict: Optional[str] = None
    summary: Optional[str] = None
    proposed_actions: Optional[List[ActionOut]] = None
    run_id: Optional[str] = None


class ConfirmIn(BaseModel):
    call: CallName


class OpenedOut(BaseModel):
    case_id: str = Field(min_length=1)


def _sees(user: User) -> tuple[bool, bool]:
    """Ticket text is automation.read. Ledger evidence is cases.read."""
    return (
        missing_permission(user.user_id, "automation.read") is None,
        missing_permission(user.user_id, "cases.read") is None,
    )


def _hide_titles(body: dict, see_text: bool) -> dict:
    if see_text:
        return body
    for key in ("pending", "reviewed"):
        for item in body[key]:
            item["title"] = None
    return body


@router.get("", response_model=QueueOut)
def get_queue(
    session: UnitOfWorkSession,
    user: User = Depends(require_person_permission("cases.read")),
):
    """Unconfirmed tickets, then confirmed ones. The two are not one list.

    ``cases.read`` is the screen. Titles still drop without ``automation.read``.
    """
    see_text, _ledger = _sees(user)
    return _hide_titles(queue(session, actor=user.principal_id), see_text)


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(
    ticket_id: int,
    session: UnitOfWorkSession,
    user: User = Depends(require_person_permission("cases.read")),
):
    """Alert fields and tool results. Verdicts stay off until a call is stored."""
    see_text, see_ledger = _sees(user)
    try:
        return ticket_view(
            session,
            ticket_id,
            actor=user.principal_id,
            see_text=see_text,
            see_ledger=see_ledger,
        )
    except Removed as gone:
        return {
            "id": ticket_id,
            "batch_id": "",
            "source": "",
            "closed_at": "",
            "ticket_id": "",
            "title": None,
            "recorded": False,
            "call": None,
            "reviewers_differ": False,
            "can_open": False,
            "case_id": None,
            "removed": True,
            "notice": gone.notice,
        }
    except NotQueued:
        raise HTTPException(status_code=404, detail="Not in the review queue")


@router.post("/{ticket_id}/confirm", response_model=TicketOut)
def post_confirm(
    ticket_id: int,
    body: ConfirmIn,
    session: UnitOfWorkSession,
    user: User = Depends(_WRITE),
):
    """Record the call. A token or a service account is refused."""
    see_text, see_ledger = _sees(user)
    if body.call not in CALLS:
        raise HTTPException(
            status_code=422, detail="The call is real, not real, or can't tell"
        )
    try:
        return confirm(
            session,
            ticket_id,
            body.call,
            actor=user.principal_id,
            see_text=see_text,
            see_ledger=see_ledger,
        )
    except Removed as gone:
        raise HTTPException(status_code=409, detail=gone.notice)
    except NotQueued:
        raise HTTPException(status_code=404, detail="Not in the review queue")


@router.post("/{ticket_id}/case", response_model=OpenedOut)
def post_case(
    ticket_id: int,
    session: UnitOfWorkSession,
    user: User = Depends(_WRITE),
):
    """Open a Case from the first confirming call. No finding, no intake."""
    try:
        return open_reviewed_case(session, ticket_id, actor=user.principal_id)
    except Removed as gone:
        raise HTTPException(status_code=409, detail=gone.notice)
    except NotQueued:
        raise HTTPException(status_code=404, detail="Not in the review queue")
    except CannotOpen:
        raise HTTPException(
            status_code=403, detail="This confirmation cannot open a Case"
        )
