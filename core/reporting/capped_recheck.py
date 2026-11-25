"""Capped re-check of the latest Day 1 list (#5093).

One batch is a fixed draw. A person starts the first. The readiness sweep
may open the next one. This driver, on every API replica, claims the next
ticket under its own lock, commits the run id, then calls ``start_redecision``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Never, Optional

import httpx
from sqlalchemy import bindparam, func, or_, select, text, update
from sqlalchemy.orm import Session

from core.agents.projections import agent_route
from core.agents.queue import new_run_id
from core.llm.bifrost.costs import get_vk_quota
from core.llm.cost.budget import read_vk
from core.llm.providers.registry import get_registry
from core.reporting.day1 import DAY1_TRIGGERS, _clock_start
from core.reporting.enablement import disabled_module_ids
from core.reporting.history_sections import CANDIDATE
from core.storage.config_service import get_config_service
from core.storage.models import (
    CappedRecheckBatch,
    CappedRecheckTicket,
    HistoryImport,
    HistoryTicket,
    LLMProviderConfig,
    ReportRecord,
    SystemConfig,
    WorkflowRun,
)
from core.storage.unit_of_work import unit_of_work
from core.workflows.playbook_resolver import (
    INVESTIGATE_BUDGETS,
    _with_deployment_wall,
)
from core.workflows.redecision import start_redecision

logger = logging.getLogger(__name__)

# Not the Day 1 sweep's lock (4874). Claim and insert share this one.
LOCK = 5093
MODULE_ID = "history.capped_recheck"
CEILING_KEY = "day1_report.capped_recheck.ceiling"
TOP_N = 25
CONTROL_N = 25
MAX_IN_FLIGHT = 2
# Long enough for the starter's own vendor read. After it, a dead process
# leaves the ticket for another replica, still on the same run id.
DISPATCH_FOR = timedelta(minutes=2)
CEILING_REASON = "not re-checked: batch ceiling reached"
RESERVE_NOTE = "In-flight runs can still cross it because the gateway fails open."
_AGENT_TIMEOUT_S = 3.0


class Refused(Exception):
    """A start or resume that did not open or continue a batch."""


@dataclass
class Drawn:
    import_id: str
    source: str
    ticket_id: str
    group: str
    position: int
    random_key: str
    import_finished_at: Optional[datetime]
    reason: Optional[str] = None


@dataclass
class Draw:
    record_id: str
    post_install: int
    control_count: int
    tickets: list[Drawn]


@dataclass(frozen=True)
class Window:
    source: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Step:
    kind: Literal["start", "again", "blocked"]
    import_id: str = ""
    ticket_id: str = ""
    run_id: str = ""
    ticket_pk: int = 0
    dispatch_token: str = ""


def ceiling_runs() -> int:
    """The run ceiling. A missing row is 0. A failed read raises."""
    with unit_of_work() as session:
        return _ceiling(session)


def _ceiling(session: Session) -> int:
    row = session.get(SystemConfig, CEILING_KEY)
    if row is None or not isinstance(row.value, dict):
        return 0
    runs = row.value.get("runs")
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 0:
        return 0
    return runs


def set_ceiling(runs: int, user_id: str) -> bool:
    """Store the ceiling. ``platform.write`` is the caller's gate."""
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 0:
        raise ValueError("The ceiling must be a whole number, at least 0.")
    return get_config_service(user_id=user_id).set_system_config(
        key=CEILING_KEY,
        value={"runs": runs},
        description="How many re-decision runs one capped batch may start",
        config_type="report",
        change_reason=f"Capped re-check ceiling set to {runs}",
    )


def module_off() -> bool:
    """Off when the Day 1 module ``history.capped_recheck`` is switched off."""
    return MODULE_ID in disabled_module_ids()


def projection_of(batch_id: str) -> dict:
    with unit_of_work() as session:
        batch = session.get(CappedRecheckBatch, batch_id)
        if batch is None:
            raise Refused("No capped re-check yet.")
        tickets = list(
            session.scalars(
                select(CappedRecheckTicket)
                .where(CappedRecheckTicket.batch_id == batch_id)
                .order_by(CappedRecheckTicket.position)
            ).all()
        )
        body = _project(session, batch, tickets)
    body["reserve"] = _reserve()
    return body


def latest_projection() -> Optional[dict]:
    with unit_of_work() as session:
        batch_id = session.scalar(
            select(CappedRecheckBatch.batch_id).order_by(
                CappedRecheckBatch.created_at.desc()
            )
        )
    if batch_id is None:
        return None
    return projection_of(batch_id)


async def start_batch(actor_id: str) -> dict:
    """Draw a batch and fill the in-flight slots. Nothing is inserted on a refusal."""
    reason = await _refuse_before_open()
    if reason:
        raise Refused(reason)
    batch_id = await asyncio.to_thread(_open_person, actor_id)
    await drive()
    return await asyncio.to_thread(projection_of, batch_id)


async def resume_batch() -> dict:
    """Continue the open batch. A ceiling of 0 does not change it."""
    reason = await _refuse_before_open()
    if reason:
        raise Refused(reason)
    batch_id = await asyncio.to_thread(_mark_resumed)
    await drive()
    return await asyncio.to_thread(projection_of, batch_id)


async def drive() -> None:
    """Claim until a batch cannot start another run. Safe on every replica."""
    if module_off():
        return
    if not await asyncio.to_thread(_has_running):
        return
    agent_ok = await asyncio.to_thread(agent_ready)
    quota_ok = True if not agent_ok else await asyncio.to_thread(quota_allows)
    while True:
        step = await asyncio.to_thread(_claim, agent_ok, quota_ok)
        if step.kind == "blocked":
            return
        if step.kind == "again":
            continue
        if step.kind == "start":
            result = await start_redecision(
                step.import_id, step.ticket_id, run_id=step.run_id
            )
            await asyncio.to_thread(
                _record, step.ticket_pk, step.dispatch_token, result
            )
            if agent_ok:
                quota_ok = await asyncio.to_thread(quota_allows)
            continue
        _never(step.kind)


def consider(session: Session) -> None:
    """Open the next batch from the Day 1 sweep, or leave the table alone.

    The caller holds the readiness lock and commits. A failure here is logged
    and does not abort that sweep.
    """
    try:
        _consider(session)
    except Exception:
        logger.exception("capped re-check did not open a batch")


def agent_ready() -> bool:
    """True when ``GET`` of the agent ``/readyz`` answers 200."""
    try:
        with httpx.Client(timeout=_AGENT_TIMEOUT_S) as client:
            response = client.get(agent_route("/readyz"))
    except Exception as exc:
        logger.debug("capped re-check agent readyz failed: %s", exc)
        return False
    return response.status_code == 200


def quota_allows() -> bool:
    """No stored key means no reserve. A failed read, or a remainder under one
    investigation, does not start a run."""
    try:
        vk = read_vk()
    except Exception:
        logger.warning("capped re-check could not read the virtual key")
        return False
    if not vk:
        return True
    quota = get_vk_quota(vk)
    if quota is None:
        return False
    remainder = _remainder(quota)
    if remainder is None:
        return True
    return remainder >= float(INVESTIGATE_BUDGETS["max_cost_usd"])


def pricing_refusal() -> Optional[str]:
    resolved = get_registry().resolve_model_for_component("investigation")
    if resolved is None:
        return "No investigation model is assigned."
    provider_id, model_id = resolved
    with unit_of_work() as session:
        row = session.get(LLMProviderConfig, provider_id)
        provider_type = row.provider_type if row is not None else None
    if not provider_type:
        return "No investigation model is assigned."
    source = get_registry().get_pricing_source(model_id, provider_type)
    if source not in ("zero", "exact"):
        return "The investigation model's price is unknown."
    return None


async def _refuse_before_open() -> Optional[str]:
    if module_off():
        return "The capped re-check is off."
    try:
        runs = await asyncio.to_thread(ceiling_runs)
    except Exception:
        logger.exception("capped re-check ceiling could not be read")
        return "The re-check ceiling could not be read."
    if runs <= 0:
        return "The re-check ceiling is 0."
    pricing = await asyncio.to_thread(pricing_refusal)
    if pricing:
        return pricing
    if not await asyncio.to_thread(agent_ready):
        return "The agent layer is not reachable."
    return None


def _open_person(actor_id: str) -> str:
    with unit_of_work() as session:
        _lock(session)
        if module_off():
            raise Refused("The capped re-check is off.")
        if _ceiling(session) <= 0:
            raise Refused("The re-check ceiling is 0.")
        pricing = pricing_refusal()
        if pricing:
            raise Refused(pricing)
        if _blocking(session):
            raise Refused("A capped re-check is already open.")
        draw, why = plan(session)
        if draw is None or why:
            raise Refused(why or "No Day 1 re-check list to draw from.")
        batch = _insert(session, draw, "person", actor_id)
        session.flush()
        return batch.batch_id


def _mark_resumed() -> str:
    with unit_of_work() as session:
        _lock(session)
        batch = _open_batch(session)
        if batch is None:
            raise Refused("No capped re-check to resume.")
        if batch.status != "running":
            batch.status = "running"
            batch.resumed_at = datetime.now(timezone.utc)
            batch.quota_hold = False
            session.execute(
                update(CappedRecheckTicket)
                .where(
                    CappedRecheckTicket.batch_id == batch.batch_id,
                    CappedRecheckTicket.reason == CEILING_REASON,
                )
                .values(reason=None)
            )
        return batch.batch_id


def _consider(session: Session) -> None:
    _lock(session)
    if module_off() or not _person_started(session) or _blocking(session):
        return
    runs = _ceiling(session)
    if runs <= 0:
        return
    draw, why = plan(session)
    if draw is None or why or not draw.tickets:
        return
    if len(draw.tickets) > runs:
        logger.info(
            "capped re-check did not open: ceiling %s is below the draw of %s",
            runs,
            len(draw.tickets),
        )
        return
    pricing = pricing_refusal()
    if pricing:
        logger.info("capped re-check did not open: %s", pricing)
        return
    if not agent_ready():
        logger.info("capped re-check did not open: the agent layer is not reachable")
        return
    _insert(session, draw, "sweep", None)


def plan(session: Session) -> tuple[Optional[Draw], Optional[str]]:
    """The draw, or why there is nothing to store."""
    record = _latest_list(session)
    if record is None:
        return None, "No Day 1 re-check list to draw from."
    clock = _clock_start(session)
    if clock is None:
        return None, "The install clock is not set."
    clock_at = _utc(clock)
    section = _section(record)
    if section is None:
        return None, "No Day 1 re-check list to draw from."
    top, top_keys, top_refs = _top(section["entries"], clock_at)
    windows = _windows(section.get("windows"))
    control, post = _control_pool(session, windows, clock_at, top_keys, top_refs)
    drawn = _shuffle(top + control)
    _stamp_finished(session, drawn)
    result = Draw(record.record_id, post, len(control), drawn)
    if not drawn:
        return result, (
            "Nothing on the list closed before install. "
            f"{post} closures are at or after it and were not drawn."
        )
    return result, None


def _claim(agent_ok: bool, quota_ok: bool) -> Step:
    with unit_of_work() as session:
        _lock(session)
        batch = _running(session)
        if batch is None:
            return Step("blocked")
        owed = _owed(session, batch.batch_id)
        if owed is not None and owed.run_id:
            token = secrets.token_hex(8)
            owed.dispatch_token = token
            owed.dispatch_until = _dispatch_deadline()
            return Step(
                "start",
                owed.import_id,
                owed.ticket_id,
                owed.run_id,
                owed.id,
                token,
            )
        if _stalled(session, batch):
            batch.status = "interrupted"
            batch.quota_hold = False
            logger.info("capped re-check %s interrupted", batch.batch_id)
            return Step("blocked")
        nxt = _next_unclaimed(session, batch.batch_id)
        if nxt is None:
            if _in_flight(session, batch.batch_id) == 0:
                batch.status = "complete"
                batch.quota_hold = False
            return Step("blocked")
        if _started(session, batch.batch_id) >= _ceiling(session):
            _mark_ceiling(session, batch)
            return Step("blocked")
        why = _import_reason(session, nxt)
        if why:
            nxt.reason = why
            return Step("again")
        if not agent_ok:
            return Step("blocked")
        if not quota_ok:
            batch.quota_hold = True
            return Step("blocked")
        batch.quota_hold = False
        if _in_flight(session, batch.batch_id) >= MAX_IN_FLIGHT:
            return Step("blocked")
        run_id = new_run_id()
        token = secrets.token_hex(8)
        nxt.run_id = run_id
        nxt.dispatch_token = token
        nxt.dispatch_until = _dispatch_deadline()
        return Step("start", nxt.import_id, nxt.ticket_id, run_id, nxt.id, token)


def _record(ticket_pk: int, token: str, result: dict) -> None:
    """Settle the claim that still holds ``token``.

    A second replica may take over after the dispatch lease. Its token is the
    only one that may write. A failure does not cover a run that already has
    a ``workflow_runs`` row: the sweeper resumes that run, and this driver
    must not start another ticket in its place.
    """
    with unit_of_work() as session:
        _lock(session)
        ticket = session.get(CappedRecheckTicket, ticket_pk)
        if ticket is None or ticket.dispatch_token != token:
            return
        ticket.dispatch_until = None
        if result.get("success"):
            run_id = result.get("run_id")
            if isinstance(run_id, str) and run_id:
                ticket.run_id = run_id
            ticket.reason = None
            return
        if ticket.run_id and session.get(WorkflowRun, ticket.run_id) is not None:
            return
        error = result.get("error")
        ticket.reason = (
            error if isinstance(error, str) and error else "could not re-check"
        )


def _project(session: Session, batch: CappedRecheckBatch, tickets: list) -> dict:
    limit = _ceiling(session)
    reached = {"top": 0, "control": 0}
    used = 0
    could_not = 0
    for ticket in tickets:
        reason = ticket.reason or ""
        if reason.startswith("could not re-check"):
            could_not += 1
            continue
        if reason == CEILING_REASON:
            continue
        if not ticket.run_id and used >= limit:
            continue
        used += 1
        if ticket.ticket_group in reached:
            reached[ticket.ticket_group] += 1
    return {
        "batch_id": batch.batch_id,
        "status": batch.status,
        "tickets_chosen": len(tickets),
        "could_not_recheck": could_not,
        "ceiling_reaches": reached,
        "run_count": _started(session, batch.batch_id),
        "worst_case": _worst(),
        "post_install_closures": batch.post_install_closures,
        "control_count": batch.control_count,
        "control_short": batch.control_count < CONTROL_N,
    }


def _insert(
    session: Session, draw: Draw, origin: str, actor_id: Optional[str]
) -> CappedRecheckBatch:
    batch = CappedRecheckBatch(
        batch_id=str(uuid.uuid4()),
        origin=origin,
        actor_id=actor_id,
        record_id=draw.record_id,
        status="running",
        post_install_closures=draw.post_install,
        control_count=draw.control_count,
        quota_hold=False,
    )
    session.add(batch)
    for ticket in draw.tickets:
        session.add(
            CappedRecheckTicket(
                batch_id=batch.batch_id,
                import_id=ticket.import_id,
                source=ticket.source,
                ticket_id=ticket.ticket_id,
                ticket_group=ticket.group,
                position=ticket.position,
                random_key=ticket.random_key,
                import_finished_at=ticket.import_finished_at,
                reason=ticket.reason,
            )
        )
    return batch


def _latest_list(session: Session) -> Optional[ReportRecord]:
    row = session.execute(
        text("""
            SELECT record_id
            FROM report_records
            WHERE trigger IN :triggers
              AND jsonb_typeof(evidence -> 'history.recheck' -> 'entries') = 'array'
              AND jsonb_array_length(evidence -> 'history.recheck' -> 'entries') > 0
            ORDER BY recorded_at DESC
            LIMIT 1
            """).bindparams(bindparam("triggers", expanding=True)),
        {"triggers": list(DAY1_TRIGGERS)},
    ).first()
    if row is None:
        return None
    return session.get(ReportRecord, row.record_id)


def _section(record: ReportRecord) -> Optional[dict]:
    evidence = record.evidence if isinstance(record.evidence, dict) else {}
    section = evidence.get("history.recheck")
    if not isinstance(section, dict):
        return None
    entries = section.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    return section


def _top(entries: list, clock_at: datetime) -> tuple[list[Drawn], set, set]:
    """The first 25 entries closed before the clock, in the stored order.

    An entry that is closed before the clock and cannot be re-checked still
    takes one of those 25 places. A later entry does not move up into it.
    """
    chosen: list[Drawn] = []
    keys: set[tuple[str, str]] = set()
    refs: set[tuple[str, str]] = set()
    taken = 0
    for entry in entries:
        if taken >= TOP_N:
            break
        if not isinstance(entry, dict):
            continue
        closed = _parse_time(entry.get("closed_at"))
        if closed is None or closed >= clock_at:
            continue
        taken += 1
        parsed = _entry(entry)
        if parsed is None:
            ref = entry.get("ref") if isinstance(entry.get("ref"), dict) else {}
            import_id = ref.get("import_id")
            source = entry.get("source")
            if not isinstance(import_id, str):
                import_id = ""
            if not isinstance(source, str) or not source.strip():
                source = "file"
            chosen.append(
                Drawn(
                    import_id,
                    source.strip(),
                    "",
                    "top",
                    0,
                    "",
                    None,
                    "could not re-check",
                )
            )
            continue
        import_id, source, ticket_id, _closed = parsed
        key = (source, ticket_id)
        if key in keys:
            continue
        keys.add(key)
        refs.add((import_id, ticket_id))
        chosen.append(Drawn(import_id, source, ticket_id, "top", 0, "", None))
    return chosen, keys, refs


def _entry(entry: object) -> Optional[tuple[str, str, str, Optional[datetime]]]:
    if not isinstance(entry, dict):
        return None
    ref = entry.get("ref")
    if not isinstance(ref, dict):
        return None
    import_id = ref.get("import_id")
    ticket_id = ref.get("ticket_id")
    source = entry.get("source")
    if not isinstance(import_id, str) or not import_id:
        return None
    if not isinstance(ticket_id, str) or not ticket_id:
        return None
    if not isinstance(source, str) or not source.strip():
        source = "file"
    return import_id, source.strip(), ticket_id, _parse_time(entry.get("closed_at"))


def _windows(raw: object) -> list[Window]:
    found: list[Window] = []
    if not isinstance(raw, list):
        return found
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        start = _parse_time(item.get("start"))
        end = _parse_time(item.get("end"))
        if not isinstance(source, str) or not source or start is None or end is None:
            continue
        found.append(Window(source, start, end))
    return found


def _control_pool(
    session: Session,
    windows: list[Window],
    clock_at: datetime,
    top_keys: set,
    top_refs: set,
) -> tuple[list[Drawn], int]:
    if not windows:
        return [], 0
    low = min(window.start for window in windows)
    high = max(window.end for window in windows)
    rows = session.scalars(
        select(HistoryTicket).where(
            HistoryTicket.closed_at >= low,
            HistoryTicket.closed_at <= high,
        )
    ).all()
    post: set[tuple[str, str]] = set()
    candidates: list[tuple[str, HistoryTicket]] = []
    for ticket in rows:
        source = (ticket.source_system or "file").strip() or "file"
        closed = _utc(ticket.closed_at)
        if not any(
            window.source == source and window.start <= closed <= window.end
            for window in windows
        ):
            continue
        if closed >= clock_at:
            post.add(
                (
                    source,
                    ticket.ticket_id or f"{ticket.import_id}:{ticket.position}",
                )
            )
            continue
        if ticket.category not in CANDIDATE or not ticket.ticket_id:
            continue
        if (source, ticket.ticket_id) in top_keys:
            continue
        if (ticket.import_id, ticket.ticket_id) in top_refs:
            continue
        candidates.append((source, ticket))
    finished = _finished_map(
        session, {ticket.import_id for _source, ticket in candidates}
    )
    best: dict[tuple[str, str], HistoryTicket] = {}
    best_source: dict[tuple[str, str], str] = {}
    for source, ticket in candidates:
        key = (source, ticket.ticket_id or "")
        current = best.get(key)
        if current is None or _newer(ticket, current, finished):
            best[key] = ticket
            best_source[key] = source
    pool = [
        Drawn(
            ticket.import_id,
            best_source[key],
            ticket.ticket_id or "",
            "control",
            0,
            "",
            None,
        )
        for key, ticket in best.items()
        if ticket.ticket_id
    ]
    if len(pool) > CONTROL_N:
        pool = random.sample(pool, CONTROL_N)
    return pool, len(post)


def _newer(ticket: HistoryTicket, other: HistoryTicket, finished: dict) -> bool:
    left = finished.get(ticket.import_id)
    right = finished.get(other.import_id)
    left_key = (
        _utc(left) if left is not None else _utc(ticket.closed_at),
        ticket.position,
    )
    right_key = (
        _utc(right) if right is not None else _utc(other.closed_at),
        other.position,
    )
    return left_key >= right_key


def _shuffle(rows: list[Drawn]) -> list[Drawn]:
    for row in rows:
        row.random_key = secrets.token_hex(16)
    rows.sort(key=lambda row: row.random_key)
    for index, row in enumerate(rows):
        row.position = index
    return rows


def _stamp_finished(session: Session, rows: list[Drawn]) -> None:
    finished = _finished_map(session, {row.import_id for row in rows})
    for row in rows:
        row.import_finished_at = finished.get(row.import_id)


def _finished_map(session: Session, import_ids: set[str]) -> dict:
    if not import_ids:
        return {}
    found = session.scalars(
        select(HistoryImport).where(HistoryImport.id.in_(import_ids))
    ).all()
    return {row.id: row.finished_at for row in found}


def _import_reason(session: Session, ticket: CappedRecheckTicket) -> Optional[str]:
    record = session.get(HistoryImport, ticket.import_id)
    if record is None:
        return "could not re-check: import is gone"
    current = record.finished_at
    stored = ticket.import_finished_at
    if current is not None and (stored is None or _utc(current) > _utc(stored)):
        return "could not re-check: import was re-derived"
    return None


def _mark_ceiling(session: Session, batch: CappedRecheckBatch) -> None:
    chosen = int(
        session.scalar(
            select(func.count())
            .select_from(CappedRecheckTicket)
            .where(CappedRecheckTicket.batch_id == batch.batch_id)
        )
        or 0
    )
    started = _started(session, batch.batch_id)
    session.execute(
        update(CappedRecheckTicket)
        .where(
            CappedRecheckTicket.batch_id == batch.batch_id,
            CappedRecheckTicket.reason.is_(None),
            CappedRecheckTicket.run_id.is_(None),
        )
        .values(reason=CEILING_REASON)
    )
    batch.status = f"stopped: ceiling, {started} of {chosen}"
    batch.quota_hold = False
    logger.info("capped re-check %s %s", batch.batch_id, batch.status)


def _stalled(session: Session, batch: CappedRecheckBatch) -> bool:
    wall = timedelta(
        milliseconds=_with_deployment_wall(dict(INVESTIGATE_BUDGETS))["max_wall_ms"]
    )
    now = datetime.now(timezone.utc)
    floor = _utc(batch.resumed_at) if batch.resumed_at is not None else None
    rows = session.execute(
        text("""
            SELECT w.started_at AS started_at,
                   (SELECT max(e.ts) FROM agent_events e
                     WHERE e.run_id = CAST(t.run_id AS uuid)) AS last_ts
            FROM capped_recheck_tickets t
            JOIN workflow_runs w ON w.run_id = t.run_id
            WHERE t.batch_id = :batch
              AND t.reason IS NULL
              AND w.status IN ('running', 'paused')
            """),
        {"batch": batch.batch_id},
    ).all()
    for row in rows:
        last = row.last_ts or row.started_at
        if last is None:
            continue
        anchor = _utc(last)
        if floor is not None and floor > anchor:
            anchor = floor
        if now - anchor >= wall:
            return True
    return False


def _started(session: Session, batch_id: str) -> int:
    found = session.execute(
        text("""
            SELECT count(*)
            FROM capped_recheck_tickets t
            WHERE t.batch_id = :batch
              AND (
                EXISTS (SELECT 1 FROM workflow_runs w WHERE w.run_id = t.run_id)
                OR (t.run_id IS NOT NULL AND t.reason IS NULL)
              )
            """),
        {"batch": batch_id},
    ).scalar()
    return int(found or 0)


def _in_flight(session: Session, batch_id: str) -> int:
    found = session.execute(
        text("""
            SELECT count(*)
            FROM capped_recheck_tickets t
            LEFT JOIN workflow_runs w ON w.run_id = t.run_id
            WHERE t.batch_id = :batch
              AND t.run_id IS NOT NULL
              AND t.reason IS NULL
              AND (w.run_id IS NULL OR w.status IN ('running', 'paused'))
            """),
        {"batch": batch_id},
    ).scalar()
    return int(found or 0)


def _owed(session: Session, batch_id: str) -> Optional[CappedRecheckTicket]:
    row = session.execute(
        text("""
            SELECT t.id
            FROM capped_recheck_tickets t
            WHERE t.batch_id = :batch
              AND t.reason IS NULL
              AND t.run_id IS NOT NULL
              AND (t.dispatch_until IS NULL OR t.dispatch_until < :now)
              AND NOT EXISTS (
                  SELECT 1 FROM workflow_runs w WHERE w.run_id = t.run_id
              )
            ORDER BY t.position
            LIMIT 1
            """),
        {"batch": batch_id, "now": datetime.now(timezone.utc)},
    ).first()
    if row is None:
        return None
    return session.get(CappedRecheckTicket, row.id)


def _next_unclaimed(session: Session, batch_id: str) -> Optional[CappedRecheckTicket]:
    return session.scalars(
        select(CappedRecheckTicket)
        .where(
            CappedRecheckTicket.batch_id == batch_id,
            CappedRecheckTicket.reason.is_(None),
            CappedRecheckTicket.run_id.is_(None),
        )
        .order_by(CappedRecheckTicket.position)
        .limit(1)
        .with_for_update()
    ).first()


def _running(session: Session) -> Optional[CappedRecheckBatch]:
    return session.scalars(
        select(CappedRecheckBatch)
        .where(CappedRecheckBatch.status == "running")
        .order_by(CappedRecheckBatch.created_at)
    ).first()


def _has_running() -> bool:
    with unit_of_work() as session:
        return _running(session) is not None


def _open_batch(session: Session) -> Optional[CappedRecheckBatch]:
    return session.scalars(
        select(CappedRecheckBatch)
        .where(_open_clause())
        .order_by(CappedRecheckBatch.created_at.desc())
    ).first()


def _blocking(session: Session) -> bool:
    return _open_batch(session) is not None


def _person_started(session: Session) -> bool:
    found = session.scalar(
        select(CappedRecheckBatch.batch_id)
        .where(CappedRecheckBatch.origin == "person")
        .limit(1)
    )
    return found is not None


def _open_clause():
    return or_(
        CappedRecheckBatch.status.in_(("running", "interrupted")),
        CappedRecheckBatch.status.like("stopped:%"),
    )


def _lock(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})


def _dispatch_deadline() -> datetime:
    return datetime.now(timezone.utc) + DISPATCH_FOR


def _worst() -> dict:
    budgets = _with_deployment_wall(dict(INVESTIGATE_BUDGETS))
    return {
        "calls": int(budgets["max_calls"]),
        "dollars": float(budgets["max_cost_usd"]),
        "wall_ms": int(budgets["max_wall_ms"]),
    }


def _reserve() -> Optional[dict]:
    try:
        vk = read_vk()
    except Exception:
        logger.warning("capped re-check could not read the virtual key")
        return None
    if not vk:
        return None
    quota = get_vk_quota(vk)
    return {
        "remainder": None if quota is None else _remainder(quota),
        "note": RESERVE_NOTE,
    }


def _remainder(quota: dict) -> Optional[float]:
    budgets = quota.get("budgets")
    if not isinstance(budgets, list):
        return None
    found: list[float] = []
    for budget in budgets:
        if not isinstance(budget, dict):
            continue
        limit = budget.get("max_limit")
        used = budget.get("current_usage")
        if isinstance(limit, bool) or isinstance(used, bool):
            continue
        if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
            found.append(float(limit) - float(used))
    if not found:
        return None
    return min(found)


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_time(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _never(kind: Never) -> None:
    raise RuntimeError(f"unexpected claim step {kind}")
