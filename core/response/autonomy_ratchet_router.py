"""Autonomy ratchet: the console's view of a hold only a person can release."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.response.autonomy_ratchet import (
    EnvironmentHolds,
    Evidence,
    NotHolding,
    RatchetView,
    promote_ratchet,
    sync_ratchet,
)
from core.routing import Auth, RouterMeta
from core.storage.models import User

router = APIRouter()
logger = logging.getLogger(__name__)

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["autonomy"],
    auth=Auth.REQUIRED,
)

_SETTINGS_WRITE = [permission_gate("settings.write")]


class EvidenceRow(BaseModel):
    """One disagreement the hold is shown with."""

    kind: str
    id: str
    summary: str
    at: str


class RatchetResponse(BaseModel):
    """Whether Vigil is holding a tighter stance, and the rule that decides it."""

    holding: bool
    due: bool
    reason: str
    evidence: list[EvidenceRow]
    demoted_at: str | None
    promoted_at: str | None
    promoted_by: str | None
    promotion_reason: str | None
    environment_wins: bool
    will_restore_act: bool
    blocked_by_environment: bool
    may_promote: bool
    graded: int
    disagreements: int
    rejections: int
    window_hours: int
    min_graded: int
    disagree_rate: float
    min_disagreements: int
    min_rejections: int
    review_threshold: float


class PromoteRequest(BaseModel):
    """Why a person is giving autonomy back."""

    reason: str = Field(max_length=500)

    @field_validator("reason")
    @classmethod
    def _sentence(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 12:
            raise ValueError("Say why in at least a short sentence.")
        return text


def _response(view: RatchetView, user: User) -> RatchetResponse:
    may_promote = view.holding and not view.blocked_by_environment and _may_write(user)
    return RatchetResponse(
        holding=view.holding,
        due=view.due,
        reason=view.reason,
        evidence=[_evidence(item) for item in view.evidence],
        demoted_at=view.demoted_at,
        promoted_at=view.promoted_at,
        promoted_by=view.promoted_by,
        promotion_reason=view.promotion_reason,
        environment_wins=view.environment_wins,
        will_restore_act=view.will_restore_act,
        blocked_by_environment=view.blocked_by_environment,
        may_promote=may_promote,
        graded=view.graded,
        disagreements=view.disagreements,
        rejections=view.rejections,
        window_hours=view.window_hours,
        min_graded=view.min_graded,
        disagree_rate=view.disagree_rate,
        min_disagreements=view.min_disagreements,
        min_rejections=view.min_rejections,
        review_threshold=view.review_threshold,
    )


def _evidence(item: Evidence) -> EvidenceRow:
    return EvidenceRow(kind=item.kind, id=item.id, summary=item.summary, at=item.at)


def _may_write(user: User) -> bool:
    try:
        return bool(AuthService.check_permission(user.user_id, "settings.write"))
    except Exception:
        logger.warning(
            "Could not check settings.write for the autonomy ratchet", exc_info=True
        )
        return False


@router.get("/autonomy/ratchet", response_model=RatchetResponse)
def read_ratchet(current_user: User = Depends(get_current_user)) -> RatchetResponse:
    """The hold and the live window. Does not write."""
    try:
        view = sync_ratchet(commit=False)
    except Exception as exc:
        logger.warning("Autonomy ratchet read failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Could not read the autonomy ratchet"
        ) from exc
    return _response(view, current_user)


@router.post("/autonomy/ratchet/sync", response_model=RatchetResponse)
def catch_up(current_user: User = Depends(get_current_user)) -> RatchetResponse:
    """Commit a tightening the rule has already decided. Never loosens.

    Any signed-in console may catch the daemon up. Promotion is separate
    and takes ``settings.write``.
    """
    try:
        view = sync_ratchet()
    except Exception as exc:
        logger.warning("Autonomy ratchet sync failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Could not update the autonomy ratchet"
        ) from exc
    return _response(view, current_user)


@router.post(
    "/autonomy/ratchet/promote",
    dependencies=_SETTINGS_WRITE,
    response_model=RatchetResponse,
)
def promote(
    body: PromoteRequest,
    current_user: User = Depends(get_current_user),
) -> RatchetResponse:
    """Give autonomy back. The ratchet cannot do this on its own."""
    try:
        view = promote_ratchet(
            actor=current_user.username,
            reason=body.reason,
            user_id=str(current_user.user_id),
        )
    except NotHolding as exc:
        raise HTTPException(
            status_code=409,
            detail="Vigil has not tightened itself, so there is nothing to promote.",
        ) from exc
    except EnvironmentHolds as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "The environment is forcing approval, so this promotion "
                "cannot restore Act."
            ),
        ) from exc
    except Exception as exc:
        logger.warning("Autonomy promotion failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Could not record the promotion"
        ) from exc
    return _response(view, current_user)
