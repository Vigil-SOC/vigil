"""Autonomy ratchet: Vigil may tighten itself, and only a person loosens it.

The product promise is a human on the loop. Graded AI decisions and rejected
high-confidence response proposals are evidence the unattended stance was too
loose. When that evidence crosses the line, this module forces manual approval
and holds there. Nothing here clears that hold except ``promote``, which a
person asks for with a reason.

The daemon and ``POST /api/autonomy/ratchet/sync`` both call :func:`sync_ratchet`.
A sync that is not over the line writes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import select

from core.config import get_settings
from core.response.approval_service import APPROVAL_CONFIG_KEY
from core.response.config import ResponseConfig
from core.storage.config_service import get_config_service
from core.storage.connection import get_db_manager
from core.storage.models import AIDecisionLog, ApprovalAction
from core.time import utcnow

logger = logging.getLogger(__name__)

RATCHET_KEY = "autonomy.ratchet"
WINDOW_HOURS = 24
MIN_GRADED = 4
DISAGREE_RATE = 0.40
MIN_DISAGREEMENTS = 2
MIN_REJECTIONS = 2
MACHINE_ACTORS = frozenset({"system", "agent"})
_SAMPLE_LIMIT = 500


class NotHolding(Exception):
    """A promotion was asked for while Vigil has not tightened itself."""


class EnvironmentHolds(Exception):
    """The environment is forcing approval, so a promotion cannot restore Act."""


@dataclass(frozen=True)
class Grade:
    """One analyst grade on an AI decision."""

    decision_id: str
    human_decision: str
    at: datetime
    summary: str


@dataclass(frozen=True)
class ProposalRejection:
    """A person rejected a response proposal."""

    action_id: str
    title: str
    confidence: float
    at: datetime
    rejected_by: str


@dataclass(frozen=True)
class Evidence:
    """One row the hold is explained with."""

    kind: str
    id: str
    summary: str
    at: str


@dataclass(frozen=True)
class Verdict:
    """Whether the recent window is over the tightening line."""

    tighten: bool
    reason: str
    evidence: tuple[Evidence, ...]
    graded: int
    disagreements: int
    rejections: int


@dataclass(frozen=True)
class StoredRatchet:
    """The hold, as kept in ``autonomy.ratchet``."""

    holding: bool = False
    reason: str = ""
    evidence: tuple[Evidence, ...] = ()
    demoted_at: Optional[str] = None
    promoted_at: Optional[datetime] = None
    promoted_by: Optional[str] = None
    promotion_reason: Optional[str] = None
    tightened_approval: bool = False
    graded: int = 0
    disagreements: int = 0
    rejections: int = 0


@dataclass(frozen=True)
class DemotionWrite:
    """What a tightening persists. ``enable_force_approval`` is false when Assist was already on."""

    stored: StoredRatchet
    enable_force_approval: bool


@dataclass(frozen=True)
class PromotionWrite:
    """What a person's promotion persists."""

    stored: StoredRatchet
    clear_force_approval: bool


@dataclass(frozen=True)
class RatchetView:
    """The console payload, aside from whether this user may promote."""

    holding: bool
    due: bool
    reason: str
    evidence: tuple[Evidence, ...]
    demoted_at: Optional[str]
    promoted_at: Optional[str]
    promoted_by: Optional[str]
    promotion_reason: Optional[str]
    environment_wins: bool
    will_restore_act: bool
    blocked_by_environment: bool
    graded: int
    disagreements: int
    rejections: int
    window_hours: int = WINDOW_HOURS
    min_graded: int = MIN_GRADED
    disagree_rate: float = DISAGREE_RATE
    min_disagreements: int = MIN_DISAGREEMENTS
    min_rejections: int = MIN_REJECTIONS
    review_threshold: float = 0.85


def judge(
    grades: Sequence[Grade],
    rejections: Sequence[ProposalRejection],
    *,
    now: datetime,
    promoted_at: Optional[datetime],
    review_threshold: float,
    window: timedelta = timedelta(hours=WINDOW_HOURS),
) -> Verdict:
    """Decide whether the window since the last promotion is too loose.

    Grades and rejections at or before ``promoted_at`` do not count: a
    promotion starts a fresh window. ``partial`` weighs half a disagreement
    and still counts as a disagreement event.
    """
    counted_grades = [
        grade
        for grade in grades
        if grade.human_decision in ("agree", "disagree", "partial")
        and _in_window(grade.at, now, promoted_at, window)
    ]
    counted_rejections = [
        rejection
        for rejection in rejections
        if _human_actor(rejection.rejected_by)
        and rejection.confidence >= review_threshold
        and _in_window(rejection.at, now, promoted_at, window)
    ]
    disagreements = sum(
        1 for grade in counted_grades if grade.human_decision in ("disagree", "partial")
    )
    weight = sum(_weight(grade.human_decision) for grade in counted_grades)
    graded = len(counted_grades)
    rejection_count = len(counted_rejections)
    rate = (weight / graded) if graded else 0.0
    grade_trigger = (
        graded >= MIN_GRADED
        and disagreements >= MIN_DISAGREEMENTS
        and rate >= DISAGREE_RATE
    )
    rejection_trigger = rejection_count >= MIN_REJECTIONS
    parts: list[str] = []
    if grade_trigger:
        parts.append(
            f"Analysts marked {disagreements} of {graded} graded decisions "
            f"as disagree or partial in the last {WINDOW_HOURS} hours."
        )
    if rejection_trigger:
        parts.append(
            f"Analysts rejected {rejection_count} response proposals that had "
            f"reached the review line in the last {WINDOW_HOURS} hours."
        )
    evidence = _evidence(counted_grades, counted_rejections)
    return Verdict(
        tighten=grade_trigger or rejection_trigger,
        reason=" ".join(parts),
        evidence=evidence,
        graded=graded,
        disagreements=disagreements,
        rejections=rejection_count,
    )


def demotion_write(
    verdict: Verdict,
    stored: StoredRatchet,
    *,
    approval_enabled: bool,
    now: datetime,
) -> Optional[DemotionWrite]:
    """The tightening to persist, or None when the hold already exists or the line is quiet."""
    if stored.holding or not verdict.tighten:
        return None
    enable = not approval_enabled
    return DemotionWrite(
        enable_force_approval=enable,
        stored=StoredRatchet(
            holding=True,
            reason=verdict.reason,
            evidence=verdict.evidence,
            demoted_at=now.isoformat(timespec="seconds"),
            promoted_at=stored.promoted_at,
            promoted_by=stored.promoted_by,
            promotion_reason=stored.promotion_reason,
            tightened_approval=enable,
            graded=verdict.graded,
            disagreements=verdict.disagreements,
            rejections=verdict.rejections,
        ),
    )


def promotion_write(
    stored: StoredRatchet,
    *,
    environment_wins: bool,
    actor: str,
    reason: str,
    now: datetime,
) -> PromotionWrite:
    """The promotion to persist.

    Raises :class:`NotHolding` when there is nothing to give back, and
    :class:`EnvironmentHolds` when this hold is what turned Assist on and the
    environment would keep it on anyway.
    """
    if not stored.holding:
        raise NotHolding()
    if stored.tightened_approval and environment_wins:
        raise EnvironmentHolds()
    return PromotionWrite(
        clear_force_approval=stored.tightened_approval,
        stored=StoredRatchet(
            holding=False,
            reason=stored.reason,
            evidence=stored.evidence,
            demoted_at=stored.demoted_at,
            promoted_at=now,
            promoted_by=actor,
            promotion_reason=reason,
            tightened_approval=False,
            graded=stored.graded,
            disagreements=stored.disagreements,
            rejections=stored.rejections,
        ),
    )


def environment_wins() -> bool:
    """Force-approval, or auto-response turned off, beats a stored Act."""
    settings = get_settings()
    return bool(settings.daemon_force_approval) or not settings.daemon_auto_response


def sync_ratchet(*, commit: bool = True) -> RatchetView:
    """Read the window and, when ``commit`` is set, persist a tightening.

    Never loosens. ``commit=False`` is the console's read. A failed read
    raises; callers decide whether that is a 503.
    """
    now = utcnow()
    threshold = float(ResponseConfig.from_settings().review_threshold)
    stored = _read_stored()
    grades, rejections = _load_samples()
    verdict = judge(
        grades,
        rejections,
        now=now,
        promoted_at=stored.promoted_at,
        review_threshold=threshold,
    )
    approval_enabled = _approval_enabled()
    write = demotion_write(verdict, stored, approval_enabled=approval_enabled, now=now)
    if commit and write is not None:
        _persist_demotion(write)
        stored = write.stored
        logger.info("Autonomy ratchet tightened: %s", stored.reason)
    return _view(stored, verdict, threshold, environment_wins())


def promote_ratchet(*, actor: str, reason: str, user_id: str) -> RatchetView:
    """Record a person's promotion and restore Act when this hold was what removed it."""
    now = utcnow()
    threshold = float(ResponseConfig.from_settings().review_threshold)
    stored = _read_stored()
    env_wins = environment_wins()
    write = promotion_write(
        stored,
        environment_wins=env_wins,
        actor=actor,
        reason=reason,
        now=now,
    )
    service = get_config_service(user_id=user_id)
    if write.clear_force_approval:
        saved = service.set_system_config(
            key=APPROVAL_CONFIG_KEY,
            value={"enabled": False},
            description="Force manual approval for all actions",
            config_type="approval",
            change_reason=f"Autonomy promotion by {actor}: {reason}",
        )
        if not saved:
            raise RuntimeError("Could not restore Act")
    if not _save_record(service, write.stored, change_reason=reason):
        raise RuntimeError("Could not record the promotion")
    grades, rejections = _load_samples()
    verdict = judge(
        grades,
        rejections,
        now=now,
        promoted_at=write.stored.promoted_at,
        review_threshold=threshold,
    )
    logger.info("Autonomy ratchet promoted by %s", actor)
    return _view(write.stored, verdict, threshold, env_wins)


def _view(
    stored: StoredRatchet,
    verdict: Verdict,
    review_threshold: float,
    env_wins: bool,
) -> RatchetView:
    holding = stored.holding
    due = verdict.tighten and not holding
    reason = stored.reason if holding else verdict.reason
    evidence = stored.evidence if holding else verdict.evidence
    will_restore = holding and stored.tightened_approval and not env_wins
    blocked = holding and stored.tightened_approval and env_wins
    promoted_at = (
        stored.promoted_at.isoformat(timespec="seconds") if stored.promoted_at else None
    )
    return RatchetView(
        holding=holding,
        due=due,
        reason=reason,
        evidence=evidence,
        demoted_at=stored.demoted_at,
        promoted_at=promoted_at,
        promoted_by=stored.promoted_by,
        promotion_reason=stored.promotion_reason,
        environment_wins=env_wins,
        will_restore_act=will_restore,
        blocked_by_environment=blocked,
        graded=verdict.graded,
        disagreements=verdict.disagreements,
        rejections=verdict.rejections,
        review_threshold=review_threshold,
    )


def _in_window(
    at: Optional[datetime],
    now: datetime,
    promoted_at: Optional[datetime],
    window: timedelta,
) -> bool:
    if at is None or at < now - window:
        return False
    if promoted_at is not None and at <= promoted_at:
        return False
    return True


def _human_actor(name: Optional[str]) -> bool:
    actor = (name or "").strip().casefold()
    return bool(actor) and actor not in MACHINE_ACTORS


def _weight(decision: str) -> float:
    if decision == "disagree":
        return 1.0
    if decision == "partial":
        return 0.5
    return 0.0


def _evidence(
    grades: Sequence[Grade], rejections: Sequence[ProposalRejection]
) -> tuple[Evidence, ...]:
    rows: list[tuple[datetime, Evidence]] = []
    for grade in grades:
        if grade.human_decision not in ("disagree", "partial"):
            continue
        rows.append(
            (
                grade.at,
                Evidence(
                    kind="decision",
                    id=grade.decision_id,
                    summary=_clip(f"{grade.human_decision}: {grade.summary}"),
                    at=grade.at.isoformat(timespec="seconds"),
                ),
            )
        )
    for rejection in rejections:
        rows.append(
            (
                rejection.at,
                Evidence(
                    kind="rejection",
                    id=rejection.action_id,
                    summary=_clip(
                        f"{rejection.rejected_by} rejected {rejection.title}"
                    ),
                    at=rejection.at.isoformat(timespec="seconds"),
                ),
            )
        )
    rows.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[1] for item in rows[:5])


def _clip(text: str, limit: int = 160) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _load_samples() -> tuple[list[Grade], list[ProposalRejection]]:
    db = get_db_manager()
    with db.session_scope() as session:
        grade_rows = session.scalars(
            select(AIDecisionLog)
            .where(AIDecisionLog.human_decision.is_not(None))
            .order_by(AIDecisionLog.feedback_timestamp.desc())
            .limit(_SAMPLE_LIMIT)
        ).all()
        rejection_rows = session.scalars(
            select(ApprovalAction)
            .where(ApprovalAction.status == "rejected")
            .order_by(ApprovalAction.approved_at.desc())
            .limit(_SAMPLE_LIMIT)
        ).all()
        grades = [
            Grade(
                decision_id=row.decision_id,
                human_decision=(row.human_decision or "").casefold(),
                at=row.feedback_timestamp or row.timestamp,
                summary=row.recommended_action or row.decision_type or row.decision_id,
            )
            for row in grade_rows
        ]
        rejections = [
            ProposalRejection(
                action_id=row.action_id,
                title=row.title or row.action_type,
                confidence=float(row.confidence or 0),
                at=row.approved_at or row.created_at,
                rejected_by=row.approved_by or "",
            )
            for row in rejection_rows
        ]
    return grades, rejections


def _approval_enabled() -> bool:
    value = get_config_service().read_system_config(APPROVAL_CONFIG_KEY)
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return False


def _read_stored() -> StoredRatchet:
    value = get_config_service().read_system_config(RATCHET_KEY)
    if not isinstance(value, dict):
        return StoredRatchet()
    evidence = tuple(
        Evidence(
            kind=str(item.get("kind") or ""),
            id=str(item.get("id") or ""),
            summary=str(item.get("summary") or ""),
            at=str(item.get("at") or ""),
        )
        for item in value.get("evidence") or []
        if isinstance(item, dict)
    )
    return StoredRatchet(
        holding=bool(value.get("holding")),
        reason=str(value.get("reason") or ""),
        evidence=evidence,
        demoted_at=value.get("demoted_at") or None,
        promoted_at=_parse_dt(value.get("promoted_at")),
        promoted_by=value.get("promoted_by") or None,
        promotion_reason=value.get("promotion_reason") or None,
        tightened_approval=bool(value.get("tightened_approval")),
        graded=int(value.get("graded") or 0),
        disagreements=int(value.get("disagreements") or 0),
        rejections=int(value.get("rejections") or 0),
    )


def _persist_demotion(write: DemotionWrite) -> None:
    service = get_config_service(user_id="autonomy-ratchet")
    if write.enable_force_approval:
        saved = service.set_system_config(
            key=APPROVAL_CONFIG_KEY,
            value={"enabled": True},
            description="Force manual approval for all actions",
            config_type="approval",
            change_reason=f"Autonomy ratchet: {write.stored.reason}",
        )
        if not saved:
            raise RuntimeError("Could not force manual approval")
    if not _save_record(service, write.stored, change_reason=write.stored.reason):
        raise RuntimeError("Could not record the autonomy hold")


def _save_record(service, stored: StoredRatchet, *, change_reason: str) -> bool:
    return bool(
        service.set_system_config(
            key=RATCHET_KEY,
            value=_record(stored),
            description="Autonomy ratchet hold. The system sets it; a person clears it.",
            config_type="approval",
            change_reason=change_reason,
        )
    )


def _record(stored: StoredRatchet) -> dict:
    return {
        "holding": stored.holding,
        "reason": stored.reason,
        "evidence": [
            {"kind": item.kind, "id": item.id, "summary": item.summary, "at": item.at}
            for item in stored.evidence
        ],
        "demoted_at": stored.demoted_at,
        "promoted_at": (
            stored.promoted_at.isoformat(timespec="seconds")
            if stored.promoted_at
            else None
        ),
        "promoted_by": stored.promoted_by,
        "promotion_reason": stored.promotion_reason,
        "tightened_approval": stored.tightened_approval,
        "graded": stored.graded,
        "disagreements": stored.disagreements,
        "rejections": stored.rejections,
    }


def _parse_dt(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
