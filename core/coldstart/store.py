"""Reads and writes for a cold start run. Synchronous: callers keep it off the loop."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import Float, func, select
from sqlalchemy import text as sql_text
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.coldstart.baseline import Alert, miss_bound
from core.coldstart.tickets import Label, Ticket
from core.storage.connection import get_db_manager
from core.storage.models import ColdStartAlert, ColdStartOutcome, ColdStartRun
from core.storage.models.cold_start import ACTIVE_STATUSES, RunStatus
from core.storage.unit_of_work import unit_of_work

_CHUNK = 1000
_DROP_KINDS = ("alerts", "tickets")

# One bigint for the whole feature: the partial index allows one active run at a time.
_WORKER_LOCK = 0x7669_6769_6C43_5331  # "vigilCS1"
# pg_locks shows the bigint form split in two, with objsubid 1.
_WORKER_LOCK_HIGH = _WORKER_LOCK >> 32
_WORKER_LOCK_LOW = _WORKER_LOCK & 0xFFFF_FFFF


class RunNotFound(Exception):
    pass


class ActiveRunExists(Exception):
    def __init__(self) -> None:
        super().__init__("Another cold start run is collecting or judging.")


class GroupNotFound(Exception):
    pass


class RunBusy(Exception):
    """A worker holds this run, or another run went live at the same moment."""


def _load(session: Session, run_id: str, lock: bool = False) -> ColdStartRun:
    query = select(ColdStartRun).where(ColdStartRun.run_id == run_id)
    run = session.scalar(query.with_for_update() if lock else query)
    if run is None:
        raise RunNotFound(run_id)
    return run


def group_key(signature: str) -> str:
    return hashlib.sha256(signature.encode()).hexdigest()[:16]


def _has_alerts(session: Session, run_id: str) -> bool:
    return (
        session.scalar(
            select(ColdStartAlert.alert_id)
            .where(ColdStartAlert.run_id == run_id)
            .limit(1)
        )
        is not None
    )


def has_alerts(run_id: str) -> bool:
    with unit_of_work() as session:
        return _has_alerts(session, run_id)


def _empty_upload_run(session: Session) -> Optional[ColdStartRun]:
    run = session.scalar(
        select(ColdStartRun).where(ColdStartRun.status == RunStatus.COLLECTING)
    )
    if run is None or run.source != "upload" or _has_alerts(session, run.run_id):
        return None
    return run


def create_run(
    *,
    source: str,
    history_days: int,
    review_days: int,
    judge_limit: int,
    created_by: str,
) -> str:
    """A new run in ``collecting``. An upload run that never received a file holds no
    work, so it gives way instead of leaving the customer unable to start another."""
    try:
        with unit_of_work() as session:
            abandoned = _empty_upload_run(session)
            if abandoned is not None:
                abandoned.move_to(RunStatus.FAILED)
                abandoned.error = "Replaced by a new run before any file was uploaded."
                session.flush()
            run = ColdStartRun(
                source=source,
                status=RunStatus.COLLECTING,
                history_days=history_days,
                review_days=review_days,
                judge_limit=judge_limit,
                created_by=created_by,
                progress={"phase": "collecting"},
                drops={},
            )
            session.add(run)
            session.flush()
            return run.run_id
    except IntegrityError as e:
        raise ActiveRunExists() from e


def transition(run_id: str, status: RunStatus, **fields: Any) -> None:
    try:
        with unit_of_work() as session:
            run = _load(session, run_id, lock=True)
            run.move_to(status)
            for name, value in fields.items():
                setattr(run, name, value)
    except IntegrityError as e:
        raise ActiveRunExists() from e


def set_progress(run_id: str, **fields: Any) -> None:
    with unit_of_work() as session:
        run = _load(session, run_id, lock=True)
        run.progress = {**run.progress, **fields}


def record_drops(run_id: str, kind: str, key: str, dropped: Mapping[str, int]) -> None:
    """Replace, not add: the same file or fetch sends the same key again."""
    assert kind in _DROP_KINDS
    with unit_of_work() as session:
        run = _load(session, run_id, lock=True)
        kinds = dict(run.drops or {})
        kinds[kind] = {**kinds.get(kind, {}), key: dict(dropped)}
        run.drops = kinds


def _insert_alerts(session: Session, run_id: str, rows: List[Dict[str, Any]]) -> None:
    for start in range(0, len(rows), _CHUNK):
        session.execute(
            insert(ColdStartAlert)
            .values([{**r, "run_id": run_id} for r in rows[start : start + _CHUNK]])
            .on_conflict_do_nothing(index_elements=["run_id", "alert_id"])
        )


def insert_alerts(run_id: str, rows: List[Dict[str, Any]]) -> None:
    with unit_of_work() as session:
        _insert_alerts(session, run_id, rows)


def insert_uploaded_alerts(run_id: str, rows: List[Dict[str, Any]]) -> None:
    """Raises ValueError, storing nothing, for an id the run holds under another source.
    The run's lock spans the check and the insert, or two uploads could both pass."""
    sources = {r["alert_id"]: r["source"] for r in rows}
    ids = list(sources)
    with unit_of_work() as session:
        _load(session, run_id, lock=True)
        for start in range(0, len(ids), _CHUNK):
            held = session.execute(
                select(ColdStartAlert.alert_id, ColdStartAlert.source).where(
                    ColdStartAlert.run_id == run_id,
                    ColdStartAlert.alert_id.in_(ids[start : start + _CHUNK]),
                )
            )
            for alert_id, source in held:
                if source != sources[alert_id]:
                    raise ValueError(
                        f"Alert id {alert_id} is already held from another source."
                    )
        _insert_alerts(session, run_id, rows)


def insert_tickets(run_id: str, tickets: Iterable[Ticket]) -> None:
    rows = [
        {
            "run_id": run_id,
            "external_id": t.external_id,
            "alert_ids": list(t.alert_ids),
            "signature": t.signature,
            "disposition": t.disposition.value,
            "closed_at": t.closed_at,
            "entities": sorted(t.entities),
            "source_system": t.source_system,
        }
        for t in tickets
    ]
    with unit_of_work() as session:
        for start in range(0, len(rows), _CHUNK):
            session.execute(
                insert(ColdStartOutcome)
                .values(rows[start : start + _CHUNK])
                .on_conflict_do_nothing(index_elements=["run_id", "external_id"])
            )


@dataclass(frozen=True)
class RunParams:
    run_id: str
    source: str
    status: RunStatus
    history_days: int
    review_days: int
    judge_limit: int
    baseline: Optional[Dict[str, Any]]


def run_params(run_id: str) -> RunParams:
    with unit_of_work() as session:
        run = _load(session, run_id)
        return RunParams(
            run_id=run.run_id,
            source=run.source,
            status=run.status,
            history_days=run.history_days,
            review_days=run.review_days,
            judge_limit=run.judge_limit,
            baseline=run.baseline,
        )


def _as_alert(row: Any) -> Alert:
    return Alert(
        alert_id=row.alert_id,
        source=row.source,
        title=row.title,
        severity=row.severity,
        observed_at=row.observed_at,
        entities=frozenset(row.entities),
    )


def load_alerts(run_id: str) -> List[Alert]:
    with unit_of_work() as session:
        rows = session.execute(
            select(
                ColdStartAlert.alert_id,
                ColdStartAlert.source,
                ColdStartAlert.title,
                ColdStartAlert.severity,
                ColdStartAlert.observed_at,
                ColdStartAlert.entities,
            ).where(ColdStartAlert.run_id == run_id)
        ).all()
        return [_as_alert(row) for row in rows]


def load_reviews(run_id: str) -> List[Tuple[str, str]]:
    """Each reviewed kind and its verdict: one label per kind, not one per alert."""
    with unit_of_work() as session:
        rows = session.execute(
            select(ColdStartAlert.signature, ColdStartAlert.review)
            .where(ColdStartAlert.run_id == run_id, ColdStartAlert.review.isnot(None))
            .distinct()
        ).all()
        return [(row[0], row[1]) for row in rows]


def load_tickets(run_id: str) -> List[Ticket]:
    with unit_of_work() as session:
        rows = session.scalars(
            select(ColdStartOutcome).where(ColdStartOutcome.run_id == run_id)
        ).all()
        return [
            Ticket(
                external_id=row.external_id,
                disposition=Label(row.disposition),
                closed_at=row.closed_at,
                alert_ids=tuple(row.alert_ids),
                signature=row.signature,
                entities=frozenset(row.entities),
                source_system=row.source_system,
            )
            for row in rows
        ]


def save_build(
    run_id: str,
    *,
    baseline: Dict[str, Any],
    as_of: datetime,
    assessed: List[Dict[str, Any]],
    tickets_unlinked: int,
) -> None:
    """Writes only the build's columns, so reviews and LLM results survive a rebuild."""
    with unit_of_work() as session:
        run = _load(session, run_id, lock=True)
        run.baseline = baseline
        run.progress = {**run.progress, "tickets_unlinked": tickets_unlinked}
        session.execute(
            update(ColdStartAlert)
            .where(
                ColdStartAlert.run_id == run_id,
                ColdStartAlert.observed_at < as_of,
                ColdStartAlert.in_review_window.is_(True),
            )
            .values(in_review_window=False, stage1=None, rule=None, reasons=None)
        )
        if assessed:
            session.execute(
                update(ColdStartAlert),
                [{**row, "run_id": run_id} for row in assessed],
            )


@dataclass(frozen=True)
class Candidate:
    alert: Alert
    description: str
    judged: bool


def judge_candidates(run_id: str) -> List[Candidate]:
    with unit_of_work() as session:
        rows = session.execute(
            select(
                ColdStartAlert.alert_id,
                ColdStartAlert.source,
                ColdStartAlert.title,
                ColdStartAlert.severity,
                ColdStartAlert.observed_at,
                ColdStartAlert.entities,
                ColdStartAlert.payload,
                ColdStartAlert.llm,
            ).where(
                ColdStartAlert.run_id == run_id,
                ColdStartAlert.in_review_window.is_(True),
                ColdStartAlert.stage1 == "abstain",
            )
        ).all()
        return [
            Candidate(
                alert=_as_alert(row),
                description=(row.payload or {}).get("description") or "",
                judged=row.llm is not None,
            )
            for row in rows
        ]


def save_llm(run_id: str, alert_id: str, llm: Dict[str, Any]) -> None:
    with unit_of_work() as session:
        session.execute(
            update(ColdStartAlert)
            .where(ColdStartAlert.run_id == run_id, ColdStartAlert.alert_id == alert_id)
            .values(llm=llm)
        )


def _merged_drops(run: ColdStartRun, kind: str) -> Dict[str, int]:
    total: Counter = Counter()
    for dropped in (run.drops or {}).get(kind, {}).values():
        total.update(dropped)
    return dict(total)


def _counts(session: Session, run: ColdStartRun) -> Dict[str, Any]:
    alert = ColdStartAlert
    in_window = alert.in_review_window.is_(True)
    latency = alert.llm["latency_ms"].astext.cast(Float)
    totals = session.execute(
        select(
            func.count().label("read"),
            func.count(func.distinct(alert.signature)).label("kinds"),
            func.count(
                func.distinct(func.date(func.timezone("UTC", alert.observed_at)))
            ).label("days"),
            func.count().filter(in_window).label("review"),
            func.count()
            .filter(in_window, alert.stage1 == "escalate")
            .label("escalate"),
            func.count().filter(in_window, alert.stage1 == "abstain").label("abstain"),
            func.count()
            .filter(in_window, alert.stage1 == "dismiss_candidate")
            .label("dismiss"),
            func.count().filter(alert.llm.is_not(None)).label("judged"),
            func.count()
            .filter(alert.llm["parsed"].astext == "false")
            .label("unparsed"),
            func.avg(latency).label("latency"),
        ).where(alert.run_id == run.run_id)
    ).one()
    tickets_kept = session.scalar(
        select(func.count())
        .select_from(ColdStartOutcome)
        .where(ColdStartOutcome.run_id == run.run_id)
    )
    return {
        "alerts_read": totals.read,
        "alerts_dropped": _merged_drops(run, "alerts"),
        "days_covered": totals.days,
        "kinds": totals.kinds,
        "tickets_kept": tickets_kept,
        "tickets_dropped": _merged_drops(run, "tickets"),
        "tickets_unlinked": (run.progress or {}).get("tickets_unlinked", 0),
        "review_alerts": totals.review,
        "dismiss_candidates": totals.dismiss,
        "escalations": totals.escalate,
        "abstained": totals.abstain,
        "judged": totals.judged,
        "unparsed": totals.unparsed,
        "average_model_seconds": (
            round(totals.latency / 1000, 2) if totals.latency is not None else None
        ),
    }


def worker_running(session: Session) -> bool:
    """A held advisory lock means a live process: the server drops it the moment the
    holder's connection dies, however that process stopped."""
    return bool(
        session.execute(
            sql_text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND granted AND classid = :high AND objid = :low AND objsubid = 1 "
                "AND database = (SELECT oid FROM pg_database "
                "WHERE datname = current_database()))"
            ),
            {"high": _WORKER_LOCK_HIGH, "low": _WORKER_LOCK_LOW},
        ).scalar_one()
    )


def _summary(session: Session, run: ColdStartRun) -> Dict[str, Any]:
    running = run.status in ACTIVE_STATUSES and worker_running(session)
    baseline = run.baseline or {}
    return {
        "id": run.run_id,
        "source": run.source,
        "status": run.status.value,
        "running": running,
        "resumable": run.status in ACTIVE_STATUSES and not running,
        "history_days": run.history_days,
        "review_days": run.review_days,
        "judge_limit": run.judge_limit,
        "as_of": baseline.get("as_of"),
        "judging_skipped": run.judging_skipped,
        "error": run.error,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "live_at": run.live_at.isoformat() if run.live_at else None,
        "progress": {
            key: value
            for key, value in (run.progress or {}).items()
            if key != "tickets_unlinked"
        },
        "counts": _counts(session, run),
    }


def run_summary(run_id: str) -> Dict[str, Any]:
    with unit_of_work() as session:
        return _summary(session, _load(session, run_id))


def latest_summary() -> Optional[Dict[str, Any]]:
    with unit_of_work() as session:
        run = session.scalar(
            select(ColdStartRun).order_by(ColdStartRun.created_at.desc()).limit(1)
        )
        return _summary(session, run) if run else None


def _group(
    signature: str, rows: List[Any], history: Mapping[str, Any]
) -> Dict[str, Any]:
    rows = sorted(rows, key=lambda r: (r.observed_at, r.alert_id), reverse=True)
    stage1 = Counter(r.stage1 for r in rows)
    rule, _ = Counter(r.rule for r in rows).most_common(1)[0]
    judged = [r.llm for r in rows if r.llm]
    verdicts = Counter(j["verdict"] for j in judged if j.get("parsed"))
    reviews = Counter(r.review for r in rows if r.review)
    benign = history.get("benign", 0)
    real = history.get("true_positive", 0)
    sample = rows[0]
    source = sample.source
    return {
        "key": group_key(signature),
        "label": signature[len(source) + 1 :],
        "source": source,
        "count": len(rows),
        "stage1": {
            "escalate": stage1["escalate"],
            "abstain": stage1["abstain"],
            "dismiss_candidate": stage1["dismiss_candidate"],
        },
        "top_rule": rule,
        "reasons": next(r.reasons for r in rows if r.rule == rule),
        "sample": {
            "alert_id": sample.alert_id,
            "title": sample.title,
            "severity": sample.severity,
            "observed_at": sample.observed_at.isoformat(),
            "entities": sample.entities,
            "description": (sample.payload or {}).get("description") or "",
        },
        "llm": {
            "verdicts": dict(verdicts),
            "judged": len(judged),
            "unparsed": sum(1 for j in judged if not j.get("parsed")),
            "example_reasoning": next(
                (j["reasoning"] for j in judged if j.get("parsed") and j["reasoning"]),
                None,
            ),
        },
        "review": {
            "agree": reviews["agree"],
            "not_benign": reviews["not_benign"],
            "pending": len(rows) - sum(reviews.values()),
        },
        "tickets": {"benign": benign, "real": real},
        "miss_bound": miss_bound(benign) if real == 0 else None,
    }


def _window_rows(session: Session, run_id: str) -> List[Any]:
    return session.execute(
        select(
            ColdStartAlert.alert_id,
            ColdStartAlert.source,
            ColdStartAlert.signature,
            ColdStartAlert.observed_at,
            ColdStartAlert.title,
            ColdStartAlert.severity,
            ColdStartAlert.entities,
            ColdStartAlert.payload,
            ColdStartAlert.stage1,
            ColdStartAlert.rule,
            ColdStartAlert.reasons,
            ColdStartAlert.llm,
            ColdStartAlert.review,
        ).where(
            ColdStartAlert.run_id == run_id,
            ColdStartAlert.in_review_window.is_(True),
        )
    ).all()


def groups(run_id: str) -> List[Dict[str, Any]]:
    with unit_of_work() as session:
        run = _load(session, run_id)
        history = (run.baseline or {}).get("signatures", {})
        by_signature: Dict[str, List[Any]] = defaultdict(list)
        for row in _window_rows(session, run_id):
            by_signature[row.signature].append(row)
        out = [
            _group(signature, rows, history.get(signature, {}))
            for signature, rows in by_signature.items()
        ]
    out.sort(key=lambda g: (-g["count"], g["key"]))
    return out


def review_group(run_id: str, key: str, verdict: str, actor: str) -> Dict[str, Any]:
    """Applies to the group's review-window alerts. Repeating a verdict changes nothing,
    so who reviewed and when stay those of the person who first said it."""
    with unit_of_work() as session:
        _load(session, run_id)
        signatures = session.scalars(
            select(ColdStartAlert.signature)
            .where(
                ColdStartAlert.run_id == run_id,
                ColdStartAlert.in_review_window.is_(True),
            )
            .distinct()
        ).all()
        signature = next((s for s in signatures if group_key(s) == key), None)
        if signature is None:
            raise GroupNotFound(key)
        in_group = (
            ColdStartAlert.run_id == run_id,
            ColdStartAlert.signature == signature,
            ColdStartAlert.in_review_window.is_(True),
        )
        session.execute(
            update(ColdStartAlert)
            .where(*in_group, ColdStartAlert.review.is_distinct_from(verdict))
            .values(review=verdict, reviewed_by=actor, reviewed_at=func.now())
        )
        counts = dict(
            session.execute(
                select(ColdStartAlert.review, func.count())
                .where(*in_group)
                .group_by(ColdStartAlert.review)
            ).all()
        )
    return {
        "key": key,
        "review": {
            "agree": counts.get("agree", 0),
            "not_benign": counts.get("not_benign", 0),
            "pending": counts.get(None, 0),
        },
    }


def mark_live(run_id: str) -> None:
    """Any earlier live run moves off live first: the index allows one at a time and
    checks each statement, so this order is the only one that can succeed."""
    try:
        with unit_of_work() as session:
            run = _load(session, run_id, lock=True)
            if run.status is RunStatus.LIVE:
                return
            previous = session.scalars(
                select(ColdStartRun)
                .where(ColdStartRun.status == RunStatus.LIVE)
                .with_for_update()
            ).all()
            for other in previous:
                other.move_to(RunStatus.READY)
                other.live_at = None
            session.flush()
            run.move_to(RunStatus.LIVE)
            run.live_at = func.now()
    except IntegrityError as e:
        raise RunBusy("Another run went live at the same moment.") from e


def live_baseline_dict() -> Optional[Dict[str, Any]]:
    with unit_of_work() as session:
        return session.scalar(
            select(ColdStartRun.baseline).where(ColdStartRun.status == RunStatus.LIVE)
        )


class WorkerLease:
    """Held for as long as a run's task works on it. ``acquire`` returns None when
    another process or task is already working."""

    def __init__(self, connection: Any):
        self._connection = connection

    @classmethod
    def acquire(cls) -> Optional["WorkerLease"]:
        connection = (
            get_db_manager()
            .engine.connect()
            .execution_options(isolation_level="AUTOCOMMIT")
        )
        got = connection.execute(
            sql_text("SELECT pg_try_advisory_lock(:key)"), {"key": _WORKER_LOCK}
        ).scalar_one()
        if not got:
            connection.close()
            return None
        return cls(connection)

    def release(self) -> None:
        try:
            self._connection.execute(
                sql_text("SELECT pg_advisory_unlock(:key)"), {"key": _WORKER_LOCK}
            )
        finally:
            self._connection.close()
