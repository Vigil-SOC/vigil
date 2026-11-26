"""The Day 1 module that freezes a capped re-check (#5094).

``day1.build`` stores the body on the report record. Ticket text, a re-derived
import, and whether the close was approximate are filled in when the report
is opened. The driver, the draw and the ceiling stay in ``capped_recheck``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from core.agents.projections import PROJECTION_READ_CONCURRENCY, read_spend
from core.auth.permissions import missing_permission
from core.detections.inventory.accurate import MIN_JUDGED
from core.llm.providers.registry import _SELF_HOSTED_TYPES, get_registry
from core.memory.imported_history import list_imports
from core.reporting.analytics_service import _NOT_REAL, _REAL, _wilson
from core.reporting.history_sections import _HOW_ADD, _row
from core.storage.models import (
    CappedRecheckBatch,
    CappedRecheckTicket,
    HistoryImport,
    HistoryTicket,
    LLMProviderConfig,
    WorkflowRun,
)
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# Same id as ``capped_recheck.MODULE_ID``. Imported there only inside a
# function: that module imports ``day1``, which imports this one.
MODULE_ID = "history.capped_recheck"
_READ_FAILED = "not checked: could not read the capped re-check."
_AWAITING = "Run the Day 1 Report."
_TEXT_NEEDS = "Those fields need automation.read."
_REMOVED = "removed with its import"
_ERASED = "evidence erased with its import"
_GROUPS = ("top", "control")
_CLOSURE_NOT_REAL = frozenset({"benign", "false_positive"})
# One could-not row. An unfinished terminal stays in the group's count.
_DIRECTIONS = (
    "not_real_not_real",
    "not_real_real",
    "undetermined_real",
    "undetermined_not_real",
    "not_real_undetermined",
    "undetermined_undetermined",
    "could_not",
)


def produce() -> dict[str, Any]:
    """The body stored at ``evidence["history.capped_recheck"]``."""
    with unit_of_work() as session:
        return _produce(session)


def present(
    session: Session,
    evidence: dict[str, Any],
    modules: list[str],
    hidden: set[str],
    recorded_at: Any,
    user_id: str,
) -> Optional[dict[str, Any]]:
    """The section for this reader. Withheld when they lack ``findings.read``.

    Title and entities need ``automation.read``. A later import finish and a
    deleted import are read through ``_row``. The two counts are not stored.
    """
    if MODULE_ID not in modules or MODULE_ID in hidden:
        return None
    stored = evidence.get(MODULE_ID)
    if not isinstance(stored, dict):
        stored = {"not_checked": _READ_FAILED}
    text_ok = missing_permission(user_id, "automation.read", session) is None
    shown: dict[str, Any] = {
        "not_checked": stored.get("not_checked"),
        "how": stored.get("how"),
        "awaiting_list": bool(stored.get("awaiting_list")),
        "none_run": bool(stored.get("none_run")),
        "open_only": bool(stored.get("open_only")),
        "finished": None,
        "stopped": None,
    }
    for key in ("finished", "stopped"):
        batch = stored.get(key)
        if isinstance(batch, dict):
            shown[key] = _present_batch(session, batch, recorded_at, text_ok)
    return shown


def _produce(session: Session) -> dict[str, Any]:
    if not list_imports(session):
        return {"not_checked": "not checked: no history imported", "how": _HOW_ADD}
    # The same read the driver uses to find a list worth drawing.
    from core.reporting.capped_recheck import _latest_list

    if _latest_list(session) is None:
        return {"awaiting_list": True, "how": _AWAITING}
    batches = list(
        session.scalars(
            select(CappedRecheckBatch).order_by(CappedRecheckBatch.created_at.desc())
        ).all()
    )
    if not batches:
        return {"none_run": True}
    finished = next((batch for batch in batches if batch.status == "complete"), None)
    stopped = _later_stopped(batches, finished)
    body: dict[str, Any] = {"finished": None, "stopped": None}
    if finished is not None:
        body["finished"] = _freeze(session, finished)
    if stopped is not None:
        body["stopped"] = _freeze(session, stopped)
    # running and interrupted are left for the live projection.
    if finished is None and stopped is None:
        body["open_only"] = True
    return body


def _later_stopped(
    batches: list[CappedRecheckBatch], finished: Optional[CappedRecheckBatch]
) -> Optional[CappedRecheckBatch]:
    """The latest batch after the finished one whose status starts ``stopped:``.

    With no finished batch, the latest stopped batch stands on its own.
    ``running`` and ``interrupted`` are not frozen.
    """
    for batch in batches:
        if not str(batch.status).startswith("stopped:"):
            continue
        if finished is None or batch.created_at > finished.created_at:
            return batch
    return None


def _freeze(session: Session, batch: CappedRecheckBatch) -> dict[str, Any]:
    tickets = list(
        session.scalars(
            select(CappedRecheckTicket)
            .where(CappedRecheckTicket.batch_id == batch.batch_id)
            .order_by(CappedRecheckTicket.position)
        ).all()
    )
    categories = _categories(session, tickets)
    finished_at = _import_finished(session, tickets)
    terminals = _terminals(session, _run_ids(tickets))
    stored = []
    counts: dict[str, dict[str, int]] = {
        group: {direction: 0 for direction in _DIRECTIONS} for group in _GROUPS
    }
    for ticket in tickets:
        group = ticket.ticket_group if ticket.ticket_group in counts else "top"
        direction = _direction(ticket, categories, finished_at, terminals)
        counts[group][direction] += 1
        stored.append(
            {
                "import_id": ticket.import_id,
                "ticket_id": ticket.ticket_id,
                "source": ticket.source,
                "group": group,
                "direction": direction,
                "reason": ticket.reason,
                "run_id": ticket.run_id,
            }
        )
    return {
        "batch_id": batch.batch_id,
        "status": batch.status,
        "groups": [_group(group, counts[group]) for group in _GROUPS],
        "tickets": stored,
        "cost": _cost(session, _started_ids(session, tickets)),
    }


def _group(group: str, counts: dict[str, int]) -> dict[str, Any]:
    total = sum(counts.values())
    rows = []
    for direction in _DIRECTIONS:
        low, high = None, None
        if total >= MIN_JUDGED:
            span = _wilson(counts[direction], total)
            if span is not None:
                low, high = span
        rows.append(
            {"direction": direction, "k": counts[direction], "low": low, "high": high}
        )
    return {"group": group, "n": total, "rows": rows}


def _direction(
    ticket: CappedRecheckTicket,
    categories: dict[tuple[str, str], str],
    finished_at: dict[str, Optional[datetime]],
    terminals: dict[str, tuple[str, str]],
) -> str:
    reason = (ticket.reason or "").strip()
    if reason:
        return "could_not"
    # A re-derived import replaced the closure the batch was drawn against.
    current = finished_at.get(ticket.import_id)
    stored = ticket.import_finished_at
    if current is not None and (stored is None or _utc(current) > _utc(stored)):
        return "could_not"
    outcome, disposition = terminals.get(ticket.run_id or "", ("", ""))
    # failed, budget_exhausted, data_starved, and any other unfinished terminal.
    if outcome != "completed":
        return "could_not"
    category = categories.get((ticket.import_id, ticket.ticket_id))
    if category in _CLOSURE_NOT_REAL:
        closure = "not_real"
    elif category == "unable_to_resolve":
        closure = "undetermined"
    else:
        return "could_not"
    label = disposition.strip().lower()
    if label in _REAL:
        decision = "real"
    elif label in _NOT_REAL:
        decision = "not_real"
    else:
        # inconclusive, needs_review, or no side taken.
        decision = "undetermined"
    return f"{closure}_{decision}"


def _categories(
    session: Session, tickets: list[CappedRecheckTicket]
) -> dict[tuple[str, str], str]:
    import_ids = {ticket.import_id for ticket in tickets if ticket.import_id}
    if not import_ids:
        return {}
    rows = session.scalars(
        select(HistoryTicket).where(HistoryTicket.import_id.in_(import_ids))
    ).all()
    found: dict[tuple[str, str], HistoryTicket] = {}
    for row in rows:
        if not row.ticket_id:
            continue
        key = (row.import_id, row.ticket_id)
        held = found.get(key)
        if held is None or row.position < held.position:
            found[key] = row
    return {key: row.category for key, row in found.items()}


def _import_finished(
    session: Session, tickets: list[CappedRecheckTicket]
) -> dict[str, Optional[datetime]]:
    import_ids = {ticket.import_id for ticket in tickets if ticket.import_id}
    if not import_ids:
        return {}
    rows = session.scalars(
        select(HistoryImport).where(HistoryImport.id.in_(import_ids))
    ).all()
    return {row.id: row.finished_at for row in rows}


def _started_ids(session: Session, tickets: list[CappedRecheckTicket]) -> list[str]:
    """Runs the projection counts: a workflow row, or a run id with no reason yet.

    A refusal keeps the id it claimed and sets a reason. That is not a run.
    """
    ids = _run_ids(tickets)
    if not ids:
        return []
    present = set(
        session.scalars(
            select(WorkflowRun.run_id).where(WorkflowRun.run_id.in_(ids))
        ).all()
    )
    by_run = {ticket.run_id: ticket for ticket in tickets if ticket.run_id}
    kept = []
    for run_id in ids:
        ticket = by_run.get(run_id)
        reason = (ticket.reason or "").strip() if ticket is not None else ""
        if run_id in present or not reason:
            kept.append(run_id)
    return kept


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _run_ids(tickets: list[CappedRecheckTicket]) -> list[str]:
    found = []
    for ticket in tickets:
        run_id = ticket.run_id
        if not isinstance(run_id, str) or not run_id:
            continue
        try:
            uuid.UUID(run_id)
        except ValueError:
            continue
        found.append(run_id)
    return found


def _terminals(session: Session, run_ids: list[str]) -> dict[str, tuple[str, str]]:
    if not run_ids:
        return {}
    rows = session.execute(
        text("""
            SELECT DISTINCT ON (run_id)
                   run_id::text AS run_id,
                   payload->>'outcome' AS outcome,
                   coalesce(payload->>'disposition', '') AS disposition
            FROM agent_events
            WHERE kind = 'terminal' AND run_id = ANY(:ids)
            ORDER BY run_id, seq DESC
            """),
        {"ids": [uuid.UUID(run_id) for run_id in run_ids]},
    ).all()
    return {row.run_id: (row.outcome or "", row.disposition or "") for row in rows}


def _cost(session: Session, run_ids: list[str]) -> dict[str, Any]:
    wall = _wall(session, run_ids)
    source, provider_type = _model_price(session)
    priced_zero = source == "zero"
    local = priced_zero and _local(provider_type)
    body: dict[str, Any] = {
        "runs": len(run_ids),
        "calls": 0 if not run_ids else None,
        "wall_ms": wall,
        "dollars": 0.0 if not run_ids and source in ("zero", "exact") else None,
        "priced_at_zero": priced_zero,
        "local_model": local,
        "unpriced": source not in ("zero", "exact"),
        "unread": False,
    }
    if not run_ids:
        # No call to price. An unknown model is not reported as an unpriced call.
        body["unpriced"] = False
        if not priced_zero and source != "exact":
            body["priced_at_zero"] = False
            body["local_model"] = False
        return body
    try:
        results = asyncio.run(_gather(run_ids))
    except Exception as exc:  # noqa: BLE001 - the figures still freeze
        logger.error("capped re-check could not read spend: %s", type(exc).__name__)
        return _unread(body, wall, len(run_ids))
    if any(isinstance(item, Exception) for item in results):
        return _unread(body, wall, len(run_ids))
    calls = 0
    dollars = 0.0
    unpriced_calls = 0
    for item in results:
        if item is None:
            continue
        calls += int(item["calls"])
        dollars += float(item["cost_usd"])
        unpriced_calls += int(item["unpriced_calls"])
    body["calls"] = calls
    if priced_zero:
        body["dollars"] = 0.0
        body["unpriced"] = False
        return body
    if source == "exact" and unpriced_calls == 0:
        body["dollars"] = round(dollars, 4)
        body["unpriced"] = False
        return body
    body["dollars"] = None
    body["priced_at_zero"] = False
    body["local_model"] = False
    body["unpriced"] = True
    return body


def _unread(body: dict[str, Any], wall: Optional[int], runs: int) -> dict[str, Any]:
    body.update(
        runs=runs,
        calls=None,
        wall_ms=wall,
        dollars=None,
        priced_at_zero=False,
        local_model=False,
        unpriced=False,
        unread=True,
    )
    return body


def _wall(session: Session, run_ids: list[str]) -> Optional[int]:
    if not run_ids:
        return 0
    values = session.scalars(
        select(WorkflowRun.duration_ms).where(WorkflowRun.run_id.in_(run_ids))
    ).all()
    known = [
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    if not known:
        return None
    return sum(known)


def _model_price(session: Session) -> tuple[str, Optional[str]]:
    try:
        resolved = get_registry().resolve_model_for_component("investigation")
        if resolved is None:
            return "unknown", None
        provider_id, model_id = resolved
        row = session.get(LLMProviderConfig, provider_id)
        provider_type = row.provider_type if row is not None else None
        if not provider_type:
            return "unknown", None
        return get_registry().get_pricing_source(model_id, provider_type), provider_type
    except Exception as exc:  # noqa: BLE001 - an unread price is not $0
        logger.error(
            "capped re-check could not read the model price: %s", type(exc).__name__
        )
        return "unknown", None


def _local(provider_type: Optional[str]) -> bool:
    return provider_type == "ollama" or (
        provider_type is not None and provider_type in _SELF_HOSTED_TYPES
    )


async def _gather(run_ids: list[str]) -> list[Any]:
    gate = asyncio.Semaphore(PROJECTION_READ_CONCURRENCY)
    lost = asyncio.Event()

    async def one(run_id: str) -> Any:
        if lost.is_set():
            return RuntimeError("could not reach the agent layer")
        async with gate:
            if lost.is_set():
                return RuntimeError("could not reach the agent layer")
            try:
                return await read_spend(run_id)
            except RuntimeError as exc:
                if str(exc).startswith("could not reach"):
                    lost.set()
                return exc
            except Exception as exc:  # noqa: BLE001 - one read, the cost line says so
                return exc

    return list(await asyncio.gather(*(one(run_id) for run_id in run_ids)))


def _present_batch(
    session: Session, stored: dict, recorded_at: Any, text_ok: bool
) -> dict[str, Any]:
    tickets = [row for row in stored.get("tickets") or [] if isinstance(row, dict)]
    after = _after_close(session, [row.get("run_id") for row in tickets])
    groups = []
    for group in stored.get("groups") or []:
        if not isinstance(group, dict):
            continue
        name = group.get("group")
        own = [row for row in tickets if row.get("group") == name]
        shown_tickets = []
        approximate = 0
        after_close = 0
        for row in own:
            ticket, note, approx = _ticket(session, row, recorded_at)
            if approx:
                approximate += 1
            if row.get("run_id") in after:
                after_close += 1
            shown_tickets.append(_shown_ticket(row, ticket, note, text_ok))
        groups.append(
            {
                "group": name,
                "n": group.get("n") or 0,
                "rows": list(group.get("rows") or []),
                "after_close": after_close,
                "approximate": approximate,
                "tickets": shown_tickets,
            }
        )
    return {
        "batch_id": stored.get("batch_id"),
        "status": stored.get("status"),
        "groups": groups,
        "cost": stored.get("cost") or {},
    }


def _after_close(session: Session, run_ids: list[Any]) -> set[str]:
    ids = []
    for run_id in run_ids:
        if not isinstance(run_id, str) or not run_id:
            continue
        try:
            ids.append(uuid.UUID(run_id))
        except ValueError:
            continue
    if not ids:
        return set()
    rows = session.execute(
        text("""
            SELECT DISTINCT run_id::text AS run_id
            FROM agent_events
            WHERE run_id = ANY(:ids)
              AND (
                kind = 'facts'
                OR (kind = 'call' AND payload->>'tool' = 'lookup_indicators')
              )
            """),
        {"ids": ids},
    ).all()
    return {row.run_id for row in rows}


def _ticket(
    session: Session, stored: dict, recorded_at: Any
) -> tuple[Optional[HistoryTicket], Optional[str], bool]:
    """The history ticket when the import is still the one the report read.

    A ticket id is not a reason to skip ``_row``. A re-derived import returns
    its date and no flag from the new rows.
    """
    import_id = stored.get("import_id")
    ticket_id = stored.get("ticket_id")
    position = None
    if isinstance(import_id, str) and isinstance(ticket_id, str) and ticket_id:
        position = session.scalar(
            select(HistoryTicket.position)
            .where(
                HistoryTicket.import_id == import_id,
                HistoryTicket.ticket_id == ticket_id,
            )
            .order_by(HistoryTicket.position)
            .limit(1)
        )
    row, note = _row(
        session,
        {"import_id": import_id, "position": position},
        recorded_at,
    )
    if note or not isinstance(row, HistoryTicket):
        return None, _gone_sentence(session, stored, note or _REMOVED), False
    return row, None, bool(row.closed_approximate)


def _gone_sentence(session: Session, stored: dict, note: str) -> str:
    """The import-gone sentence, read from the stored ticket. Not a rewrite.

    Erased only when the import is gone, the ticket was a run (a ``run_id``
    and an empty reason), and that ``workflow_runs`` row is gone. A refused
    claim keeps a reason and never had a row. A run a Case was opened from
    keeps its row. Both stay ``removed with its import``.
    """
    if note != _REMOVED:
        return note
    run_id = stored.get("run_id")
    if stored.get("reason") or not isinstance(run_id, str) or not run_id:
        return note
    import_id = stored.get("import_id")
    if not isinstance(import_id, str) or not import_id:
        return note
    if session.get(HistoryImport, import_id) is not None:
        return note
    if session.get(WorkflowRun, run_id) is not None:
        return note
    return _ERASED


def _shown_ticket(
    stored: dict,
    ticket: Optional[HistoryTicket],
    note: Optional[str],
    text_ok: bool,
) -> dict[str, Any]:
    shown = {
        "ticket": stored.get("ticket_id") or "no ticket ID in the export",
        "source": stored.get("source") or "",
        "group": stored.get("group"),
        "direction": stored.get("direction"),
        "reason": stored.get("reason"),
        "title": None,
        "entities": [],
        "text_note": None,
    }
    if not text_ok:
        shown["text_note"] = _TEXT_NEEDS
        return shown
    if note or ticket is None:
        shown["text_note"] = note or _REMOVED
        return shown
    title = (ticket.title or "").strip()
    shown["title"] = title or None
    shown["entities"] = _entities(ticket)
    return shown


def _entities(ticket: HistoryTicket) -> list[str]:
    values = []
    for key in ticket.entity_keys or []:
        _kind, sep, value = str(key).partition(":")
        text_value = value if sep and value else str(key).strip()
        if text_value:
            values.append(text_value)
    return values
