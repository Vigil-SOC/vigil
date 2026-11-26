"""Day 1 Report: when a run starts, what it stores, and how it is read.

The readiness sweep calls :func:`consider` inside its advisory lock.
A start is committed there, and the record is inserted afterwards, so a
start this process is still writing shows as running and a start left
behind shows as not completed. That start does not start another automatic
run. On demand takes the same lock.

After the first readiness or fallback record, the report runs again on a
cadence (``recurring``). A recurring run is read against the run before it,
and notifies only when a settings check that passed now fails.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from core.audit.retention import view as retention_view
from core.auth import mfa_coverage, security_keys, session_policy
from core.auth.permissions import holders, missing_permission
from core.config import is_demo_mode
from core.integrations import health
from core.integrations.readiness import (
    DESKTOP_LINE,
    READY,
    UNAVAILABLE,
    UNKNOWN,
    evaluate,
)
from core.llm import residency, statements
from core.platform import notices
from core.platform.process_health import ProcessHealth
from core.reporting.capped_recheck_report import present as present_capped
from core.reporting.capped_recheck_report import produce as produce_capped
from core.reporting.checks import (
    CHANGES,
    COVERAGE,
    ESTATE,
    FORECAST,
    HISTORY,
    HISTORY_CAPPED,
    LEGACY_CITATIONS,
    MODULES,
    SETTINGS,
    SITE,
    VISIBILITY,
    ScopedValue,
    applicable,
    resolve,
    scope_label,
    spec_for,
)
from core.reporting.coverage import by_layer as coverage_by_layer
from core.reporting.coverage import produce as produce_coverage
from core.reporting.enablement import (
    RECURRING_KEY,
    disabled_module_ids,
    parse_recurring,
)
from core.reporting.estate import produce as produce_estate
from core.reporting.forecast import failed as forecast_failed
from core.reporting.forecast import produce as produce_forecast
from core.reporting.history_sections import present as present_history
from core.reporting.history_sections import produce as produce_history
from core.reporting.selection import SelectionError, SelectionView, read_view
from core.reporting.site_report import CHECK_IDS as SITE_CHECKS
from core.reporting.site_report import produce as produce_site
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
RECURRING = "recurring"
# The Production Start Report uses the same tables. Day 1 readers ignore both.
PRODUCTION = "production"
PRODUCTION_ON_DEMAND = "production_on_demand"
DAY1_TRIGGERS = (READINESS, FALLBACK, ON_DEMAND, RECURRING)

PASS = "pass"
FAIL = "fail"
NOT_CHECKED = "not_checked"
NEEDS_A_PERSON = "needs_a_person"
# No cited value for the selection. Not a Check Outcome.
NO_VALUE = "no_value"

PREFACE = "This report describes the install, not the demo data."
NOTICE_KIND = "day1_report"
NOTICE_TITLE = "Day 1 Report"
NOTICE_MESSAGE = "A Day 1 Report is ready."
RECURRING_MESSAGE = (
    "A setting checked by the Day 1 Report got worse since the last run."
)

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
    starts nothing. Once either has a record, a recurring run falls due one
    interval after the latest record or recurring start.
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


def finish(
    run_id: str,
    trigger: str,
    actor_id: Optional[str],
    state: dict,
    gateway: Optional[tuple] = None,
) -> str:
    """Build the record for a committed start and insert it. Clears the running mark.

    ``gateway`` is :func:`read_gateway`'s answer, read on the event loop.
    """
    try:
        built = build(state, gateway)
        return _insert_record(run_id, trigger, actor_id, built)
    finally:
        _clear(run_id)


def notify_finished(trigger: str, actor_id: Optional[str], record_id: str) -> None:
    """One link-only notice. Automatic runs go to platform.write holders;
    an on-demand run goes to the person who started it. No section content.
    A recurring run notifies only when a settings check regressed."""
    link = f"/report?record={record_id}"
    message = NOTICE_MESSAGE
    if trigger == RECURRING:
        try:
            with unit_of_work() as session:
                record = session.get(ReportRecord, record_id)
                regressed = _regressed(session, record) if record is not None else []
        except Exception:  # noqa: BLE001 - the record is stored; the tick goes on
            logger.exception("day 1 report could not compare for a notice")
            return
        # Other modules show as worse on the page. Medic and the gaps run notify on those.
        if not any(check.get("module") == SETTINGS.id for check in regressed):
            return
        message = RECURRING_MESSAGE
    if trigger == ON_DEMAND and actor_id:
        recipients = [actor_id]
    else:
        recipients = holders("platform.write")
    for principal in recipients:
        try:
            notices.notify(principal, NOTICE_KIND, NOTICE_TITLE, message, link=link)
        except Exception:  # noqa: BLE001 - the record is already stored
            logger.exception("day 1 report notice was not sent")


async def run_on_demand(
    actor_id: str,
    processes: ProcessHealth,
    begun: Optional[asyncio.Future] = None,
) -> str:
    """Run the Day 1 Report now as ``actor_id``, tell them, and return the record id.

    ``begun`` gets the run id once the start is committed, for a caller that
    does not wait for the record. A failure after that leaves the start, which
    Home shows as not completed.
    """
    state = await evaluate(processes)
    run_id = await asyncio.to_thread(begin_on_demand, actor_id)
    if begun is not None and not begun.done():
        begun.set_result(run_id)
    gateway = await read_gateway()
    record_id = await asyncio.to_thread(
        finish, run_id, ON_DEMAND, actor_id, state, gateway
    )
    await asyncio.to_thread(notify_finished, ON_DEMAND, actor_id, record_id)
    return record_id


async def read_gateway() -> tuple:
    """Where gateway providers' calls go, and the Vertex region hash, for the
    model check. Read here on the event loop: the shared routability snapshot
    can't be awaited from the report's thread."""
    try:
        return (
            await residency.gateway_residency(),
            await statements.vertex_region_hash(),
        )
    except Exception as exc:  # noqa: BLE001 - the check reads it as unknown
        logger.error("Day 1 Report could not read the gateway: %s", type(exc).__name__)
        return None, None


def build(state: dict, gateway: Optional[tuple] = None) -> dict[str, Any]:
    """Each module that is switched on.

    A listed id is left out, and ``modules`` names only what ran. Daemon,
    model and collection come from ``state``, daemon first. Sources come
    from integration health. A failed read is one not-checked check, and the
    report still completes. Settings and coverage are read on a run that
    includes them, including unknown readiness and the desktop app. Coverage
    is the existing coverage, gap and rule-check reads. Demo findings are
    not read. Host counts for CrowdStrike, Defender and Wazuh follow coverage,
    and only for a tool that is switched on. That read is the same health
    result visibility uses.
    """
    disabled = disabled_module_ids()
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    modules: list[str] = []
    sources: Optional[list] = None
    health_failed = False
    if {VISIBILITY.id, ESTATE.id, SITE.id} - disabled:
        try:
            sources = health.collect()
        except Exception as exc:  # noqa: BLE001 - the report still completes
            health_failed = True
            logger.error("Day 1 Report could not read health: %s", type(exc).__name__)
    if VISIBILITY.id not in disabled:
        modules.append(VISIBILITY.id)
        checks.extend(_visibility_checks(state, evidence, sources, health_failed))
    view, selection_problem = _read_selection()
    evidence["selection"] = _selection_snapshot(view, selection_problem)
    if SETTINGS.id not in disabled:
        modules.append(SETTINGS.id)
        if selection_problem or view is None:
            checks.extend(
                _unchecked_settings(
                    selection_problem or "not checked: the selection could not be read."
                )
            )
        else:
            checks.extend(_settings_checks(view, gateway or (None, None)))
    if COVERAGE.id not in disabled:
        modules.append(COVERAGE.id)
        checks.extend(_coverage_checks())
        evidence[COVERAGE.id] = _coverage_layers()
    if ESTATE.id not in disabled:
        modules.append(ESTATE.id)
        checks.extend(produce_estate(sources, health_failed))
    if SITE.id not in disabled:
        modules.append(SITE.id)
        checks.extend(_site(sources, health_failed, evidence))
    _append_history(modules, evidence, disabled)
    _append_capped(modules, evidence, disabled)
    if FORECAST.id not in disabled:
        modules.append(FORECAST.id)
        evidence[FORECAST.id] = _forecast(state)
    preface = PREFACE if is_demo_mode() else None
    return {
        "modules": modules,
        "checks": checks,
        "evidence": evidence,
        "preface": preface,
    }


def _site(
    sources: Optional[list], health_failed: bool, evidence: dict[str, Any]
) -> list[dict[str, Any]]:
    """The declared site against what Wazuh, CrowdStrike and Defender list.
    A failure is not checked."""
    try:
        checks, evidence[SITE.id] = produce_site(sources, health_failed)
        return checks
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error("Day 1 Report could not read the site: %s", type(exc).__name__)
        evidence[SITE.id] = {"not_checked": "not checked: could not read the site."}
        return [
            _check(check_id, NOT_CHECKED, "not checked: could not read the site.")
            for check_id in SITE_CHECKS
        ]


def _visibility_checks(
    state: dict,
    evidence: dict[str, Any],
    sources: Optional[list],
    health_failed: bool,
) -> list[dict[str, Any]]:
    checks = _platform_checks(state)
    if health_failed or sources is None:
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


def _append_history(
    modules: list[str], evidence: dict[str, Any], disabled: set[str]
) -> None:
    """The three history sections. A failed read is not-checked on each of them."""
    pending = [module.id for module in HISTORY if module.id not in disabled]
    if not pending:
        return
    try:
        produced = produce_history()
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read imported history: %s", type(exc).__name__
        )
        produced = {
            module_id: {"not_checked": "not checked: could not read imported history."}
            for module_id in pending
        }
    for module_id in pending:
        modules.append(module_id)
        evidence[module_id] = produced[module_id]


def _append_capped(
    modules: list[str], evidence: dict[str, Any], disabled: set[str]
) -> None:
    """The capped re-check. A failed read is not-checked here, and the report completes."""
    if HISTORY_CAPPED.id in disabled:
        return
    modules.append(HISTORY_CAPPED.id)
    try:
        evidence[HISTORY_CAPPED.id] = produce_capped()
    except Exception as exc:  # noqa: BLE001 - this module only
        logger.error(
            "Day 1 Report could not read the capped re-check: %s", type(exc).__name__
        )
        evidence[HISTORY_CAPPED.id] = {
            "not_checked": "not checked: could not read the capped re-check."
        }


def _forecast(state: dict) -> dict[str, Any]:
    """The cost forecast. A failure here is not checked; the report still completes."""
    try:
        return produce_forecast(state)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Day 1 Report could not build the cost forecast: %s", type(exc).__name__
        )
        return forecast_failed("not checked: could not build the cost forecast.")


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


def _coverage_layers() -> dict[str, Any]:
    """The by-layer figures. A failed read is a not-checked note, not a check."""
    try:
        return coverage_by_layer()
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read coverage by layer: %s", type(exc).__name__
        )
        return {"not_checked": "not checked: could not read coverage by layer."}


# A cell's state, worst first. not_checked is not on the scale. A tested-and-
# missed cell reads no_detection, so it is a regression from any catch.
_LAYER_RANK = {"no_detection": 0, "present": 1, "partial": 2, "validated": 3}


def _layers_comparison(current: Any, prior: Any) -> Any:
    """Each (technique, layer) cell against the compared run's. Read, never stored.

    ``not_in_compared_run`` when the compared record has no by-layer body. A
    cell either side could not check, or that only one side has, is not compared,
    and so is every cell when the compared run's read failed.
    """
    if not isinstance(current, dict) or not isinstance(current.get("techniques"), list):
        return None
    if not isinstance(prior, dict):
        return "not_in_compared_run"
    before = {
        (row.get("technique"), layer): cell
        for row in prior.get("techniques") or []
        for layer, cell in (row.get("layers") or {}).items()
    }
    counts = {"improved": 0, "regressed": 0, "unchanged": 0, "not_compared": 0}
    moves = []
    for row in current["techniques"]:
        for layer, cell in (row.get("layers") or {}).items():
            was = before.get((row.get("technique"), layer)) or {}
            now_rank = _LAYER_RANK.get(cell.get("state"))
            was_rank = _LAYER_RANK.get(was.get("state"))
            if now_rank is None or was_rank is None:
                counts["not_compared"] += 1
                continue
            if now_rank == was_rank:
                counts["unchanged"] += 1
                continue
            kind = "improved" if now_rank > was_rank else "regressed"
            counts[kind] += 1
            moves.append(
                {
                    "technique": row.get("technique"),
                    "name": row.get("name"),
                    "layer": layer,
                    "kind": kind,
                    "before": was["state"],
                    "after": cell["state"],
                }
            )
    return {"counts": counts, "moves": moves}


def status() -> dict[str, Any]:
    """Home's line: a run this process is writing, a start with no record, the latest."""
    running = producing_ids()
    with unit_of_work() as session:
        open_runs = (
            session.query(ReportRun)
            .outerjoin(ReportRecord, ReportRecord.run_id == ReportRun.run_id)
            .filter(
                ReportRecord.record_id.is_(None),
                ReportRun.trigger.in_(DAY1_TRIGGERS),
            )
            .order_by(ReportRun.started_at.desc())
            .all()
        )
        latest = (
            session.query(ReportRecord)
            .filter(ReportRecord.trigger.in_(DAY1_TRIGGERS))
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
            if record is not None and record.trigger not in DAY1_TRIGGERS:
                record = None
        else:
            record = (
                session.query(ReportRecord)
                .filter(ReportRecord.trigger.in_(DAY1_TRIGGERS))
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
        if module_id != CHANGES.id
        and missing_permission(user_id, MODULES[module_id].permission, session)
        is not None
    ]
    hidden = set(withheld)
    # A production record stays off _compared. Its settings checks are read
    # against the Day 1 anchor; nothing else on it is compared.
    production_run = record.trigger in (PRODUCTION, PRODUCTION_ON_DEMAND)
    anchor = None
    compared = None
    if production_run:
        anchor, _mode = day1_anchor(session, _production_started(record.evidence or {}))
    else:
        compared = _compared(session, record)
    prior_record = anchor if anchor is not None else compared
    prior = (
        _visible_checks(session, user_id, prior_record, hidden)
        if prior_record is not None
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
                # Kept off the response model. _annotate reads it, then the
                # model drops it. Absent on a record from before scoped values.
                "citation": check.get("citation"),
            }
        )
        if compared is not None or (
            anchor is not None and check.get("module") == SETTINGS.id
        ):
            baseline = anchor if anchor is not None else compared
            shown["comparison"] = _comparison(
                check,
                prior.get(check.get("id")),
                (record.evidence or {}).get("selection"),
                (
                    (baseline.evidence or {}).get("selection")
                    if baseline is not None
                    else None
                ),
            )
        checks.append(shown)
    # A recurring record says when a section the reader can't see got worse.
    # The check is never named.
    hidden_worse = record.trigger == RECURRING and any(
        check.get("module") in hidden or not _allowed(session, user_id, check)
        for check in _regressed(session, record, compared)
    )
    not_run = (
        [m for m in compared.modules or [] if m not in (record.modules or [])]
        if compared is not None
        else []
    )
    evidence = record.evidence or {}
    switched_off = []
    if VISIBILITY.id not in hidden:
        switched_off = (evidence.get(VISIBILITY.id) or {}).get("switched_off") or []
    forecast = None
    if FORECAST.id not in hidden:
        stored = evidence.get(FORECAST.id)
        if isinstance(stored, dict):
            forecast = stored
    coverage_comparison = None
    if compared is not None and COVERAGE.id not in hidden:
        coverage_comparison = _layers_comparison(
            evidence.get(COVERAGE.id), (compared.evidence or {}).get(COVERAGE.id)
        )
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
        "compared_at": (
            _iso(prior_record.recorded_at) if prior_record is not None else None
        ),
        "not_run": not_run,
        "hidden_worse": hidden_worse,
        "checks": checks,
        "selection": _selection_public(evidence.get("selection")),
        "evidence": _readable_evidence(session, user_id, record, hidden),
        "switched_off": switched_off,
        "forecast": forecast,
        "history_capped_recheck": present_capped(
            session,
            evidence,
            list(record.modules or []),
            hidden,
            record.recorded_at,
            user_id,
        ),
        "coverage_comparison": coverage_comparison,
        **present_history(
            session,
            evidence,
            list(record.modules or []),
            hidden,
            record.recorded_at,
            record.record_id,
            record.run_id,
            user_id,
        ),
    }


def _readable_evidence(
    session: Session, user_id: str, record: ReportRecord, hidden: set[str]
) -> dict[str, Any]:
    """Evidence for modules the caller may read. The rest is omitted.

    Visibility, the history sections and the forecast are already returned as
    their own fields. This is the remainder, plus the production window.
    """
    stored = record.evidence or {}
    shown: dict[str, Any] = {}
    for key, body in stored.items():
        if not isinstance(key, str):
            continue
        if key == "production":
            if missing_permission(user_id, "platform.read", session) is None:
                shown[key] = body
            continue
        # Item gates are applied in present(), not by withholding the module.
        if key == CHANGES.id:
            shown[key] = body
            continue
        module = MODULES.get(key)
        if module is None or key in hidden:
            continue
        if key in (VISIBILITY.id, FORECAST.id) or key.startswith("history."):
            continue
        if missing_permission(user_id, module.permission, session) is not None:
            continue
        shown[key] = body
    return shown


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
    """A stored citation wins. A record with none keeps the legacy snapshot.

    The live registry is not consulted: an upgrade must not rewrite an old report.
    """
    stored = check.get("citation")
    if isinstance(stored, dict):
        return {
            **check,
            "cited_value": stored.get("cited_value"),
            "source_id": stored.get("source_id"),
            "clause": stored.get("clause"),
            "link": stored.get("link"),
            "date_checked": stored.get("date_checked"),
            "scope": stored.get("scope"),
            "proposed": list(stored.get("proposed") or []),
            "gap": stored.get("gap"),
            "window": stored.get("window"),
        }
    legacy = LEGACY_CITATIONS.get(str(check.get("id") or ""))
    if legacy is None:
        return check
    return {**check, **legacy}


def _production_started(evidence: Any) -> Optional[datetime]:
    if not isinstance(evidence, dict):
        return None
    production = evidence.get("production")
    if not isinstance(production, dict):
        return None
    return _parse(production.get("started_at"))


def day1_anchor(
    session: Session, started_at: Optional[datetime]
) -> tuple[Optional[ReportRecord], str]:
    """The Day 1 record a production report is read against. Not ``_compared``.

    The latest readiness, fallback, or on-demand record at or before
    ``started_at``. When every Day 1 record is later, the earliest, and the
    mode is ``before``. ``none`` when there is no Day 1 record.
    """
    # Recurring runs are Day 1 records, and they are not this anchor.
    day1 = ReportRecord.trigger.in_((READINESS, FALLBACK, ON_DEMAND))
    if started_at is not None:
        latest = (
            session.query(ReportRecord)
            .filter(day1, ReportRecord.recorded_at <= started_at)
            .order_by(ReportRecord.recorded_at.desc())
            .first()
        )
        if latest is not None:
            return latest, "at"
    earliest = (
        session.query(ReportRecord)
        .filter(day1)
        .order_by(ReportRecord.recorded_at.asc())
        .first()
    )
    if earliest is None:
        return None, "none"
    return earliest, "before"


def collect_settings() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Settings checks and the selection snapshot they were scored against."""
    view, problem = _read_selection()
    snapshot = _selection_snapshot(view, problem)
    if problem or view is None:
        checks = _unchecked_settings(
            problem or "not checked: the selection could not be read."
        )
    else:
        checks = _settings_checks(view)
    return checks, snapshot


def _compared(session: Session, record: ReportRecord) -> Optional[ReportRecord]:
    """The run this record is read against. The earliest record has none.

    The latest completed record strictly earlier than this one whose trigger
    is readiness or fallback. When that record does not exist, the earliest
    completed record. A recurring record is read against the latest earlier
    Day 1 record of any trigger. Nothing is stored.
    """
    before = ReportRecord.recorded_at < record.recorded_at
    if record.trigger == RECURRING:
        return (
            session.query(ReportRecord)
            .filter(before, ReportRecord.trigger.in_(DAY1_TRIGGERS))
            .order_by(ReportRecord.recorded_at.desc())
            .first()
        )
    day1 = (
        session.query(ReportRecord)
        .filter(before, ReportRecord.trigger.in_((READINESS, FALLBACK)))
        .order_by(ReportRecord.recorded_at.desc())
        .first()
    )
    if day1 is not None:
        return day1
    # A production row is never the Day 1 comparison.
    return (
        session.query(ReportRecord)
        .filter(before, ReportRecord.trigger.in_(DAY1_TRIGGERS))
        .order_by(ReportRecord.recorded_at.asc())
        .first()
    )


_UNSET: Any = object()


def _regressed(
    session: Session, record: ReportRecord, compared: Any = _UNSET
) -> list[dict]:
    """Checks on the full record that passed on the compared run and now fail.

    A fail explained by a moved cited value or a changed selection is changed,
    not regressed. No permission filter: whether to notify is judged on the
    whole record.
    """
    if compared is _UNSET:
        compared = _compared(session, record)
    if compared is None:
        return []
    prior = {check.get("id"): check for check in compared.checks or []}
    current_selection = (record.evidence or {}).get("selection")
    prior_selection = (compared.evidence or {}).get("selection")
    regressed = []
    for check in record.checks or []:
        before = prior.get(check.get("id"))
        if before is None or before.get("outcome") != PASS:
            continue
        result = _comparison(check, before, current_selection, prior_selection)
        if result["kind"] == "new" and "change" not in result:
            regressed.append(check)
    return regressed


def _visible_checks(
    session: Session, user_id: str, record: ReportRecord, hidden: set[str]
) -> dict[str, dict]:
    return {
        check.get("id"): check
        for check in record.checks or []
        if check.get("module") not in hidden and _allowed(session, user_id, check)
    }


def _comparison(
    current: dict,
    prior: Optional[dict],
    current_selection: Any,
    prior_selection: Any,
) -> dict[str, Any]:
    """How ``current`` reads against the same check on the compared run.

    New and fixed only when both outcomes are pass or fail. ``no_value`` is
    neither: a move from fail to no cited value is not fixed. Anything else
    names both outcomes. When the outcome changed, ``change`` names a cited
    value that moved or a kind that was ticked or cleared. A check the
    compared run did not have, including one whose module was off, is not in
    that run. Evidence is the record's own, and only beside new, fixed, or
    unchanged.
    """
    if prior is None:
        return {"kind": "not_in_compared_run"}
    before = prior.get("outcome")
    after = current.get("outcome")
    if before == after and before in (PASS, FAIL, NO_VALUE):
        kind = "unchanged"
    elif before in (PASS, FAIL) and after in (PASS, FAIL):
        kind = "fixed" if before == FAIL else "new"
    else:
        kind = "named"
    result: dict[str, Any] = {"kind": kind}
    if kind in ("new", "fixed", "unchanged"):
        result["prior_evidence"] = dict(prior.get("evidence") or {})
    if kind == "named" and before in (
        PASS,
        FAIL,
        NOT_CHECKED,
        NEEDS_A_PERSON,
        NO_VALUE,
    ):
        result["prior_outcome"] = before
    if before != after and current.get("module") == SETTINGS.id:
        change = _change_detail(current, prior, current_selection, prior_selection)
        if change:
            result["change"] = change
    return result


def _change_detail(
    current: dict, prior: dict, current_selection: Any, prior_selection: Any
) -> Optional[str]:
    parts: list[str] = []
    moved = _value_move(current, prior)
    if moved:
        parts.append(moved)
    parts.extend(_kind_moves(current_selection, prior_selection))
    if not parts:
        return None
    return " ".join(parts)


def _citation_of(check: dict) -> Optional[dict]:
    """The stored citation, or the legacy snapshot a record before scoped values shows."""
    stored = check.get("citation")
    if isinstance(stored, dict):
        return stored
    return LEGACY_CITATIONS.get(str(check.get("id") or ""))


def _value_phrase(citation: dict) -> str:
    cited = citation.get("cited_value")
    if not cited:
        return "no cited value"
    source = citation.get("source_id")
    scope = citation.get("scope")
    if scope and source:
        return f"{cited} (cited for {scope} by {source})"
    if source:
        return f"{cited} (cited by {source})"
    return str(cited)


def _value_move(current: dict, prior: dict) -> Optional[str]:
    # A check that was not scored did not move its cited value.
    if current.get("outcome") == NOT_CHECKED or prior.get("outcome") == NOT_CHECKED:
        return None
    after = _citation_of(current)
    before = _citation_of(prior)
    if after is None or before is None:
        return None
    fields = ("cited_value", "source_id", "scope")
    # A legacy citation names no scope. Its value and source are compared alone.
    if after.get("scope") is None or before.get("scope") is None:
        fields = ("cited_value", "source_id")
    if all(after.get(field) == before.get(field) for field in fields):
        return None
    return (
        "The cited value moved from "
        f"{_value_phrase(before)} to {_value_phrase(after)}."
    )


def _kind_moves(current_selection: Any, prior_selection: Any) -> list[str]:
    if not isinstance(current_selection, dict) or not isinstance(prior_selection, dict):
        return []
    if current_selection.get("unreadable") or prior_selection.get("unreadable"):
        return []
    labels = {}
    labels.update(prior_selection.get("kind_labels") or {})
    labels.update(current_selection.get("kind_labels") or {})
    before = list(prior_selection.get("kinds") or [])
    after = list(current_selection.get("kinds") or [])
    lines = [
        f"Kind ticked: {labels.get(kind_id, kind_id)}."
        for kind_id in after
        if kind_id not in before
    ]
    lines.extend(
        f"Kind cleared: {labels.get(kind_id, kind_id)}."
        for kind_id in before
        if kind_id not in after
    )
    return lines


def _due(session: Session, readiness: Optional[str], now: datetime) -> Optional[str]:
    records = _triggers(session, ReportRecord)
    starts = _triggers(session, ReportRun)
    readiness_clear = READINESS not in records and READINESS not in starts
    if readiness == READY and readiness_clear:
        return READINESS
    if READINESS in records or FALLBACK in records:
        return RECURRING if _recurring_due(session, now) else None
    if not readiness_clear or FALLBACK in starts:
        return None
    anchor = _anchor_time(session)
    clock = _clock_start(session)
    if anchor is None or clock is None:
        return None
    if now >= max(anchor, clock) + FALLBACK_AFTER:
        return FALLBACK
    return None


def _recurring_due(session: Session, now: datetime) -> bool:
    """One interval after the latest Day 1 record or recurring start. 0 is off.

    A recurring start left without a record counts, so it is tried again after
    one interval and not on every tick.
    """
    row = session.get(SystemConfig, RECURRING_KEY)
    days = parse_recurring(row.value if row is not None else None)
    if days == 0:
        return False
    latest_record = (
        session.query(func.max(ReportRecord.recorded_at))
        .filter(ReportRecord.trigger.in_(DAY1_TRIGGERS))
        .scalar()
    )
    latest_start = (
        session.query(func.max(ReportRun.started_at))
        .filter(ReportRun.trigger == RECURRING)
        .scalar()
    )
    last = max(t for t in (latest_record, latest_start, datetime.min) if t)
    return now >= last + timedelta(days=days)


def _triggers(session: Session, model) -> set[str]:
    """Day 1 triggers only. A production row does not change when Day 1 is due."""
    rows = (
        session.query(model.trigger)
        .filter(model.trigger.in_(DAY1_TRIGGERS))
        .distinct()
        .all()
    )
    return {trigger for (trigger,) in rows}


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


_AT = "at the value for your selection"
_STRICTER = "stricter than the value for your selection"
_WEAKER = "weaker than the value for your selection"
_WRITTEN = (
    "The source lets a named officer approve an equivalent or stronger "
    "control in writing, which Vigil can't see."
)
_SESSIONS_SCREEN = "/settings?section=sessions"
_AUDIT_SCREEN = "/settings?section=audit"
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
    "settings.phishing_resistant_everyone",
    "settings.mfa_everyone",
)
_MODEL_ID = "settings.model_processing"
_SETTINGS_IDS = _SESSION_IDS + ("settings.retention", _MODEL_ID)
_AI_SCREEN = "/settings?section=ai-config"
_SCREENS = {"settings.retention": _AUDIT_SCREEN, _MODEL_ID: _AI_SCREEN}


def _read_selection() -> tuple[Optional[SelectionView], Optional[str]]:
    """The selection, or why every settings check is not checked.

    A failed read is never replaced with the empty selection. Visibility and
    coverage do not use this result.
    """
    try:
        view = read_view()
    except SelectionError:
        logger.error("Day 1 Report selection row does not parse")
        return None, "not checked: the selection row does not parse."
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read the selection: %s", type(exc).__name__
        )
        return None, "not checked: the selection could not be read."
    if view.unoffered:
        names = ", ".join(item.id for item in view.unoffered)
        return (
            view,
            "not checked: the selection names "
            f"{names}, which this release does not offer.",
        )
    return view, None


def _selection_snapshot(
    view: Optional[SelectionView], problem: Optional[str]
) -> dict[str, Any]:
    """Frozen onto the record. Labels are the catalogue sentences at run time."""
    if view is None:
        return {
            "jurisdictions": [],
            "kinds": [],
            "jurisdiction_labels": {},
            "kind_labels": {},
            "unreadable": problem,
        }
    chosen_j = set(view.jurisdictions)
    chosen_k = set(view.kinds)
    snapshot: dict[str, Any] = {
        "jurisdictions": list(view.jurisdictions),
        "kinds": list(view.kinds),
        "jurisdiction_labels": {
            row.id: row.sentence
            for row in view.catalogue.jurisdictions
            if row.id in chosen_j
        },
        "kind_labels": {
            row.id: row.sentence for row in view.catalogue.kinds if row.id in chosen_k
        },
    }
    if problem:
        snapshot["unreadable"] = problem
    return snapshot


def _selection_public(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    if raw.get("unreadable"):
        return {
            "jurisdictions": [],
            "kinds": [],
            "unreadable": raw["unreadable"],
        }
    jurisdictions = raw.get("jurisdiction_labels") or {}
    kinds = raw.get("kind_labels") or {}
    return {
        "jurisdictions": [
            jurisdictions.get(item, item) for item in raw.get("jurisdictions") or []
        ],
        "kinds": [kinds.get(item, item) for item in raw.get("kinds") or []],
        "unreadable": None,
    }


def _unchecked_settings(reason: str) -> list[dict[str, Any]]:
    """Every settings check, unscored. The every-install values are not used."""
    checks = []
    for check_id in _SETTINGS_IDS:
        screen = _SCREENS.get(check_id, _SESSIONS_SCREEN)
        check = _check(check_id, NOT_CHECKED, reason, extra={"screen": screen})
        check["citation"] = {"proposed": []}
        checks.append(check)
    return checks


def _settings_checks(view: SelectionView, gateway: tuple) -> list[dict[str, Any]]:
    return [
        *_session_checks(view),
        _retention_check(view),
        _safe_model_check(view, gateway),
    ]


def _safe_model_check(view: SelectionView, gateway: tuple) -> dict[str, Any]:
    try:
        return _model_check(view, gateway)
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not judge model processing: %s", type(exc).__name__
        )
        extra = {"screen": _AI_SCREEN}
        values = applicable(_MODEL_ID, list(view.jurisdictions), list(view.kinds))
        return _model_result(
            values, NOT_CHECKED, "Could not judge the model providers.", extra
        )


def _session_checks(view: SelectionView) -> list[dict[str, Any]]:
    """Session checks from the stored row. Defaults are not a substitute."""
    try:
        with unit_of_work() as session:
            policy = session_policy.read_row(session)[0].policy
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read session settings: %s", type(exc).__name__
        )
        return [
            _scored(
                view,
                check_id,
                NOT_CHECKED,
                _UNREADABLE_SESSION,
                screen=_SESSIONS_SCREEN,
                actual=None,
            )
            for check_id in _SESSION_IDS
        ]
    return _judge_session(view, policy, _coverage(policy))


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
                "keys_everyone": _accounts(
                    mfa_coverage.without_key(session, "everyone"), "security key"
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


def _judge_session(
    view: SelectionView, policy, coverage: dict[str, str]
) -> list[dict[str, Any]]:
    rows = (
        (
            "settings.idle",
            None if _looser_idle(policy) == 0 else _looser_idle(policy),
            _idle_reason(policy),
            None,
            False,
        ),
        (
            "settings.length",
            policy.absolute_hours,
            _length_reason(policy),
            None,
            False,
        ),
        (
            "settings.lockout",
            policy.lockout_threshold,
            _lockout_reason(policy),
            None,
            False,
        ),
        (
            "settings.dormancy",
            policy.dormancy_days,
            _dormancy_reason(policy),
            None,
            False,
        ),
        (
            "settings.unused",
            policy.token_unused_days,
            _unused_reason(policy),
            None,
            False,
        ),
        (
            "settings.mfa_privileged",
            policy.require_mfa,
            _mfa_reason(policy.require_mfa, "privileged"),
            coverage.get("privileged"),
            False,
        ),
        (
            "settings.phishing_resistant",
            policy.require_phishing_resistant,
            _key_reason(policy.require_phishing_resistant),
            coverage.get("keys"),
            True,
        ),
        (
            "settings.phishing_resistant_everyone",
            policy.require_phishing_resistant,
            _key_everyone_reason(policy.require_phishing_resistant),
            coverage.get("keys_everyone"),
            True,
        ),
        (
            "settings.mfa_everyone",
            policy.require_mfa,
            _mfa_reason(policy.require_mfa, "everyone"),
            coverage.get("everyone"),
            False,
        ),
    )
    checks = []
    for check_id, actual, reason, count, keys in rows:
        extras: dict[str, Any] = {}
        if count:
            extras["coverage"] = count
        check = _scored(
            view,
            check_id,
            PASS,
            reason,
            screen=_SESSIONS_SCREEN,
            actual=actual,
            extras=extras,
        )
        if keys and check["outcome"] == FAIL:
            check["evidence"]["prerequisites"] = _key_prerequisites()
        checks.append(check)
    return checks


def _retention_check(view: SelectionView) -> dict[str, Any]:
    """The period in force. A pending period, and no period, are not in force."""
    try:
        with unit_of_work() as session:
            days = retention_view(session, datetime.now(timezone.utc))["effective_days"]
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read audit retention: %s", type(exc).__name__
        )
        return _scored(
            view,
            "settings.retention",
            NOT_CHECKED,
            "Could not read the audit-trail retention period.",
            screen=_AUDIT_SCREEN,
            actual=None,
        )
    if days is None:
        actual = None
        reason = "Audit-trail retention: no period is in force."
    else:
        actual = int(days)
        reason = f"Audit-trail retention: the trail is kept for {int(days)} days."
    return _scored(
        view,
        "settings.retention",
        PASS,
        reason,
        screen=_AUDIT_SCREEN,
        actual=actual,
    )


# Where model processing runs. Components whose calls the check follows.
_MODEL_COMPONENTS = (
    "chat_default",
    "triage",
    "investigation",
    "summarization",
    "reporting",
)
_MODEL_LEAD = "Where model processing runs (model calls only): "
_CJIS_NOTE = (
    "CJIS Security Policy v6.1 restricts where criminal-justice information "
    "may be stored in a cloud. Whether a model provider's processing counts "
    "is a question for your counsel; this check doesn't score it."
)
_AUTHORIZED = ("high", "moderate", "low")
_BASES = ("adequacy", "dpf", "scc", "bcr", "art49")
# The statements each rule reads.
_MODEL_RULES = {
    "hipaa": ("self_hosted", "baa", "hipaa_account"),
    "fti": ("region", "self_hosted", "fedramp"),
    "gdpr": ("region", "transfer_basis"),
}


def _reached() -> tuple[dict[str, Any], dict[str, list], dict[str, str]]:
    """Each component's provider row, every row of each type reached, and the
    names of those who made statements. Plain copies: the session closes."""
    from core.llm.providers.registry import get_registry
    from core.storage.models import LLMProviderConfig

    registry = get_registry()
    copy = lambda r: SimpleNamespace(  # noqa: E731
        provider_id=r.provider_id,
        provider_type=r.provider_type,
        base_url=r.base_url,
        config=dict(r.config or {}),
    )
    reached: dict[str, Any] = {}
    by_type: dict[str, list] = {}
    with unit_of_work() as session:
        for component in _MODEL_COMPONENTS:
            picked = registry.resolve_model_for_component(component)
            row = session.get(LLMProviderConfig, picked[0]) if picked else None
            reached[component] = copy(row) if row is not None else None
            if row is not None and row.provider_type not in by_type:
                by_type[row.provider_type] = [
                    copy(r)
                    for r in statements.rows_of_type(session, row.provider_type, row)
                ]
        every = [r for rows in by_type.values() for r in rows]
        names = statements.account_names(session, statements.authors(every))
    return reached, by_type, names


def _statement_line(name: str, item: dict) -> str:
    value = statements.VALUE_WORDS.get(item.get("value") or "", item.get("value"))
    line = f"{statements.NAMES[name]}: {value}"
    if item.get("by_name"):
        line += f", stated by {item['by_name']} on {str(item.get('at') or '')[:10]}"
    if item.get("why"):
        cleared = "cleared: " if item.get("state") == statements.CLEARED else ""
        line += f" ({cleared}{item['why']})"
    return line


def _meets(rule: str, has) -> bool:
    self_hosted = has("self_hosted", ("yes",))
    if rule == "hipaa":
        return self_hosted or (
            has("baa", ("signed",)) and has("hipaa_account", ("on",))
        )
    if rule == "fti":
        return has("region", ("us",)) and (self_hosted or has("fedramp", _AUTHORIZED))
    if rule == "gdpr":
        return has("region", ("eu_eea",)) or has("transfer_basis", _BASES)
    raise RuntimeError(rule)


def _missing(rule: str, has) -> str:
    if rule == "hipaa":
        parts = []
        if not has("baa", ("signed",)):
            parts.append("a signed BAA")
        if not has("hipaa_account", ("on",)):
            parts.append("the HIPAA-eligible account mode")
        return f"{' and '.join(parts)}, or a self-hosted statement"
    if rule == "fti":
        parts = []
        if not has("region", ("us",)):
            parts.append("a United States processing and support region")
        if not has("self_hosted", ("yes",)) and not has("fedramp", _AUTHORIZED):
            parts.append("a FedRAMP authorization, or a self-hosted statement")
        return " and ".join(parts)
    return "an EU/EEA processing region or an EU transfer basis"


def _judge_type(
    rows: list, res: dict, region: Any, rules: list[str]
) -> tuple[str, list[str]]:
    """PASS, NEEDS_A_PERSON or FAIL for one provider type, and why.

    Every row of the type has to meet every rule. A statement standing but
    made before a credential changed, or a flag from before statements,
    counts only toward needs a person.
    """
    strict = lenient = True
    gaps: list[str] = []
    notes: list[str] = []
    for row in rows:
        held = {
            name: statements.standing(row.config, name, res[row.provider_id], region)
            for name in statements.VALUES
        }

        def has(name, ok, loose=False):
            s = held[name]
            return s.get("value") in ok and (
                s["state"] == statements.HELD
                or (loose and s["state"] == statements.PENDING)
            )

        for rule in rules:
            if not _meets(rule, has):
                strict = False
                if not _meets(rule, lambda n, ok: has(n, ok, True)):
                    lenient = False
                    gap = _missing(rule, has)
                    if gap not in gaps:
                        gaps.append(gap)
        for name in {n for rule in rules for n in _MODEL_RULES[rule]}:
            s = held[name]
            if s["state"] == statements.CLEARED:
                note = f"its {statements.NAMES[name]} statement was cleared: {s['why']}"
            elif s.get("why") == statements.LEGACY_SELF_HOSTED:
                note = s["why"]
            elif s["state"] == statements.PENDING:
                note = f"its {statements.NAMES[name]} statement was {s['why']}"
            else:
                continue
            if note not in notes:
                notes.append(note)
    if not lenient:
        return FAIL, [f"is missing {' and '.join(gaps)}", *notes]
    if not strict:
        them = "them" if len(notes) > 1 else "it"
        return NEEDS_A_PERSON, [*notes, f"a person has to re-confirm {them}"]
    return PASS, []


def _model_check(view: SelectionView, read: tuple) -> dict[str, Any]:
    """Every provider the model components reach, against every held value.

    ``read`` is :func:`read_gateway`'s (residency by gateway provider, Vertex
    region hash); a gateway that couldn't be read is (None, None).
    """
    values = applicable(_MODEL_ID, list(view.jurisdictions), list(view.kinds))
    extra: dict[str, Any] = {"screen": _AI_SCREEN}
    if "criminal_justice_information" in view.kinds:
        extra["counsel"] = _CJIS_NOTE
    try:
        reached, by_type, names = _reached()
    except Exception as exc:  # noqa: BLE001 - the report still completes
        logger.error(
            "Day 1 Report could not read the model providers: %s", type(exc).__name__
        )
        return _model_result(
            values, NOT_CHECKED, "Could not read the model assignments.", extra
        )
    components: dict[str, list[str]] = {}
    no_model = []
    for component, row in reached.items():
        if row is None:
            no_model.append(component)
        else:
            components.setdefault(row.provider_type, []).append(component)
    gateway, vertex = read
    regions = {"vertex": vertex}
    rules = [str(value.bound) for value in values]
    providers, fails, unchecked, people, passes = [], [], [], [], []
    first_problem = None
    for ptype, rows in by_type.items():
        res = {r.provider_id: residency.row_residency(r, gateway) for r in rows}
        why = statements.unknown(res.values())
        view_ = (
            {} if why else statements.type_view(rows, res, regions.get(ptype), names)
        )
        main = res[rows[0].provider_id]
        own = ", ".join(components.get(ptype, []))
        sh = view_.get("self_hosted") or {}
        sh_held = sh.get("state") == statements.HELD and sh.get("value") == "yes"
        providers.append(
            {
                "provider_type": ptype,
                "residency": residency.UNKNOWN if why else main.cls,
                "reason": why,
                "components": components.get(ptype, []),
                "statements": [
                    _statement_line(name, item)
                    for name, item in view_.items()
                    # a held self-hosted statement is the class label itself
                    if item.get("value") and not (name == "self_hosted" and sh_held)
                ],
                # The class label reads "Self-hosted, stated by … on …" then.
                "self_hosted_by": sh.get("by_name") if sh_held else None,
                "self_hosted_at": str(sh.get("at") or "")[:10] if sh_held else None,
            }
        )
        if why:
            unchecked.append(f"Not checked for {own}: {why}")
            continue
        if not rules:
            continue
        outcome, lines = _judge_type(rows, res, regions.get(ptype), rules)
        if outcome == FAIL:
            fails.append(f"{ptype} {'; '.join(lines)}.")
            first_problem = first_problem or ptype
        elif outcome == NEEDS_A_PERSON:
            people.append(f"{ptype}: {'; '.join(lines)}.")
            first_problem = first_problem or ptype
        else:
            relied = {n for rule in rules for n in _MODEL_RULES[rule]}
            held = [
                _statement_line(name, item)
                for name, item in view_.items()
                if name in relied
                and item.get("state") == statements.HELD
                and item.get("value") not in (None, "none", "no", "off")
            ]
            passes.append(f"{ptype}: {'; '.join(held)}.")
    if no_model:
        unchecked.append(
            f"Not checked for {', '.join(no_model)}: no model is assigned and "
            "no provider is active."
        )
    unchecked = [f"{line.rstrip('.')}." for line in unchecked]
    extra["providers"] = providers
    if not rules:
        reason = "No value for your selection restricts it. " + " ".join(unchecked)
        return _model_result(values, NO_VALUE, reason.strip(), extra)
    if first_problem:
        extra["screen"] = f"{_AI_SCREEN}&tab=statements&provider={first_problem}"
    if fails:
        outcome, body = FAIL, fails + unchecked + people
    elif unchecked:
        outcome, body = NOT_CHECKED, unchecked + people
    elif people:
        outcome, body = NEEDS_A_PERSON, people
    else:
        outcome = PASS
        body = [
            "Every provider your model calls reach meets the value for your selection.",
            *passes,
        ]
    return _model_result(values, outcome, " ".join(body), extra)


def _model_result(
    values: tuple[ScopedValue, ...], outcome: str, reason: str, extra: dict
) -> dict[str, Any]:
    check = _check(_MODEL_ID, outcome, f"{_MODEL_LEAD}{reason}", extra=extra)
    check["citation"] = _model_citation(values)
    return check


def _model_citation(values: tuple[ScopedValue, ...]) -> dict[str, Any]:
    """Every held value, frozen on the record. One value reads with its scope."""
    body: dict[str, Any] = {"proposed": []}
    if not values:
        return body
    join = lambda field: "; ".join(getattr(v, field) for v in values)  # noqa: E731
    body.update(
        cited_value=join("cited_value"),
        source_id=join("source_id"),
        clause=join("clause"),
        link=join("link"),
        date_checked=", ".join(dict.fromkeys(v.date_checked for v in values)),
    )
    if len(values) == 1:
        body["scope"] = scope_label(values[0])
    return body


def _scored(
    view: SelectionView,
    check_id: str,
    outcome: str,
    reason: str,
    *,
    screen: str,
    actual: Any,
    extras: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Score ``actual`` against the Applicable Value and store that citation."""
    resolved = resolve(check_id, list(view.jurisdictions), list(view.kinds))
    comparison = None
    if outcome != NOT_CHECKED:
        if resolved.scored is None:
            outcome = NO_VALUE
        else:
            outcome, comparison = _score(actual, resolved.scored)
            if outcome == FAIL and resolved.scored.written_alternative:
                reason = f"{reason} {_WRITTEN}"
    extra = dict(extras or {})
    extra["screen"] = screen
    if comparison:
        extra["comparison"] = comparison
    check = _check(check_id, outcome, reason, extra=extra)
    check["citation"] = _citation(resolved)
    return check


def _score(actual: Any, value: ScopedValue) -> tuple[str, str]:
    if value.direction == "ceiling":
        if actual is None:
            return FAIL, _WEAKER
        return _ceiling(int(actual), int(value.bound))
    if value.direction == "floor":
        if actual is None:
            return FAIL, _WEAKER
        return _floor(int(actual), int(value.bound))
    if value.direction == "level":
        return _level(str(actual), str(value.bound), value.above)
    raise RuntimeError(value.direction)


def _citation(resolved) -> dict[str, Any]:
    scored = resolved.scored
    body: dict[str, Any] = {
        "proposed": [
            {
                "cited_value": value.cited_value,
                "source_id": value.source_id,
                "scope": scope_label(value),
            }
            for value in resolved.proposed
        ]
    }
    if scored is not None:
        body["cited_value"] = scored.cited_value
        body["source_id"] = scored.source_id
        body["clause"] = scored.clause
        body["link"] = scored.link
        body["date_checked"] = scored.date_checked
        body["scope"] = scope_label(scored)
    if resolved.gap is not None and resolved.gap.release_gap:
        body["gap"] = (
            f"{resolved.gap.release_gap} Cited for {scope_label(resolved.gap)} "
            f"by {resolved.gap.source_id}."
        )
    if resolved.window is not None and resolved.window.window_minutes is not None:
        minutes = resolved.window.window_minutes
        body["window"] = (
            f"{minutes}-minute window: met. Vigil counts failures in a row and "
            "time never resets the count. "
            f"Cited for {scope_label(resolved.window)} by {resolved.window.source_id}."
        )
    return body


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


def _level(actual: str, cited: str, above: Optional[str]) -> tuple[str, str]:
    if above is not None and actual == above:
        return PASS, _STRICTER
    if actual == cited:
        return PASS, _AT
    return FAIL, _WEAKER


def _looser_idle(policy) -> int:
    """The looser idle limit, in minutes. 0 (no idle limit) is the loosest."""
    standard, privileged = policy.idle_standard_minutes, policy.idle_privileged_minutes
    if standard == 0 or privileged == 0:
        return 0
    return max(standard, privileged)


def _idle_reason(policy) -> str:
    """Name the looser limit, or both when they are equal. 0 reads as no idle limit."""
    standard = policy.idle_standard_minutes
    privileged = policy.idle_privileged_minutes
    if standard == privileged:
        if standard == 0:
            return (
                "Session inactivity: both the standard and the privileged "
                "idle limits are no idle limit."
            )
        return (
            "Session inactivity: both the standard and the privileged idle "
            f"limits end a sign-in after {standard} minutes with no activity."
        )
    # 0 is looser than any length; otherwise the longer one is.
    if standard == 0 or (privileged != 0 and standard > privileged):
        which, minutes = "standard", standard
    else:
        which, minutes = "privileged", privileged
    if minutes == 0:
        return f"Session inactivity: the {which} idle limit is no idle limit."
    return (
        f"Session inactivity: the {which} idle limit ends a sign-in after "
        f"{minutes} minutes with no activity."
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


def _key_everyone_reason(level: str) -> str:
    if level == "everyone":
        return (
            "Phishing-resistant MFA for everyone: every person must use a security key."
        )
    if level == "privileged":
        return (
            "Phishing-resistant MFA for everyone: privileged accounts must use "
            "a security key."
        )
    return (
        "Phishing-resistant MFA for everyone: sign-in does not require a security key."
    )


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
