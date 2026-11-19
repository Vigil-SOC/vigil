"""Findings — unversioned operator/console endpoints.

The frozen read and record-update surface lives in ``core/api/v1/findings_router.py``
and is mounted at both ``/api/v1/findings`` and (for now) ``/api/findings``.
This module keeps the routes that are *not* part of that contract, such as
the destructive wipe. They remain under ``/api/findings`` only.
"""

import logging
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.audit import trail
from core.auth.permissions import permission_gate
from core.cases import case_trail
from core.cases.closure import ClosedByKind, FalsePositiveCause
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.config_service import get_config_service
from core.storage.intake_queue import insert_intake_trigger, intake_severity_band
from core.storage.models import Finding, User
from core.time import utcnow
from services.api.middleware.auth import get_current_user

router = APIRouter()
_FINDINGS_WRITE = [permission_gate("findings.write")]
router.dependencies.append(permission_gate("findings.read"))

ROUTER_META = RouterMeta(
    prefix="/api/findings",
    tags=["findings"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


class NoiseMarkRequest(BaseModel):
    reason: Optional[str] = None
    noise_cause: Optional[FalsePositiveCause] = None


class NoiseMarkResponse(BaseModel):
    finding_id: str
    noise_marked_at: Optional[datetime] = None
    noise_marked_by: Optional[str] = None
    noise_marked_by_kind: Optional[str] = None
    noise_reason: Optional[str] = None
    noise_cause: Optional[str] = None


class IntakeLaunchResponse(BaseModel):
    finding_id: str
    queued: bool
    already_queued: bool
    trigger_id: Optional[int] = None


def _finding_or_404(session: UnitOfWorkSession, finding_id: str) -> Finding:
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@router.delete("/all", dependencies=[permission_gate("findings.delete")])
def clear_all_findings(
    session: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Delete all findings from the database."""
    count = session.query(Finding).count()
    # The wipe cascades to the AI decision log, so name who did it first.
    get_config_service(user_id=str(current_user.user_id)).record_audit(
        config_type="findings",
        config_key="findings",
        action="delete_all",
        old_value={"count": count},
        new_value=None,
        change_reason="All findings deleted via API",
    )
    session.query(Finding).delete()

    logger.info(f"Cleared {count} findings")
    return {"success": True, "deleted": count, "message": f"Deleted {count} findings"}


@router.post(
    "/{finding_id}/noise",
    response_model=NoiseMarkResponse,
    dependencies=_FINDINGS_WRITE,
)
def mark_finding_noise(
    finding_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
    body: Optional[NoiseMarkRequest] = None,
):
    """Dismiss the finding as noise. The body is optional.

    The console sends one only when a cause was chosen. A cause outside the
    set is rejected before this runs, so the mark does not land. No cause
    stores null.
    """
    finding = _finding_or_404(session, finding_id)
    reason = ((body.reason if body else None) or "").strip()
    finding.noise_marked_at = utcnow()
    finding.noise_marked_by = current_user.user_id
    finding.noise_marked_by_kind = ClosedByKind.ANALYST.value
    finding.noise_reason = reason or None
    finding.noise_cause = body.noise_cause.value if body and body.noise_cause else None
    case_trail.append_or_legacy(
        session,
        action=trail.ACTION_FINDING_NOISE_MARK,
        signed_in=current_user.username,
        case_id=None,
        operator_text=reason or None,
        ids=lambda: {"finding_id": finding_id},
    )
    return NoiseMarkResponse(
        finding_id=finding.finding_id,
        noise_marked_at=finding.noise_marked_at,
        noise_marked_by=finding.noise_marked_by,
        noise_marked_by_kind=finding.noise_marked_by_kind,
        noise_reason=finding.noise_reason,
        noise_cause=finding.noise_cause,
    )


@router.delete(
    "/{finding_id}/noise",
    response_model=NoiseMarkResponse,
    dependencies=_FINDINGS_WRITE,
)
def clear_finding_noise(
    finding_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Clear the noise mark. The actor is the signed-in user; the columns go back to null."""
    finding = _finding_or_404(session, finding_id)
    logger.info("cleared noise mark on %s by %s", finding_id, current_user.user_id)
    if finding.noise_marked_at is not None:
        case_trail.append_or_legacy(
            session,
            action=trail.ACTION_FINDING_NOISE_CLEAR,
            signed_in=current_user.username,
            case_id=None,
            ids=lambda: {"finding_id": finding_id},
        )
    finding.noise_marked_at = None
    finding.noise_marked_by = None
    finding.noise_marked_by_kind = None
    finding.noise_reason = None
    finding.noise_cause = None
    return NoiseMarkResponse(finding_id=finding.finding_id)


@router.post(
    "/{finding_id}/intake",
    response_model=IntakeLaunchResponse,
    dependencies=_FINDINGS_WRITE,
)
def launch_finding_into_intake(
    finding_id: str,
    session: UnitOfWorkSession,
):
    """Queue this finding for intake. ``already_queued`` when a queued row exists."""
    finding = _finding_or_404(session, finding_id)
    trigger_id = insert_intake_trigger(
        kind="detection",
        finding_id=finding.finding_id,
        priority=intake_severity_band("detection", finding_severity=finding.severity),
    )
    return IntakeLaunchResponse(
        finding_id=finding.finding_id,
        queued=trigger_id is not None,
        already_queued=trigger_id is None,
        trigger_id=trigger_id,
    )
