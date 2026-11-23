"""Day 1 Report: when a run starts, what it stores, and how it is read.

The readiness sweep calls :func:`consider` inside its advisory lock.
A start is committed there, and the record is inserted afterwards, so a
start this process is still writing shows as running and a start left
behind shows as not completed. That start does not start another automatic
run. On demand takes the same lock.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth.permissions import holders, missing_permission
from core.config import is_demo_mode
from core.integrations import health
from core.integrations.readiness import DESKTOP_LINE, READY, UNAVAILABLE, UNKNOWN
from core.platform import notices
from core.reporting.checks import MODULES, VISIBILITY, spec_for
from core.reporting.enablement import disabled_module_ids
from core.storage.models import ReportRecord, ReportRun, SystemConfig, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow
from core.version import __version__

logger = logging.getLogger(__name__)

# The readiness sweep's lock (services/api/readiness.py). One key, so a tick
# and an on-demand run cannot both decide at once.
LOCK = 4874
FALLBACK_AFTER = timedelta(hours=24)
ANCHOR_KEY = "day1_report.anchor"
SETUP_KEY = "setup.wizard"

READINESS = "readiness"
FALLBACK = "fallback"
ON_DEMAND = "on_demand"

PASS = "pass"
FAIL = "fail"
NOT_CHECKED = "not_checked"
NEEDS_A_PERSON = "needs_a_person"

PREFACE = "This report describes the install, not the demo data."
NOTICE_KIND = "day1_report"
NOTICE_TITLE = "Day 1 Report"
NOTICE_MESSAGE = "A Day 1 Report is ready."

_ALERT_KINDS = frozenset({"pulled", "kafka", "receiver"})
_INTEGRATION_KINDS = frozenset({"integration", "pulled"})

_guard = threading.Lock()
_producing: set[str] = set()


def producing_ids() -> frozenset[str]:
    """Run ids this process has started and not yet finished writing."""
    with _guard:
        return frozenset(_producing)


def _mark(run_id: str) -> None:
    with _guard:
        _producing.add(run_id)


def _clear(run_id: str) -> None:
    with _guard:
        _producing.discard(run_id)


def abandon(run_id: str) -> None:
    """The start did not commit. It is not running."""
    _clear(run_id)


def consider(
    session: Session, state: dict, now: Optional[datetime] = None
) -> Optional[tuple[str, str]]:
    """Start the one automatic run this tick owes, or nothing.

    The caller holds the advisory lock and commits. Returns ``(run_id, trigger)``.
    Unknown and the desktop app's unavailable state are not first readiness.
    When the fallback and first readiness fall due together, the trigger is
    readiness. A later tick that finds a start or a record for that trigger
    starts nothing.
    """
    now = now or utcnow()
    _ensure_anchor(session, now)
    trigger = _due(session, state.get("state"), now)
    if trigger is None:
        return None
    run_id = _insert_start(session, trigger, None, now)
    return run_id, trigger


def begin_on_demand(actor_id: str) -> str:
    """Insert an on-demand start under the sweep's lock. The caller writes the record."""
    now = utcnow()
    run_id: Optional[str] = None
    try:
        with unit_of_work() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
            _ensure_anchor(session, now)
            run_id = _insert_start(session, ON_DEMAND, actor_id, now)
        return run_id
    except Exception:
        if run_id is not None:
            _clear(run_id)
        raise


def finish(run_id: str, trigger: str, actor_id: Optional[str], state: dict) -> str:
    """Build the record for a committed start and insert it. Clears the running mark."""
    try:
        built = build(state)
        return _insert_record(run_id, trigger, actor_id, built)
    finally:
        _clear(run_id)


def notify_finished(trigger: str, actor_id: Optional[str], record_id: str) -> None:
    """One link-only notice. Automatic runs go to platform.write holders;
    an on-demand run goes to the person who started it. No section content."""
    link = f"/report?record={record_id}"
    if trigger == ON_DEMAND and actor_id:
        recipients = [actor_id]
    else:
        recipients = holders("platform.write")
    for principal in recipients:
        try:
            notices.notify(
                principal, NOTICE_KIND, NOTICE_TITLE, NOTICE_MESSAGE, link=link
            )
        except Exception:  # noqa: BLE001 - the record is already stored
            logger.exception("day 1 report notice was not sent")


def build(state: dict) -> dict[str, Any]:
    """Each module that is switched on, from this readiness result.

    A listed id is left out, and ``modules`` names only what ran. Daemon,
    model and collection come from ``state``, daemon first. Sources come
    from integration health. A failed source read is one not-checked check,
    and the report still completes. Demo findings are not read.
    """
    disabled = disabled_module_ids()
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    modules: list[str] = []
    if VISIBILITY.id not in disabled:
        modules.append(VISIBILITY.id)
        checks.extend(_visibility_checks(state, evidence))
    preface = PREFACE if is_demo_mode() else None
    return {
        "modules": modules,
        "checks": checks,
        "evidence": evidence,
        "preface": preface,
    }


def _visibility_checks(state: dict, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks = _platform_checks(state)
    try:
        sources = health.collect()
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error("Day 1 Report could not read health: %s", type(exc).__name__)
        checks.append(
            _check(
                "visibility.source",
                NOT_CHECKED,
                "Could not read integration health.",
            )
        )
        evidence[VISIBILITY.id] = {"switched_off": []}
        return checks
    checks.extend(_source_checks(state, sources))
    evidence[VISIBILITY.id] = {"switched_off": _switched_off(sources)}
    return checks


def status() -> dict[str, Any]:
    """Home's line: a run this process is writing, a start with no record, the latest."""
    running = producing_ids()
    with unit_of_work() as session:
        open_runs = (
            session.query(ReportRun)
            .outerjoin(ReportRecord, ReportRecord.run_id == ReportRun.run_id)
            .filter(ReportRecord.record_id.is_(None))
            .order_by(ReportRun.started_at.desc())
            .all()
        )
        latest = (
            session.query(ReportRecord)
            .order_by(ReportRecord.recorded_at.desc())
            .first()
        )
        running_since = next(
            (row.started_at for row in open_runs if row.run_id in running), None
        )
        incomplete_since = next(
            (row.started_at for row in open_runs if row.run_id not in running), None
        )
        return {
            "running_since": _iso(running_since),
            "incomplete_since": _iso(incomplete_since),
            "latest_id": latest.record_id if latest else None,
            "latest_at": _iso(latest.recorded_at) if latest else None,
        }


def read_record(user_id: str, record_id: Optional[str]) -> Optional[dict[str, Any]]:
    """One record, or the latest. Sections the caller cannot read are left out."""
    with unit_of_work() as session:
        if record_id:
            record = session.get(ReportRecord, record_id)
        else:
            record = (
                session.query(ReportRecord)
                .order_by(ReportRecord.recorded_at.desc())
                .first()
            )
        if record is None:
            return None
        run = session.get(ReportRun, record.run_id)
        started = run.started_at if run is not None else record.recorded_at
        return _present(session, record, started, user_id)


def _present(
    session: Session, record: ReportRecord, started: datetime, user_id: str
) -> dict[str, Any]:
    withheld = [
        module_id
        for module_id in record.modules or []
        if missing_permission(user_id, MODULES[module_id].permission, session)
        is not None
    ]
    hidden = set(withheld)
    compared = _compared(session, record)
    prior = _visible_checks(compared, hidden) if compared is not None else {}
    checks = []
    for check in record.checks or []:
        if check.get("module") in hidden:
            continue
        shown = {
            "id": check.get("id"),
            "module": check.get("module"),
            "outcome": check.get("outcome"),
            "reason": check.get("reason") or "",
            "evidence": dict(check.get("evidence") or {}),
        }
        if compared is not None:
            shown["comparison"] = _comparison(check, prior.get(check.get("id")))
        checks.append(shown)
    evidence = record.evidence or {}
    switched_off = []
    if VISIBILITY.id not in hidden:
        switched_off = (evidence.get(VISIBILITY.id) or {}).get("switched_off") or []
    return {
        "id": record.record_id,
        "run_id": record.run_id,
        "recorded_at": _iso(record.recorded_at),
        "started_at": _iso(started),
        "trigger": record.trigger,
        "actor_id": record.actor_id,
        "actor_name": _actor_name(session, record.actor_id),
        "vigil_version": record.vigil_version,
        "preface": record.preface,
        "modules": list(record.modules or []),
        "withheld": withheld,
        "compared_at": _iso(compared.recorded_at) if compared is not None else None,
        "checks": checks,
        "switched_off": switched_off,
    }


def _compared(session: Session, record: ReportRecord) -> Optional[ReportRecord]:
    """The run this record is read against. The earliest record has none.

    The latest completed record strictly earlier than this one whose trigger
    is readiness or fallback. When that record does not exist, the earliest
    completed record. Nothing is stored.
    """
    before = ReportRecord.recorded_at < record.recorded_at
    day1 = (
        session.query(ReportRecord)
        .filter(before, ReportRecord.trigger.in_((READINESS, FALLBACK)))
        .order_by(ReportRecord.recorded_at.desc())
        .first()
    )
    if day1 is not None:
        return day1
    return (
        session.query(ReportRecord)
        .filter(before)
        .order_by(ReportRecord.recorded_at.asc())
        .first()
    )


def _visible_checks(record: ReportRecord, hidden: set[str]) -> dict[str, dict]:
    return {
        check.get("id"): check
        for check in record.checks or []
        if check.get("module") not in hidden
    }


def _comparison(current: dict, prior: Optional[dict]) -> dict[str, Any]:
    """How ``current`` reads against the same check on the compared run.

    New and fixed only when both outcomes are pass or fail. Anything else
    names both outcomes. A check the compared run did not have, including
    one whose module was off, is not in that run. Evidence is the record's
    own, and only beside new, fixed, or unchanged.
    """
    if prior is None:
        return {"kind": "not_in_compared_run"}
    before = prior.get("outcome")
    after = current.get("outcome")
    if before in (PASS, FAIL) and after in (PASS, FAIL):
        if before == after:
            kind = "unchanged"
        elif before == FAIL:
            kind = "fixed"
        else:
            kind = "new"
        return {"kind": kind, "prior_evidence": dict(prior.get("evidence") or {})}
    named: dict[str, Any] = {"kind": "named"}
    if before in (PASS, FAIL, NOT_CHECKED, NEEDS_A_PERSON):
        named["prior_outcome"] = before
    return named


def _due(session: Session, readiness: Optional[str], now: datetime) -> Optional[str]:
    records = _triggers(session, ReportRecord)
    starts = _triggers(session, ReportRun)
    readiness_clear = READINESS not in records and READINESS not in starts
    if readiness == READY and readiness_clear:
        return READINESS
    if not readiness_clear or FALLBACK in records or FALLBACK in starts:
        return None
    anchor = _anchor_time(session)
    clock = _clock_start(session)
    if anchor is None or clock is None:
        return None
    if now >= max(anchor, clock) + FALLBACK_AFTER:
        return FALLBACK
    return None


def _triggers(session: Session, model) -> set[str]:
    return {trigger for (trigger,) in session.query(model.trigger).distinct().all()}


def _ensure_anchor(session: Session, now: datetime) -> None:
    if session.get(SystemConfig, ANCHOR_KEY) is not None:
        return
    session.add(
        SystemConfig(
            key=ANCHOR_KEY,
            value={"at": _iso(now)},
            description="When the Day 1 Report was first available on this install",
            config_type="report",
            updated_by="system",
        )
    )


def _anchor_time(session: Session) -> Optional[datetime]:
    row = session.get(SystemConfig, ANCHOR_KEY)
    if row is None:
        return None
    return _parse((row.value or {}).get("at"))


def _clock_start(session: Session) -> Optional[datetime]:
    """Setup's recorded finish, or the earliest person account when it has none."""
    row = session.get(SystemConfig, SETUP_KEY)
    if row is not None:
        finished = _parse((row.value or {}).get("finished_at"))
        if finished is not None:
            return finished
    return (
        session.query(User.created_at)
        .filter(User.service_account.is_(False))
        .order_by(User.created_at.asc())
        .limit(1)
        .scalar()
    )


def _insert_start(
    session: Session, trigger: str, actor_id: Optional[str], now: datetime
) -> str:
    run_id = str(uuid.uuid4())
    session.add(
        ReportRun(run_id=run_id, started_at=now, trigger=trigger, actor_id=actor_id)
    )
    session.flush()
    _mark(run_id)
    return run_id


def _insert_record(
    run_id: str, trigger: str, actor_id: Optional[str], built: dict
) -> str:
    record_id = str(uuid.uuid4())
    with unit_of_work() as session:
        session.add(
            ReportRecord(
                record_id=record_id,
                run_id=run_id,
                recorded_at=utcnow(),
                trigger=trigger,
                actor_id=actor_id,
                vigil_version=__version__,
                modules=built["modules"],
                checks=built["checks"],
                evidence=built["evidence"],
                preface=built["preface"],
            )
        )
    return record_id


def _platform_checks(state: dict) -> list[dict[str, Any]]:
    """Daemon, then the model, then collection, from the readiness result."""
    status_name = state.get("state")
    if status_name == UNKNOWN:
        line = state.get("line") or "Install readiness is unknown."
        return [
            _check("visibility.daemon", NOT_CHECKED, line),
            _check("visibility.model", NOT_CHECKED, line),
            _check("visibility.collection", NOT_CHECKED, line),
        ]
    if status_name == UNAVAILABLE:
        line = state.get("line") or DESKTOP_LINE
        skipped = "Not judged on the desktop app."
        return [
            _check("visibility.daemon", NOT_CHECKED, line),
            _check("visibility.model", NOT_CHECKED, skipped),
            _check("visibility.collection", NOT_CHECKED, skipped),
        ]
    items = state.get("items") or []
    daemon = _by_id(items, "daemon")
    model = _by_id(items, "model")
    alert = _by_id(items, "alert-source")
    legacy = [
        item for item in items if str(item.get("id", "")).startswith("collected:")
    ]
    if alert is not None:
        collection = _from_item("visibility.collection", FAIL, alert)
    elif legacy:
        collection = _check(
            "visibility.collection",
            NOT_CHECKED,
            " ".join(item["text"] for item in legacy),
            fix=legacy[0].get("fix") or "Switch on scheduled collection.",
        )
    else:
        collection = _check(
            "visibility.collection",
            PASS,
            "Alert sources are collected.",
        )
    return [
        (
            _from_item("visibility.daemon", FAIL, daemon)
            if daemon
            else _check("visibility.daemon", PASS, "The background service is running.")
        ),
        (
            _from_item("visibility.model", FAIL, model)
            if model
            else _check(
                "visibility.model",
                PASS,
                "A model is assigned and its provider is routable.",
            )
        ),
        collection,
    ]


def _source_checks(state: dict, sources: list[dict]) -> list[dict[str, Any]]:
    items = state.get("items") or []
    lead = [
        item["id"][len("source:") :]
        for item in items
        if str(item.get("id", "")).startswith("source:")
    ]
    by_id = {
        row["source_id"]: row
        for row in sources
        if row.get("switched_on") and row.get("source_id")
    }
    ordered: list[dict] = []
    seen: set[str] = set()
    for source_id in lead:
        row = by_id.get(source_id)
        if row is not None:
            ordered.append(row)
            seen.add(source_id)
    for source_id in sorted(by_id):
        if source_id not in seen:
            ordered.append(by_id[source_id])
    item_by_source = {
        item["id"][len("source:") :]: item
        for item in items
        if str(item.get("id", "")).startswith("source:")
    }
    checks = []
    for row in ordered:
        source_id = row["source_id"]
        item = item_by_source.get(source_id)
        outcome = _source_outcome(row.get("state"))
        if item is not None and outcome != PASS:
            checks.append(
                _from_item(
                    f"visibility.source.{source_id}",
                    outcome,
                    item,
                    source=row,
                )
            )
            continue
        reason = _source_reason(row)
        fix = (
            None
            if outcome == PASS
            else _source_fix(row.get("state"), row.get("reason"))
        )
        checks.append(
            _check(
                f"visibility.source.{source_id}",
                outcome,
                reason,
                fix=fix,
                source=row,
            )
        )
    return checks


def _switched_off(sources: list[dict]) -> list[dict[str, str]]:
    listed = [
        {"source_id": row["source_id"], "brings": _brings(row.get("kind"))}
        for row in sources
        if not row.get("switched_on") and row.get("kind") in _INTEGRATION_KINDS
    ]
    listed.sort(key=lambda row: row["source_id"])
    return listed


def _source_outcome(state: Optional[str]) -> str:
    if state == health.HEALTHY or state == health.FAILING_ONCE:
        # One failure is shown on the source and does not fail the check.
        # Readiness treats it the same way.
        return PASS
    if state == health.REQUIRES_TEST:
        return NEEDS_A_PERSON
    if state == health.NOT_CHECKED:
        return NOT_CHECKED
    return FAIL


def _source_reason(row: dict) -> str:
    source_id = row["source_id"]
    state = row.get("state")
    reason = row.get("reason")
    if state == health.HEALTHY:
        text = "healthy"
    elif state == health.FAILING_ONCE:
        text = "failing once"
        if reason:
            text = f"{text}. {reason}"
    elif state == health.NO_ATTEMPT:
        text = "no attempt yet"
    elif state == health.REQUIRES_TEST:
        text = reason or "requires a test"
    elif state == health.NOT_CHECKED:
        text = reason or "not checked"
    elif state == health.UNKNOWN:
        text = reason or "unknown"
    elif reason:
        text = reason
    else:
        text = str(state or "unhealthy").replace("_", " ")
    return f"{source_id}: {text}."


def _source_fix(state: Optional[str], reason: Optional[str]) -> Optional[str]:
    if state == health.NOT_CHECKED:
        if reason == health.LEGACY_REASON:
            return "Switch on scheduled collection."
        return None
    if state == health.NO_ATTEMPT:
        return "It has not succeeded since it was switched on."
    if state == health.REQUIRES_TEST:
        return "Confirm the credential and its read access, then test it."
    if state == health.UNKNOWN:
        return "Its health record has not been updated."
    return "Check the credential and the endpoint."


def _brings(kind: Optional[str]) -> str:
    if kind in _ALERT_KINDS:
        return "alerts"
    if kind == "detection_source":
        return "rules"
    return "lookups"


def _evidence(source: Optional[dict], fix: Optional[str]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    if fix:
        evidence["fix"] = fix
    if not source:
        return evidence
    record = source.get("record") or {}
    evidence.update(
        {
            "source_id": source.get("source_id"),
            "brings": _brings(source.get("kind")),
            "state": source.get("state"),
            "last_data": (
                _iso(record.get("last_data_at"))
                if isinstance(record.get("last_data_at"), datetime)
                else None
            ),
            "labels": _labels(record.get("labels")),
        }
    )
    return evidence


def _labels(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        return sorted(str(key) for key in raw)
    if isinstance(raw, list):
        return [str(key) for key in raw]
    return []


def _check(
    check_id: str,
    outcome: str,
    reason: str,
    *,
    fix: Optional[str] = None,
    source: Optional[dict] = None,
) -> dict[str, Any]:
    spec_for(check_id)  # a produced id has to be one the registry knows
    return {
        "id": check_id,
        "module": VISIBILITY.id,
        "outcome": outcome,
        "reason": reason,
        "evidence": _evidence(source, fix),
    }


def _from_item(
    check_id: str, outcome: str, item: dict, source: Optional[dict] = None
) -> dict[str, Any]:
    return _check(
        check_id,
        outcome,
        item.get("text") or "",
        fix=item.get("fix"),
        source=source,
    )


def _by_id(items: list[dict], item_id: str) -> Optional[dict]:
    return next((item for item in items if item.get("id") == item_id), None)


def _actor_name(session: Session, actor_id: Optional[str]) -> Optional[str]:
    if not actor_id:
        return None
    row = (
        session.query(User.full_name, User.username)
        .filter(User.principal_id == actor_id)
        .first()
    )
    if row is None:
        return None
    return row.full_name or row.username


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _parse(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
