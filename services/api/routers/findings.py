"""Findings — unversioned operator/console endpoints.

The frozen read and record-update surface lives in ``core/api/v1/findings_router.py``
and is mounted at both ``/api/v1/findings`` and (for now) ``/api/findings``.
This module keeps the routes that are *not* part of that contract: AI
enrichment generation, the destructive wipe, and an analyst's decision on one
finding. They remain under ``/api/findings`` only.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.audit import trail
from core.auth.permissions import deny_unless, permission_gate
from core.cases import case_trail
from core.cases.assignee import resolve_assignee
from core.cases.case_sla_service import case_title, open_case, open_case_holding
from core.cases.closure import ClosedByKind
from core.findings.enrichment import (
    FindingNotFound,
    NoProviderConfigured,
    ProviderUnavailable,
    enrich,
    is_generated_analysis,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.config_service import get_config_service
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import Finding, User
from core.time import utcnow
from services.api.middleware.auth import get_current_user
from services.daemon.orchestrator import insert_intake_trigger, intake_severity_band

router = APIRouter()
_FINDINGS_WRITE = [permission_gate("findings.write")]
router.dependencies.append(permission_gate("findings.read"))

ROUTER_META = RouterMeta(
    prefix="/api/findings",
    tags=["findings"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)
data_service = DatabaseDataService()


Outcome = Literal["noise", "triage", "take_case"]


class FindingDecision(BaseModel):
    outcome: Outcome
    reason: Optional[str] = None
    request_id: str = Field(
        min_length=1,
        max_length=200,
        description="Minted once per decision. Sending it again replays the first answer.",
    )


class FindingDecisionResponse(BaseModel):
    finding_id: str
    outcome: Outcome
    case_id: Optional[str] = None
    already_queued: Optional[bool] = None
    replayed: bool = False


class NoiseMarkResponse(BaseModel):
    finding_id: str
    noise_marked_at: Optional[datetime] = None
    noise_marked_by: Optional[str] = None
    noise_marked_by_kind: Optional[str] = None
    noise_reason: Optional[str] = None


def _finding_or_404(session: UnitOfWorkSession, finding_id: str) -> Finding:
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@router.post("/{finding_id}/enrich", dependencies=_FINDINGS_WRITE)
async def get_or_generate_enrichment(
    finding_id: str, force_regenerate: bool = Query(False)
):
    """
    Get or generate AI enrichment for a finding.

    Returns cached enrichment if present, otherwise generates, caches, and
    returns new enrichment from the configured reporting model.

    Args:
        finding_id: The finding ID to enrich
        force_regenerate: Force regeneration even if enrichment exists

    Returns:
        AI enrichment data with threat analysis, impact, recommendations, etc.
    """
    import asyncio

    # The data layer is synchronous SQLAlchemy, so keep it off the event loop —
    # this handler stays async because it awaits the LLM.
    finding = await asyncio.to_thread(data_service.get_finding, finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    # Caching is HTTP policy — the shared enrich() seam deliberately doesn't do
    # this check, since force_regenerate is a query param and the daemon has its
    # own freshness rules. Triage-only keys don't count as a cached analysis.
    existing_enrichment = finding.get("ai_enrichment")
    if is_generated_analysis(existing_enrichment) and not force_regenerate:
        logger.info(f"Returning cached enrichment for {finding_id}")
        return {
            "finding_id": finding_id,
            "cached": True,
            "enrichment": existing_enrichment,
        }

    try:
        # Pass the path param, not the id off the row we just read — it's the
        # authoritative write target, exactly as the pre-extraction handler did.
        enrichment = await enrich(
            finding, finding_id=finding_id, data_service=data_service
        )
    except FindingNotFound:
        raise HTTPException(status_code=404, detail="Finding not found")
    except NoProviderConfigured:
        # 503 with the structured payload the chat drawer matches on to render
        # a "Configure a provider" CTA instead of a generic error bubble.
        from services.api.routers.claude import NO_PROVIDER_DETAIL

        raise HTTPException(status_code=503, detail=NO_PROVIDER_DETAIL)
    except ProviderUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {"finding_id": finding_id, "cached": False, "enrichment": enrichment}


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


def _mark_noise(
    session: Session, finding: Finding, user: User, reason: Optional[str]
) -> dict[str, Any]:
    finding.noise_marked_at = utcnow()
    finding.noise_marked_by = user.user_id
    finding.noise_marked_by_kind = ClosedByKind.ANALYST.value
    finding.noise_reason = reason
    return {}


def _send_to_triage(
    session: Session, finding: Finding, user: User, reason: Optional[str]
) -> dict[str, Any]:
    trigger_id = insert_intake_trigger(
        kind="detection",
        finding_id=finding.finding_id,
        priority=intake_severity_band("detection", finding_severity=finding.severity),
        session=session,
    )
    return {"already_queued": trigger_id is None}


def _take_case(
    session: Session, finding: Finding, user: User, reason: Optional[str]
) -> dict[str, Any]:
    owner = resolve_assignee(session, user.username)
    case_id = open_case_holding(finding.finding_id, session=session)
    if case_id is None:
        band = intake_severity_band("detection", finding_severity=finding.severity)
        case = open_case(
            data_service,
            title=case_title(
                finding.title or finding.description or finding.finding_id
            ),
            finding_ids=[finding.finding_id],
            priority="medium" if band == "unknown" else band,
            description=finding.description or "",
            session=session,
        )
        if not case:
            raise HTTPException(status_code=500, detail="Failed to create case")
        case_id = case["case_id"]
    if not data_service.update_case(case_id, session=session, assignee=owner):
        raise HTTPException(status_code=500, detail="Failed to assign case")
    return {"case_id": case_id}


@dataclass(frozen=True)
class _Decision:
    # Held beyond the route's findings.write, all checked before the first write.
    permissions: tuple[str, ...]
    apply: Callable[[Session, Finding, User, Optional[str]], dict[str, Any]]


_DECISIONS: dict[Outcome, _Decision] = {
    "noise": _Decision((), _mark_noise),
    "triage": _Decision((), _send_to_triage),
    "take_case": _Decision(("cases.write", "cases.assign"), _take_case),
}


@router.post(
    "/{finding_id}/decide",
    response_model=FindingDecisionResponse,
    dependencies=_FINDINGS_WRITE,
)
def decide_finding(
    finding_id: str,
    body: FindingDecision,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """One analyst decision on one finding, in one transaction with its trail entry.

    A repeated ``request_id`` answers with the outcome on record and writes nothing.
    """
    decision = _DECISIONS[body.outcome]
    for permission in decision.permissions:
        deny_unless(current_user.user_id, permission, session)

    # A duplicate waits here for the first to commit, then finds it on the trail.
    # NO KEY so linking this finding to a case elsewhere does not wait on it.
    finding = session.get(Finding, finding_id, with_for_update={"key_share": True})
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    recorded = trail.recorded_decision(session, finding_id, body.request_id)
    if recorded is not None:
        return FindingDecisionResponse(
            finding_id=finding_id,
            outcome=recorded["decision"],
            case_id=recorded.get("case_id"),
            already_queued=recorded.get("already_queued"),
            replayed=True,
        )

    reason = (body.reason or "").strip() or None
    result = decision.apply(session, finding, current_user, reason)
    case_trail.append_or_legacy(
        session,
        action=trail.ACTION_FINDING_DECIDED,
        signed_in=current_user.username,
        case_id=result.get("case_id"),
        operator_text=reason,
        ids=lambda: {
            trail.OBJECT: finding_id,
            "finding_id": finding_id,
            "decision": body.outcome,
            "request_id": body.request_id,
            "already_queued": result.get("already_queued"),
        },
    )
    return FindingDecisionResponse(
        finding_id=finding_id, outcome=body.outcome, **result
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
    """Clear the noise mark. The actor is the signed-in user; the columns go back to null.

    Clearing a mark that is not there changes nothing, so it records nothing.
    """
    finding = _finding_or_404(session, finding_id)
    if finding.noise_marked_at is not None:
        logger.info("cleared noise mark on %s by %s", finding_id, current_user.user_id)
        finding.noise_marked_at = None
        finding.noise_marked_by = None
        finding.noise_marked_by_kind = None
        finding.noise_reason = None
        case_trail.append_or_legacy(
            session,
            action=trail.ACTION_FINDING_NOISE_CLEARED,
            signed_in=current_user.username,
            case_id=None,
            ids=lambda: {trail.OBJECT: finding_id, "finding_id": finding_id},
        )
    return NoiseMarkResponse(finding_id=finding.finding_id)
