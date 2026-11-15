"""Replay person-determined cases on a candidate investigation model (#4389).

The row is the latest replay only. This module selects the cases, drives the
agent one run at a time, and writes the row as each case finishes. It does not
change the model assignment.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.agents.projections import _headers, agent_route
from core.cases.closure import closed_by_person, closure_says_real
from core.reporting.analytics_service import _NOT_REAL, _REAL, _wilson
from core.response.ceiling import AGREEMENT_WINDOW
from core.storage.models import InvestigationReplay, LLMProviderConfig
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow
from core.workflows.playbook_resolver import DEFAULT_SPEND

logger = logging.getLogger(__name__)

# The same wall the agent resets an investigate run to
# (services/agent/core/spec.ts DEFAULT_BUDGETS.max_wall_ms).
WALL = timedelta(milliseconds=DEFAULT_SPEND["max_wall_ms"])
WALL_S = DEFAULT_SPEND["max_wall_ms"] / 1000
STALE = "no progress for one investigation wall budget"
CONCURRENCY = 2
_ROW_ID = 1

# The agreement query's lateral, without the sampled_at arm: closed_at only,
# and the run id the terminal belongs to.
_CASES = text("""
    SELECT ci.case_id, ci.closure_category, ci.close_channel, ci.evidence_opened,
           picked.run_id
    FROM case_closure_info ci
    JOIN cases c ON c.case_id = ci.case_id AND c.status = 'closed'
    JOIN LATERAL (
        SELECT r.run_id
        FROM workflow_runs r
        JOIN agent_events e
          ON e.run_id = CAST(substring(r.run_id FROM
                 '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
             ) AS uuid)
        WHERE r.trigger_context->>'case_id' = ci.case_id
          AND r.deleted_at IS NULL
          AND e.kind = 'terminal'
          AND e.run_kind = 'investigate'
          AND coalesce(e.payload->>'disposition', '') <> ''
          AND e.ts <= (ci.closed_at AT TIME ZONE 'UTC')
        ORDER BY e.ts DESC, e.seq DESC
        LIMIT 1
    ) picked ON true
    WHERE ci.closed_at >= :start AND ci.closed_at <= :end
    ORDER BY ci.case_id
    """)


@dataclass
class Selected:
    case_id: str
    run_id: str
    closure_category: str
    says_real: bool
    evidence_opened: Optional[bool]


@dataclass
class Tally:
    total: int
    replayed: int = 0
    skipped: int = 0
    false_closes: int = 0
    agreed: int = 0
    disagreed: int = 0
    abstained: int = 0
    cost_usd: float = 0.0
    false_close: list = field(default_factory=list)


def _provider(db: Session, provider_id: str) -> LLMProviderConfig:
    provider = db.get(LLMProviderConfig, provider_id)
    if provider is None:
        raise HTTPException(
            status_code=400, detail=f"provider not found: {provider_id}"
        )
    if not provider.is_active:
        raise HTTPException(
            status_code=400, detail=f"provider {provider_id} is not active"
        )
    return provider


def _locked(db: Session) -> Optional[InvestigationReplay]:
    return (
        db.query(InvestigationReplay)
        .filter(InvestigationReplay.id == _ROW_ID)
        .with_for_update()
        .one_or_none()
    )


def _stale(row: InvestigationReplay, now: datetime) -> bool:
    return row.status == "running" and now - row.updated_at >= WALL


def _fail_stale(row: InvestigationReplay, now: datetime) -> None:
    row.status = "failed"
    row.reason = STALE
    row.updated_at = now


def select_cases(db: Session, now: datetime) -> list[Selected]:
    """Person-determined closes in the agreement window, with the run agreement picks."""
    rows = db.execute(_CASES, {"start": now - AGREEMENT_WINDOW, "end": now}).all()
    chosen: list[Selected] = []
    for row in rows:
        if not closed_by_person(row.close_channel):
            continue
        says_real = closure_says_real(row.closure_category)
        if says_real is None:
            continue
        chosen.append(
            Selected(
                case_id=row.case_id,
                run_id=str(row.run_id),
                closure_category=row.closure_category,
                says_real=says_real,
                evidence_opened=row.evidence_opened,
            )
        )
    return chosen


def _rates(tally: Tally) -> dict[str, Any]:
    false_rate = None
    false_low = None
    false_high = None
    if tally.replayed:
        false_rate = round(tally.false_closes / tally.replayed, 4)
        bounds = _wilson(tally.false_closes, tally.replayed)
        if bounds is not None:
            false_low, false_high = bounds
    scored = tally.agreed + tally.disagreed
    agreement = None
    agree_low = None
    agree_high = None
    if scored:
        agreement = round(tally.agreed / scored, 4)
        bounds = _wilson(tally.agreed, scored)
        if bounds is not None:
            agree_low, agree_high = bounds
    return {
        "replayed": tally.replayed,
        "skipped": tally.skipped,
        "false_closes": tally.false_closes,
        "false_close_rate": false_rate,
        "false_close_wilson_low": false_low,
        "false_close_wilson_high": false_high,
        "agreed": tally.agreed,
        "disagreed": tally.disagreed,
        "abstained": tally.abstained,
        "agreement_rate": agreement,
        "agreement_wilson_low": agree_low,
        "agreement_wilson_high": agree_high,
        "cost_usd": round(tally.cost_usd, 6),
        "false_close": list(tally.false_close),
    }


def payload_of(row: InvestigationReplay) -> dict[str, Any]:
    return {
        "status": row.status,
        "reason": row.reason,
        "provider_id": row.provider_id,
        "model_id": row.model_id,
        "started_by": row.started_by,
        "started_at": row.started_at.isoformat(),
        "total": row.total,
        "replayed": row.replayed,
        "skipped": row.skipped,
        "false_closes": row.false_closes,
        "false_close_rate": row.false_close_rate,
        "false_close_wilson_low": row.false_close_wilson_low,
        "false_close_wilson_high": row.false_close_wilson_high,
        "agreed": row.agreed,
        "disagreed": row.disagreed,
        "abstained": row.abstained,
        "agreement_rate": row.agreement_rate,
        "agreement_wilson_low": row.agreement_wilson_low,
        "agreement_wilson_high": row.agreement_wilson_high,
        "cost_usd": row.cost_usd,
        "false_close": row.false_close or [],
    }


def _apply(
    row: InvestigationReplay,
    tally: Tally,
    status: str,
    reason: Optional[str],
    now: datetime,
) -> None:
    rated = _rates(tally)
    row.status = status
    row.reason = reason
    row.updated_at = now
    for key, value in rated.items():
        setattr(row, key, value)


def begin(
    db: Session, provider_id: str, model_id: str, started_by: str
) -> tuple[dict[str, Any], list[Selected], str, datetime]:
    """Validate, claim the row, and return the public payload plus the cases to run."""
    provider = _provider(db, provider_id)
    now = utcnow()
    row = _locked(db)
    if row is not None and row.status == "running":
        if not _stale(row, now):
            raise HTTPException(status_code=409, detail="a replay is already running")
        _fail_stale(row, now)
    cases = select_cases(db, now)
    tally = Tally(total=len(cases))
    if row is None:
        row = InvestigationReplay(
            id=_ROW_ID,
            provider_id=provider_id,
            model_id=model_id,
            started_by=started_by,
            started_at=now,
            total=tally.total,
            status="running",
            reason=None,
            updated_at=now,
            replayed=0,
            skipped=0,
            false_closes=0,
            agreed=0,
            disagreed=0,
            abstained=0,
            cost_usd=0,
            false_close=[],
        )
        db.add(row)
    else:
        row.provider_id = provider_id
        row.model_id = model_id
        row.started_by = started_by
        row.started_at = now
        row.total = tally.total
        row.status = "running"
        row.reason = None
        _apply(row, tally, "running", None, now)
    db.flush()
    return payload_of(row), cases, provider.provider_type, row.started_at


def read(db: Session) -> dict[str, Any]:
    now = utcnow()
    row = _locked(db)
    if row is None:
        raise HTTPException(status_code=404, detail="no investigation replay")
    if _stale(row, now):
        _fail_stale(row, now)
        db.flush()
    return payload_of(row)


def _note(tally: Tally, case: Selected, body: dict[str, Any]) -> None:
    if "skipped" in body:
        tally.skipped += 1
        return
    word = body.get("disposition")
    disposition = word.strip().lower() if isinstance(word, str) else ""
    cost = body.get("cost_usd")
    if isinstance(cost, (int, float)):
        tally.cost_usd += float(cost)
    tally.replayed += 1
    took_side = disposition in _REAL or disposition in _NOT_REAL
    if not took_side:
        tally.abstained += 1
    elif (disposition in _REAL) == case.says_real:
        tally.agreed += 1
    else:
        tally.disagreed += 1
    if case.says_real and disposition in _NOT_REAL:
        tally.false_closes += 1
        tally.false_close.append(
            {
                "case_id": case.case_id,
                "closure_category": case.closure_category,
                "disposition": disposition,
                "evidence_opened": case.evidence_opened,
            }
        )


def _save(
    tally: Tally, status: str, reason: Optional[str], started_at: datetime
) -> bool:
    """Write the snapshot. False when this drive no longer owns the running row."""
    with unit_of_work() as db:
        row = _locked(db)
        if row is None or row.status != "running" or row.started_at != started_at:
            return False
        _apply(row, tally, status, reason, utcnow())
        return True


async def _one(
    client: httpx.AsyncClient, case: Selected, provider: str, model: str
) -> dict[str, Any]:
    try:
        response = await client.post(
            agent_route("/investigation-replay"),
            json={"run_id": case.run_id, "provider": provider, "model": model},
            headers=_headers(),
            timeout=WALL_S,
        )
    except Exception as exc:  # noqa: BLE001 -- one case failing is an abstention
        logger.warning("replay case %s did not finish: %s", case.case_id, exc)
        return {"disposition": None, "cost_usd": 0}
    if response.status_code != 200:
        logger.warning("replay case %s answered %s", case.case_id, response.status_code)
        return {"disposition": None, "cost_usd": 0}
    try:
        body = response.json()
    except ValueError:
        return {"disposition": None, "cost_usd": 0}
    return body if isinstance(body, dict) else {"disposition": None, "cost_usd": 0}


async def drive(
    client: httpx.AsyncClient,
    cases: list[Selected],
    provider: str,
    model: str,
    started_at: datetime,
) -> None:
    """Run the cases at concurrency 2 and fold each one into the row."""
    tally = Tally(total=len(cases))
    gate = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    stopped = False

    async def run(case: Selected) -> None:
        nonlocal stopped
        if stopped:
            return
        async with gate:
            if stopped:
                return
            body = await _one(client, case, provider, model)
        async with lock:
            if stopped:
                return
            _note(tally, case, body)
            if not _save(tally, "running", None, started_at):
                stopped = True

    try:
        await asyncio.gather(*(run(case) for case in cases))
    except Exception as exc:  # noqa: BLE001 -- the row has to leave running
        logger.exception("investigation replay failed")
        _save(tally, "failed", str(exc), started_at)
        return
    if not stopped:
        _save(tally, "succeeded", None, started_at)
