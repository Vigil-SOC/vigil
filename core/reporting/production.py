"""Production Start, and the one Production Start Report.

``production.start`` holds ``started_at`` (the moment they name) and
``declared_at`` (when this server wrote it). Clearing stores both as null.
Who wrote it is only the ``config.change`` audit's ``changed_by``.

The report is one automatic run, trigger ``production``, at the first
readiness tick at or after that start plus 48 hours. ``production_on_demand``
is a person's run and does not count as the automatic one. The sweep calls
:func:`consider` under the same lock as Day 1. This module imports Day 1;
Day 1 does not import it.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth.permissions import holders
from core.config import get_settings, is_demo_mode
from core.integrations.readiness import UNAVAILABLE, UNKNOWN
from core.platform import notices
from core.reporting.checks import (
    AGREEMENT,
    ALERTS,
    CHANGES,
    CONFIDENCE,
    MODULES,
    QUIET,
    RUNNING,
    SETTINGS,
    SPEND,
    TIME,
    TUNING,
)
from core.reporting.day1 import (
    FAIL,
    LOCK,
    NEEDS_A_PERSON,
    NOT_CHECKED,
    PASS,
    PREFACE,
    PRODUCTION,
    PRODUCTION_ON_DEMAND,
    _by_id,
    _check,
    _clear,
    _clock_start,
    _insert_record,
    _insert_start,
    _iso,
    _parse,
    _present,
)
from core.reporting.day1 import abandon as day1_abandon
from core.reporting.day1 import collect_settings, producing_ids
from core.reporting.enablement import disabled_module_ids
from core.reporting.production_agreement import agreement, confidence
from core.reporting.production_alerts import alerts as produce_alerts
from core.reporting.production_alerts import quiet as produce_quiet
from core.reporting.production_changes import present as present_changes
from core.reporting.production_changes import produce as produce_changes
from core.reporting.production_spend import spend as produce_spend
from core.reporting.production_time import report as time_report
from core.reporting.production_tuning import tuning as produce_tuning
from core.storage.config_service import get_config_service, get_session
from core.storage.models import ReportRecord, ReportRun
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

KEY = "production.start"
# A browser's "now" can sit a few seconds ahead of this server.
SKEW = timedelta(seconds=5)
DUE_AFTER = timedelta(hours=48)
_CONFIG_TYPE = "report"
_REPORT_TRIGGERS = (PRODUCTION, PRODUCTION_ON_DEMAND)
NOTICE_KIND = "production_report"
NOTICE_TITLE = "Production Start Report"
NOTICE_MESSAGE = "A Production Start Report is ready."
SERVICE_OFF = "not checked: background service off, nothing is collected"
TRIAGE_UNSET = "not checked: set in the background service's environment"

_guard = threading.Lock()
# The declared start a run used, kept until the record is written.
_used: dict[str, Optional[str]] = {}


class Rejected(Exception):
    """A time this server will not store. ``detail`` names the rule."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def read() -> dict[str, Optional[str]]:
    """The stored row, or both times null when it has not been written."""
    value = get_config_service().read_system_config(KEY)
    return _row(value if isinstance(value, dict) else None)


def declare(started_at: Optional[datetime], user_id: str) -> Optional[dict]:
    """Write the row as ``user_id``. None when the write failed.

    ``started_at`` of None clears both times. A time is accepted at or before
    now (plus a few seconds of skew) and not earlier than Day 1's clock start.
    ``declared_at`` is this server's now, except on a clear, when it is null.
    """
    if started_at is None:
        value = {"started_at": None, "declared_at": None}
        reason = "Production start cleared"
    else:
        started = _naive(started_at)
        now = utcnow()
        if started > now + SKEW:
            raise Rejected("started_at is in the future")
        with get_session() as session:
            earliest = _clock_start(session)
        if earliest is not None and started < _naive(earliest):
            raise Rejected(
                "started_at is earlier than when setup finished"
                " or the earliest person account was created"
            )
        value = {"started_at": _stamp(started), "declared_at": _stamp(now)}
        reason = "Production start declared"
    saved = get_config_service(user_id=user_id).set_system_config(
        key=KEY,
        value=value,
        description="When a person declared this install in production",
        config_type=_CONFIG_TYPE,
        change_reason=reason,
    )
    return value if saved else None


def _row(value: Optional[dict]) -> dict[str, Optional[str]]:
    if not value:
        return {"started_at": None, "declared_at": None}
    started = value.get("started_at")
    declared = value.get("declared_at")
    return {
        "started_at": started if isinstance(started, str) else None,
        "declared_at": declared if isinstance(declared, str) else None,
    }


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds") + "Z"


class NotInProduction(Exception):
    """On demand before a start is declared."""

    detail = "Not in production yet"


def consider(
    session: Session, now: Optional[datetime] = None
) -> Optional[tuple[str, str]]:
    """Start the one automatic run this tick owes, or nothing.

    The caller holds the advisory lock and commits. A null start, a start
    younger than 48 hours, or any ``production`` row already stored (finished
    or not) starts nothing. An on-demand row does not count.
    """
    now = now or utcnow()
    declared = read()
    started = _parse(declared["started_at"])
    if started is None:
        return None
    if _automatic_exists(session):
        return None
    if now < started + DUE_AFTER:
        return None
    run_id = _insert_start(session, PRODUCTION, None, now)
    _remember(run_id, declared["started_at"])
    return run_id, PRODUCTION


def begin_on_demand(actor_id: str) -> str:
    """Insert an on-demand start under the sweep's lock. The caller writes the record."""
    declared = read()
    if _parse(declared["started_at"]) is None:
        raise NotInProduction
    now = utcnow()
    run_id: Optional[str] = None
    try:
        with unit_of_work() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
            run_id = _insert_start(session, PRODUCTION_ON_DEMAND, actor_id, now)
            _remember(run_id, declared["started_at"])
        return run_id
    except Exception:
        if run_id is not None:
            abandon(run_id)
        raise


def abandon(run_id: str) -> None:
    """The start did not commit. It is not running."""
    _forget(run_id)
    day1_abandon(run_id)


def finish(run_id: str, trigger: str, actor_id: Optional[str], state: dict) -> str:
    """Build the record for a committed start and insert it."""
    try:
        started_at = _forget(run_id)
        if started_at is None:
            started_at = read()["started_at"]
        with unit_of_work() as session:
            run = session.get(ReportRun, run_id)
            window_end = run.started_at if run is not None else utcnow()
        built = build(state, started_at, window_end)
        return _insert_record(run_id, trigger, actor_id, built)
    finally:
        _clear(run_id)


def notify_finished(trigger: str, actor_id: Optional[str], record_id: str) -> None:
    """One link-only notice. Automatic runs go to platform.write holders;
    an on-demand run goes to the person who started it."""
    link = f"/report?record={record_id}"
    if trigger == PRODUCTION_ON_DEMAND and actor_id:
        recipients = [actor_id]
    else:
        recipients = holders("platform.write")
    for principal in recipients:
        try:
            notices.notify(
                principal, NOTICE_KIND, NOTICE_TITLE, NOTICE_MESSAGE, link=link
            )
        except Exception:  # noqa: BLE001 - the record is already stored
            logger.exception("production start report notice was not sent")


def build(
    state: dict, started_at: Optional[str], window_end: datetime
) -> dict[str, Any]:
    """Every built production module that is switched on.

    Each module returns its checks and, when it has figures, its evidence.
    One that is not built yet returns None and is skipped. A failed read is
    not-checked for that module only. Day 1's ``build`` does not call these.
    """
    end = _iso(window_end)
    evidence: dict[str, Any] = {
        "production": {
            "started_at": started_at,
            "window": {"start": started_at, "end": end},
        }
    }
    disabled = disabled_module_ids()
    checks: list[dict[str, Any]] = []
    modules: list[str] = []
    for module_id in MODULES:
        if not module_id.startswith("production."):
            continue
        if module_id in disabled:
            continue
        produced = _produce(module_id, state, started_at, window_end)
        if produced is None:
            continue
        module_checks, module_evidence = produced
        modules.append(module_id)
        checks.extend(module_checks)
        if module_evidence is not None:
            evidence[module_id] = module_evidence
    # The same settings checks Day 1 stores, under their existing ids.
    # ``settings`` is not a production module.
    if SETTINGS.id not in disabled:
        setting_checks, selection = collect_settings()
        checks.extend(setting_checks)
        evidence["selection"] = selection
    return {
        "modules": modules,
        "checks": checks,
        "evidence": evidence,
        "preface": PREFACE if is_demo_mode() else None,
    }


def _produce(
    module_id: str,
    state: dict,
    started_at: Optional[str],
    window_end: datetime,
) -> Optional[tuple[list[dict[str, Any]], Optional[dict[str, Any]]]]:
    """Checks and evidence for one module. None when it is not built yet."""
    if module_id == RUNNING.id:
        return _running_checks(state), None
    if module_id == QUIET.id:
        return produce_quiet(started_at, window_end)
    if module_id == ALERTS.id:
        return [], produce_alerts(started_at, window_end)
    if module_id == TUNING.id:
        return [], produce_tuning(started_at, window_end)
    if module_id == CONFIDENCE.id:
        return [], confidence(started_at, window_end)
    if module_id == AGREEMENT.id:
        return [], agreement(started_at, window_end)
    if module_id == SPEND.id:
        return [], produce_spend(started_at, window_end)
    if module_id == CHANGES.id:
        return [], produce_changes(started_at, window_end)
    if module_id == TIME.id:
        return [], time_report({"start": started_at, "end": _iso(window_end)})
    return None


def _running_checks(state: dict) -> list[dict[str, Any]]:
    return [
        _service_check(state),
        _triage_check(),
        _source_check(state),
    ]


def _service_check(state: dict) -> dict[str, Any]:
    status_name = state.get("state")
    if status_name == UNAVAILABLE or _stopped(state):
        return _check(
            "production.running.service",
            NOT_CHECKED,
            SERVICE_OFF,
            fix="Start the background service.",
        )
    if status_name == UNKNOWN:
        return _check(
            "production.running.service",
            NOT_CHECKED,
            state.get("line") or "Could not read the background service.",
        )
    daemon = _by_id(state.get("items") or [], "daemon")
    if daemon is not None:
        return _check(
            "production.running.service",
            NOT_CHECKED,
            daemon.get("text") or "The background service is not serving.",
            fix=daemon.get("fix"),
        )
    return _check(
        "production.running.service",
        PASS,
        "The background service is running.",
    )


def _stopped(state: dict) -> bool:
    daemon = _by_id(state.get("items") or [], "daemon")
    if daemon is None:
        return False
    return "stopped" in str(daemon.get("text") or "").lower()


def _triage_check() -> dict[str, Any]:
    settings = get_settings()
    if "daemon_auto_triage" not in settings.model_fields_set:
        return _check("production.running.triage", NOT_CHECKED, TRIAGE_UNSET)
    if settings.daemon_auto_triage:
        return _check(
            "production.running.triage",
            PASS,
            "Automatic triage is on.",
        )
    return _check(
        "production.running.triage",
        FAIL,
        "Automatic triage is off. Every alert waits, and none gets a first verdict.",
    )


def _source_check(state: dict) -> dict[str, Any]:
    """A switched-on alert source is the one Day 1 visibility already uses."""
    status_name = state.get("state")
    if status_name == UNKNOWN:
        return _check(
            "production.running.source",
            NOT_CHECKED,
            state.get("line") or "Could not read integration health.",
        )
    if status_name == UNAVAILABLE:
        return _check(
            "production.running.source",
            NOT_CHECKED,
            "Not judged on the desktop app.",
        )
    alert = _by_id(state.get("items") or [], "alert-source")
    if alert is not None:
        return _check(
            "production.running.source",
            NEEDS_A_PERSON,
            alert.get("text") or "No alert source switched on.",
            fix=alert.get("fix"),
        )
    return _check(
        "production.running.source",
        PASS,
        "An alert source is switched on.",
    )


def status() -> dict[str, Any]:
    """Home's line for this report: a run being written, an unfinished automatic start, the latest."""
    running = producing_ids()
    with unit_of_work() as session:
        open_runs = (
            session.query(ReportRun)
            .outerjoin(ReportRecord, ReportRecord.run_id == ReportRun.run_id)
            .filter(
                ReportRecord.record_id.is_(None),
                ReportRun.trigger.in_(_REPORT_TRIGGERS),
            )
            .order_by(ReportRun.started_at.desc())
            .all()
        )
        latest = (
            session.query(ReportRecord)
            .filter(ReportRecord.trigger.in_(_REPORT_TRIGGERS))
            .order_by(ReportRecord.recorded_at.desc())
            .first()
        )
        running_since = next(
            (row.started_at for row in open_runs if row.run_id in running), None
        )
        incomplete_since = next(
            (
                row.started_at
                for row in open_runs
                if row.trigger == PRODUCTION and row.run_id not in running
            ),
            None,
        )
        facts = (latest.evidence or {}).get("production") if latest else None
        used = facts.get("started_at") if isinstance(facts, dict) else None
        return {
            "running_since": _iso(running_since),
            "incomplete_since": _iso(incomplete_since),
            "latest_id": latest.record_id if latest else None,
            "latest_at": _iso(latest.recorded_at) if latest else None,
            "latest_started_at": used if isinstance(used, str) else None,
        }


def read_record(user_id: str, record_id: Optional[str]) -> Optional[dict[str, Any]]:
    """One production record, or the latest. Sections the caller cannot read are left out."""
    with unit_of_work() as session:
        if record_id:
            record = session.get(ReportRecord, record_id)
        else:
            record = (
                session.query(ReportRecord)
                .filter(ReportRecord.trigger.in_(_REPORT_TRIGGERS))
                .order_by(ReportRecord.recorded_at.desc())
                .first()
            )
        if record is None or record.trigger not in _REPORT_TRIGGERS:
            return None
        run = session.get(ReportRun, record.run_id)
        started = run.started_at if run is not None else record.recorded_at
        body = _present(session, record, started, user_id)
    return _gate_changes(user_id, body)


def _gate_changes(user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Apply audit, findings and integrations gates inside production.changes.

    The module itself is not withheld: a missing gate names what is left out
    and the other items stay.
    """
    evidence = body.get("evidence")
    if not isinstance(evidence, dict):
        return body
    if CHANGES.id not in (body.get("modules") or []) and CHANGES.id not in evidence:
        return body
    stored = evidence.get(CHANGES.id)
    evidence[CHANGES.id] = present_changes(
        user_id, stored if isinstance(stored, dict) else {}
    )
    return body


def _automatic_exists(session: Session) -> bool:
    return (
        session.query(ReportRun.run_id).filter(ReportRun.trigger == PRODUCTION).first()
        is not None
    )


def _remember(run_id: str, started_at: Optional[str]) -> None:
    with _guard:
        _used[run_id] = started_at


def _forget(run_id: str) -> Optional[str]:
    with _guard:
        return _used.pop(run_id, None)
