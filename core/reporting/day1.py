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

from core.audit.retention import view as retention_view
from core.auth import mfa_coverage, security_keys, session_policy
from core.auth.permissions import holders, missing_permission
from core.config import is_demo_mode
from core.integrations import health
from core.integrations.readiness import DESKTOP_LINE, READY, UNAVAILABLE, UNKNOWN
from core.platform import notices
from core.reporting.checks import COVERAGE, MODULES, SETTINGS, VISIBILITY, spec_for
from core.reporting.coverage import produce as produce_coverage
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
    """Each module that is switched on.

    A listed id is left out, and ``modules`` names only what ran. Daemon,
    model and collection come from ``state``, daemon first. Sources come
    from integration health. A failed read is one not-checked check, and the
    report still completes. Settings and coverage are read on a run that
    includes them, including unknown readiness and the desktop app. Coverage
    is the existing coverage, gap and rule-check reads. Demo findings are
    not read.
    """
    disabled = disabled_module_ids()
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    modules: list[str] = []
    if VISIBILITY.id not in disabled:
        modules.append(VISIBILITY.id)
        checks.extend(_visibility_checks(state, evidence))
    if SETTINGS.id not in disabled:
        modules.append(SETTINGS.id)
        checks.extend(_settings_checks())
    if COVERAGE.id not in disabled:
        modules.append(COVERAGE.id)
        checks.extend(_coverage_checks())
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


def _coverage_checks() -> list[dict[str, Any]]:
    try:
        return produce_coverage()
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read detection coverage: %s", type(exc).__name__
        )
        return [
            _check(
                "coverage.technique",
                NOT_CHECKED,
                "Could not read detection coverage.",
            )
        ]


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
    prior = (
        _visible_checks(session, user_id, compared, hidden)
        if compared is not None
        else {}
    )
    checks = []
    for check in record.checks or []:
        if check.get("module") in hidden or not _allowed(session, user_id, check):
            continue
        shown = _annotate(
            {
                "id": check.get("id"),
                "module": check.get("module"),
                "outcome": check.get("outcome"),
                "reason": check.get("reason") or "",
                "evidence": dict(check.get("evidence") or {}),
            }
        )
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


def _allowed(session: Session, user_id: str, check: dict) -> bool:
    """Settings checks follow their own permission. The stored row stays."""
    if check.get("module") != SETTINGS.id:
        return True
    try:
        spec = spec_for(str(check.get("id") or ""))
    except KeyError:
        return False
    return missing_permission(user_id, spec.permission, session) is None


def _annotate(check: dict) -> dict:
    """Value checks carry the registry citation. It is not stored on the record."""
    try:
        spec = spec_for(str(check.get("id") or ""))
    except KeyError:
        return check
    if spec.cited_value is None:
        return check
    return {
        **check,
        "cited_value": spec.cited_value,
        "source_id": spec.source_id,
        "clause": spec.clause,
        "link": spec.link,
        "date_checked": spec.date_checked,
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


def _visible_checks(
    session: Session, user_id: str, record: ReportRecord, hidden: set[str]
) -> dict[str, dict]:
    return {
        check.get("id"): check
        for check in record.checks or []
        if check.get("module") not in hidden and _allowed(session, user_id, check)
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
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    spec = spec_for(check_id)  # a produced id has to be one the registry knows
    evidence = _evidence(source, fix)
    for key, value in (extra or {}).items():
        if value is None or value == [] or value == "":
            continue
        evidence[key] = value
    return {
        "id": check_id,
        "module": spec.module,
        "outcome": outcome,
        "reason": reason,
        "evidence": evidence,
    }


_AT = "at the cited value"
_STRICTER = "stricter than the cited value"
_WEAKER = "weaker than the cited value"
_SESSIONS_SCREEN = "/settings?section=sessions"
_AUDIT_SCREEN = "/settings?section=audit"
# One year is 365 days, the same length as the retention minimum.
_THREE_YEARS = 365 * 3
_UNREADABLE_SESSION = "Could not read the session settings."
_HOSTNAME = (
    "Set VIGIL_CONSOLE_HOSTNAME and VIGIL_COOKIE_SECURE in Compose's .env, "
    "or in the Helm chart's config, then restart the API."
)
_KEY_HOLDER = "A person who holds a key has to raise this."

_SESSION_IDS = (
    "settings.idle",
    "settings.length",
    "settings.lockout",
    "settings.dormancy",
    "settings.unused",
    "settings.mfa_privileged",
    "settings.phishing_resistant",
    "settings.mfa_everyone",
)


def _settings_checks() -> list[dict[str, Any]]:
    return [*_session_checks(), _retention_check()]


def _session_checks() -> list[dict[str, Any]]:
    """Eight session checks from the stored row. Defaults are not a substitute."""
    try:
        with unit_of_work() as session:
            policy = session_policy.read_row(session)[0].policy
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read session settings: %s", type(exc).__name__
        )
        return [
            _check(
                check_id,
                NOT_CHECKED,
                _UNREADABLE_SESSION,
                extra={"screen": _SESSIONS_SCREEN},
            )
            for check_id in _SESSION_IDS
        ]
    return _judge_session(policy, _coverage(policy))


def _coverage(policy) -> dict[str, str]:
    """Counts only. A failed count leaves the outcome to the setting."""
    try:
        with unit_of_work() as session:
            level = policy.require_phishing_resistant
            if level == "nobody":
                level = "privileged"
            return {
                "privileged": _accounts(
                    mfa_coverage.without_mfa(session, "privileged"), "MFA"
                ),
                "everyone": _accounts(
                    mfa_coverage.without_mfa(session, "everyone"), "MFA"
                ),
                "keys": _accounts(
                    mfa_coverage.without_key(session, level), "security key"
                ),
            }
    except Exception as exc:  # noqa: BLE001 - the setting is still judged
        logger.error(
            "Day 1 Report could not count MFA coverage: %s", type(exc).__name__
        )
        return {}


def _accounts(count: int, gap: str) -> str:
    if count == 1:
        return f"1 account has no {gap}."
    return f"{count} accounts have no {gap}."


def _judge_session(policy, coverage: dict[str, str]) -> list[dict[str, Any]]:
    idle = _ceiling(policy.idle_standard_minutes, 15)
    length = _ceiling(policy.absolute_hours, 12)
    lock = (
        (PASS, _AT)
        if policy.lockout_threshold == 3 and not policy.lockout_timed
        else (FAIL, _WEAKER)
    )
    dormancy = _optional_ceiling(policy.dormancy_days, 45)
    unused = _optional_ceiling(policy.token_unused_days, 45)
    privileged = _level(policy.require_mfa, "privileged", "everyone")
    keys = _level(policy.require_phishing_resistant, "privileged", "everyone")
    everyone = _level(policy.require_mfa, "everyone", None)
    rows = (
        ("settings.idle", idle, _idle_reason(policy), None),
        ("settings.length", length, _length_reason(policy), None),
        ("settings.lockout", lock, _lockout_reason(policy), None),
        ("settings.dormancy", dormancy, _dormancy_reason(policy), None),
        ("settings.unused", unused, _unused_reason(policy), None),
        (
            "settings.mfa_privileged",
            privileged,
            _mfa_reason(policy.require_mfa, "privileged"),
            coverage.get("privileged"),
        ),
        (
            "settings.phishing_resistant",
            keys,
            _key_reason(policy.require_phishing_resistant),
            coverage.get("keys"),
        ),
        (
            "settings.mfa_everyone",
            everyone,
            _mfa_reason(policy.require_mfa, "everyone"),
            coverage.get("everyone"),
        ),
    )
    checks = []
    for check_id, judged, reason, count in rows:
        outcome, comparison = judged
        extra: dict[str, Any] = {"comparison": comparison, "screen": _SESSIONS_SCREEN}
        if count:
            extra["coverage"] = count
        if check_id == "settings.phishing_resistant" and outcome == FAIL:
            extra["prerequisites"] = _key_prerequisites()
        checks.append(_check(check_id, outcome, reason, extra=extra))
    return checks


def _retention_check() -> dict[str, Any]:
    """The period in force. A pending period, and no period, are not in force."""
    try:
        with unit_of_work() as session:
            days = retention_view(session, datetime.now(timezone.utc))["effective_days"]
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read audit retention: %s", type(exc).__name__
        )
        return _check(
            "settings.retention",
            NOT_CHECKED,
            "Could not read the audit-trail retention period.",
            extra={"screen": _AUDIT_SCREEN},
        )
    if days is None:
        outcome, comparison = FAIL, _WEAKER
        reason = "Audit-trail retention: no period is in force."
    else:
        outcome, comparison = _floor(int(days), _THREE_YEARS)
        reason = f"Audit-trail retention: the trail is kept for {int(days)} days."
    return _check(
        "settings.retention",
        outcome,
        reason,
        extra={"comparison": comparison, "screen": _AUDIT_SCREEN},
    )


def _ceiling(actual: int, limit: int) -> tuple[str, str]:
    """Shorter than the cited maximum is stricter."""
    if actual < limit:
        return PASS, _STRICTER
    if actual == limit:
        return PASS, _AT
    return FAIL, _WEAKER


def _floor(actual: int, limit: int) -> tuple[str, str]:
    """Longer than the cited minimum is stricter."""
    if actual > limit:
        return PASS, _STRICTER
    if actual == limit:
        return PASS, _AT
    return FAIL, _WEAKER


def _optional_ceiling(actual: Optional[int], limit: int) -> tuple[str, str]:
    if actual is None:
        return FAIL, _WEAKER
    return _ceiling(actual, limit)


def _level(actual: str, cited: str, above: Optional[str]) -> tuple[str, str]:
    if above is not None and actual == above:
        return PASS, _STRICTER
    if actual == cited:
        return PASS, _AT
    return FAIL, _WEAKER


def _idle_reason(policy) -> str:
    return (
        "Session inactivity: a standard sign-in ends after "
        f"{policy.idle_standard_minutes} minutes with no activity."
    )


def _length_reason(policy) -> str:
    return (
        "Session length: a sign-in ends after "
        f"{policy.absolute_hours} hours whether or not it is in use."
    )


def _lockout_reason(policy) -> str:
    lead = (
        f"Lockout: an account locks after {policy.lockout_threshold} "
        "failed sign-ins in a row"
    )
    if policy.lockout_timed:
        return f"{lead}, and the lock ends after {policy.lockout_minutes} minutes."
    return f"{lead} and stays locked until an administrator releases it."


def _dormancy_reason(policy) -> str:
    if policy.dormancy_days is None:
        return "Dormant accounts: idle accounts are not deactivated."
    return (
        "Dormant accounts: an account with no sign-in and no personal token use "
        f"for {policy.dormancy_days} days is deactivated."
    )


def _unused_reason(policy) -> str:
    if policy.token_unused_days is None:
        return "Unused credentials: unused tokens are not revoked."
    return f"Unused credentials: a token unused for {policy.token_unused_days} days is revoked."


def _mfa_reason(level: str, which: str) -> str:
    name = (
        "MFA for privileged accounts" if which == "privileged" else "MFA for everyone"
    )
    if level == "off":
        return f"{name}: sign-in does not require MFA."
    if level == "privileged":
        return f"{name}: privileged accounts must use MFA."
    return f"{name}: every person must use MFA."


def _key_reason(level: str) -> str:
    if level == "nobody":
        return "Phishing-resistant MFA: sign-in does not require a security key."
    if level == "privileged":
        return "Phishing-resistant MFA: privileged accounts must use a security key."
    return "Phishing-resistant MFA: every person must use a security key."


def _key_prerequisites() -> list[str]:
    lines = []
    reason = security_keys.availability().reason
    if reason:
        lines.append(reason)
    lines.append(_HOSTNAME)
    lines.append(_KEY_HOLDER)
    return lines


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
