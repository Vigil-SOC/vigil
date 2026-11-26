"""Master agent orchestrator for autonomous SOC operations.

The orchestrator runs three loops:
  1. Intake loop: picks up new findings/tasks and creates investigations
  2. Supervision loop: monitors running agents, reconciles finished ones
  3. Distil loop: turns finished hunts and closed cases into memory

It does NOT maintain a persistent Claude conversation.
All routine operations are pure Python logic.
"""

import asyncio
import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import httpx
from sqlalchemy import func, select

from core.agents.builtins import ORCHESTRATOR_ACTOR
from core.agents.lost_runs import Cursor, end_lost_start_jobs
from core.auth import sign_ins
from core.cases.case_sla_service import case_title, open_case_holding
from core.config import get_settings, is_integration_enabled
from core.daemon_config import OrchestratorConfig
from core.integrations._base.config import resolve
from core.integrations.slack.descriptor import SLACK
from core.storage.case_insert import insert_case
from core.storage.intake_queue import SEVERITY_BANDS, intake_severity_band
from core.time import utcnow

try:
    from opentelemetry.trace import SpanKind

    from core.telemetry import get_meter, get_tracer, inject_traceparent

    _tracer = get_tracer("vigil.daemon.orchestrator")
    _orch_meter = get_meter("vigil.daemon.orchestrator")
    _inv_created = _orch_meter.create_counter(
        "soc_daemon_orchestrator_investigations_created_total",
        description="Total investigations created",
        unit="1",
    )
    _inv_completed = _orch_meter.create_counter(
        "soc_daemon_orchestrator_investigations_completed_total",
        description="Total investigations completed",
        unit="1",
    )
    _inv_failed = _orch_meter.create_counter(
        "soc_daemon_orchestrator_investigations_failed_total",
        description="Total investigations failed",
        unit="1",
    )
    _dedup_prevented = _orch_meter.create_counter(
        "soc_daemon_orchestrator_dedup_prevented_total",
        description="Total investigations deduplicated",
        unit="1",
    )
    _stuck_agents = _orch_meter.create_counter(
        "soc_daemon_orchestrator_stuck_agents_total",
        description="Stuck agents detected and killed",
        unit="1",
    )

    def _observe_intake_queue_depth(_options: Any):
        try:
            from opentelemetry.metrics import Observation

            return [Observation(_count_queued_intake_rows())]
        except Exception as e:
            logger.debug("intake queue depth observation failed: %s", e)
            return []

    _intake_queue_depth = _orch_meter.create_observable_gauge(
        "soc_daemon_orchestrator_intake_queue_depth",
        callbacks=[_observe_intake_queue_depth],
        description="Queued intake triggers waiting for admission",
        unit="1",
    )
except Exception:
    _tracer = None  # type: ignore[assignment]
    _inv_created = _inv_completed = _inv_failed = _dedup_prevented = _stuck_agents = (
        _intake_queue_depth
    ) = None  # type: ignore[assignment]
from core.agents.operator_manifest import added_request, operator_request
from core.agents.projections import (
    THREAT_HUNT_WORKFLOW_ID,
    read_projection,
    run_id_for,
)
from core.agents.queue import RUN_KINDS, build_start_job, enqueue_run
from core.findings.exclusions import cached_active_ips, excluded_ips_of
from core.memory.entity_keys import finding_entity_keys, normalise_keys
from core.response.checkpoints import raise_for_checkpoint
from core.storage.connection import get_db_manager
from core.storage.models import (
    FINISHED_INVESTIGATION_STATUSES,
    IN_FLIGHT_INVESTIGATION_STATUSES,
    IntakeTrigger,
    Investigation,
)
from core.storage.shared_ioc_repository import (
    CaseMatch,
    SharedIOCRepository,
    reconcile_case_safely,
)
from core.threat_intel.mitre_lookup import (
    iter_techniques,
    resolve_technique,
    top_technique,
)
from core.workflows.enablement import is_enabled
from core.workflows.hypothesis_subjects import kept_subjects
from core.workflows.routing import (
    EMULATE_WORKFLOW,
    FALLBACK_WORKFLOW,
    SHADOW_WORKFLOW_ID,
)
from core.workflows.workflow_run_service import WorkflowRunService
from core.workflows.workflows_service import AGENT_RUN_PREFIX, WorkflowsService
from services.daemon.plan_generator import (
    _infer_title,
    generate_initial_context,
    generate_initial_state,
    select_workflow,
)
from services.daemon.workdir import WorkdirManager

logger = logging.getLogger(__name__)


def _count_queued_intake_rows() -> int:
    with get_db_manager().session_scope() as session:
        return session.query(IntakeTrigger).filter_by(state="queued").count()


# Proactive hunts (nightly, intel) never hold more than this many slots, so a
# detection arriving on a quiet fleet still finds one free.
SCHEDULE_RUN_CEILING = 1

# Seconds between looks for runs whose start job was lost: the look scans the runs table.
LOST_CHECK_EVERY = 60


# A hunt parked out of turns or budget, waiting on Keep going. A park on an
# approval checkpoint ("awaiting ...") is not: it is waiting_approval while the
# checkpoint is open and resumes on its own once answered.
def _parked_on_a_person(projection: Dict[str, Any]) -> bool:
    reason = projection.get("reason") or ""
    return projection.get("status") == "parked" and not reason.startswith("awaiting ")


def _count_investigations_in_flight() -> int:
    with get_db_manager().session_scope() as session:
        return (
            session.query(Investigation)
            .filter(Investigation.status.in_(IN_FLIGHT_INVESTIGATION_STATUSES))
            .count()
        )


# Rows actually running. An `assigned` row is stranded or transient, never
# enqueued and waiting, so it must not count against the slot it needs freed:
# counting it deadlocks pickup once `assigned` rows fill the cap (#1874). A parked
# row has released its lease and calls no model, so unanswered approvals do not
# stop new alerts being looked at. An answer resumes the run whatever this count
# says: a step a person approved does not queue behind unrelated alerts.
RUNNING_INVESTIGATION_STATUSES = ("executing",)


def _count_investigations_running() -> int:
    with get_db_manager().session_scope() as session:
        return (
            session.query(Investigation)
            .filter(Investigation.status.in_(RUNNING_INVESTIGATION_STATUSES))
            .count()
        )


def _count_schedule_runs_in_flight() -> int:
    with get_db_manager().session_scope() as session:
        return (
            session.query(IntakeTrigger)
            .join(
                Investigation,
                Investigation.investigation_id == IntakeTrigger.investigation_id,
            )
            .filter(
                IntakeTrigger.kind == "schedule",
                Investigation.status.in_(IN_FLIGHT_INVESTIGATION_STATUSES),
            )
            .count()
        )


MAX_CLOSED_LINKS = 5
MAX_OPEN_LINKS = (
    5  # counted apart, so a long closed history can't crowd out an open Case
)


# related_to: the type the case screen labels and an analyst's link defaults to.
def _link_cases(case_a: str, case_b: str, notes: str) -> None:
    from core.auth.permissions import builtin_reader
    from core.cases.case_records_service import link_cases
    from core.storage.connection import get_db_manager

    # Intake's own work: the built-in reader, which links restricted cases too.
    with builtin_reader(), get_db_manager().session_scope() as session:
        link_cases(
            session,
            case_a,
            case_b,
            relationship_type="related_to",
            created_by=ORCHESTRATOR_ACTOR,
            notes=notes,
        )


def lift_ai_enrichment(finding: Dict) -> Dict:
    """Copy ``ai_enrichment`` keys onto the top level ``select_workflow`` reads."""
    nested = finding.get("ai_enrichment")
    if not isinstance(nested, dict) or not nested:
        return finding
    from core.storage.service import DAEMON_ENRICHMENT_KEYS

    lifted = dict(finding)
    for key in DAEMON_ENRICHMENT_KEYS:
        if key in nested and lifted.get(key) is None:
            lifted[key] = nested[key]
    return lifted


_BAND_RANK = {name: i for i, name in enumerate(SEVERITY_BANDS)}


def _as_naive_utc(value: Any) -> Optional[datetime]:
    """Parse a dump-string or datetime into the naive UTC ``utcnow`` uses."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def intake_age_seconds(row: Dict, now: datetime) -> float:
    """Age of a queued row. Missing ``created_at`` reads as new, not oldest."""
    created = _as_naive_utc(row.get("created_at"))
    if created is None:
        return 0.0
    return (now - created).total_seconds()


def intake_raised_by(row: Dict) -> Optional[Dict[str, Any]]:
    """What raises a detection row ahead of older ones in its band, or None.

    A crown jewel the applied site declares over one of the finding's Entity
    Keys, else vStrike enrichment that says ``criticality: critical``. Schedule
    and ask rows, and a row with no finding loaded, are never raised.
    """
    finding = row.get("_finding")
    if row.get("kind") != "detection" or not isinstance(finding, dict):
        return None
    from core.memory.entity_keys import entity_context_candidates, entity_key
    from core.site import match

    keys = [
        entity_key(kind, value) for kind, value in entity_context_candidates(finding)
    ]
    jewel = next((m for m in match(keys) if m["kind"] == "crown_jewels"), None)
    if jewel is not None:
        return {
            "source": "crown_jewel",
            "name": jewel.get("name"),
            "file": jewel.get("file"),
            "line": jewel.get("line"),
            "revision": jewel.get("revision"),
            "selector": jewel.get("selector"),
            "key": jewel.get("key"),
        }
    context = finding.get("entity_context")
    vstrike = context.get("vstrike") if isinstance(context, dict) else None
    if isinstance(vstrike, dict) and vstrike.get("criticality") == "critical":
        return {"source": "enrichment", "criticality": "critical"}
    return None


def intake_rank_inputs(
    row: Dict,
    *,
    now: datetime,
    ttl_seconds: int,
    promote_fraction: float,
) -> tuple[str, bool, float, Optional[Dict[str, Any]]]:
    """``(band, promoted, age_seconds, raised_by)``: what ``rank_intake_row`` sorts on.

    Detection rows read current finding severity from ``_finding``;
    ``schedule`` and ``human_ask`` read the row's ``priority``.
    """
    age = intake_age_seconds(row, now)
    remaining = ttl_seconds - age
    promoted = 0 < remaining <= ttl_seconds * promote_fraction
    finding = row.get("_finding")
    finding_severity = finding.get("severity") if isinstance(finding, dict) else None
    band = intake_severity_band(
        row.get("kind"),
        finding_severity=finding_severity,
        priority=row.get("priority"),
    )
    return band, promoted, age, intake_raised_by(row)


def rank_intake_row(
    row: Dict,
    *,
    now: datetime,
    ttl_seconds: int,
    promote_fraction: float,
) -> tuple:
    """Sort key: last-quarter TTL promotion, then severity band, then raised
    (crown jewel or critical enrichment), then oldest.

    Lower sorts first. Never ``ORDER BY`` the severity string: alphabetically
    ``low`` precedes ``medium``.
    """
    band, promoted, _, raised = intake_rank_inputs(
        row, now=now, ttl_seconds=ttl_seconds, promote_fraction=promote_fraction
    )
    created = _as_naive_utc(row.get("created_at"))
    return (
        0 if promoted else 1,
        _BAND_RANK[band],
        0 if raised else 1,
        created if created is not None else now,
    )


def _caller(investigation_id: str) -> tuple[Optional[str], Optional[str]]:
    """The person a Human Ask named, as ``(username, principal)``.

    Both are absent for a detection or a schedule. A read that fails is the
    same answer: the run still starts, and its tools record an agent.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    try:
        with get_db_manager().session_scope() as session:
            row = (
                session.query(IntakeTrigger)
                .filter(IntakeTrigger.investigation_id == investigation_id)
                .order_by(IntakeTrigger.id.desc())
                .first()
            )
            payload = row.payload if row is not None else None
    except Exception:  # noqa: BLE001 — unreadable is not a person
        logger.warning("could not read the caller for %s", investigation_id)
        return None, None
    if not isinstance(payload, dict):
        return None, None
    name = payload.get("started_by")
    token = payload.get("principal")
    username = name if isinstance(name, str) and name else None
    principal = token if isinstance(token, str) and token else None
    return username, principal


def caseless_run_of(investigation_id: str) -> Optional[Dict[str, Any]]:
    """How to queue a rerun of a run that opened no Case: the daily gaps run (a
    schedule intake, with its gap proposals) or a person's Emulate (a Human Ask on
    the MITRE analyst). ``kind`` is the intake kind. None for any other run, or when
    the row is gone.

    A restart of either queues it as one, so it opens no Case, and the rules a gaps
    run saves still attach to the gaps it was handed.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    with get_db_manager().session_scope() as session:
        row = (
            session.query(IntakeTrigger)
            .filter(
                IntakeTrigger.investigation_id == investigation_id,
                IntakeTrigger.kind.in_(("schedule", "human_ask")),
            )
            .order_by(IntakeTrigger.id.desc())
            .first()
        )
        kind = row.kind if row is not None else None
        payload = row.payload if row is not None else None
    if not isinstance(payload, dict) or not payload.get("no_case"):
        return None
    if kind == "human_ask":
        if payload.get("workflow_id") != EMULATE_WORKFLOW:
            return None
        return {"kind": kind, "no_case": True}
    return {
        "kind": kind,
        "no_case": True,
        "gap_proposal_ids": [
            i for i in payload.get("gap_proposal_ids") or () if isinstance(i, str) and i
        ],
    }


def _stamp_phases(context: Dict[str, Any], workflow_id: str, workflows) -> None:
    """The phase agents as the definition names them now, stored on the run."""
    from core.agents.attribution import phase_actors

    try:
        workflow = workflows.get_workflow(workflow_id)
    except Exception:  # noqa: BLE001 - a run with no snapshot uses its plugin
        return
    if workflow is None:
        return
    agents = phase_actors(workflow.phases)
    if agents:
        context["phase_agents"] = agents


def _principal_of(started_by: Optional[str], principal: Optional[str]) -> Optional[str]:
    """The principal beside triggered_by. A person when the token names one, else system.

    ``started_by`` is the text column. orchestrator is not an agent principal.
    """
    del started_by
    from core.auth import tool_principal
    from core.auth.principals import SYSTEM_PRINCIPAL_ID

    if principal:
        try:
            claims = tool_principal.read(principal)
        except tool_principal.InvalidPrincipal:
            return SYSTEM_PRINCIPAL_ID
        if claims.person_id:
            return claims.person_id
        if claims.username:
            from core.agents.attribution import principal_id_for_username

            return principal_id_for_username(claims.username)
    return SYSTEM_PRINCIPAL_ID


def _sid_of(principal: Optional[str]) -> Optional[str]:
    """The sign-in a Human Ask's stored token names. Absent for anything else."""
    from core.auth import tool_principal

    try:
        return tool_principal.read(principal).sid if principal else None
    except tool_principal.InvalidPrincipal:
        return None


def _start_principal(
    run_id: str,
    run_kind: str,
    started_by: Optional[str],
    principal: Optional[str],
    trigger_type: Optional[str],
) -> tuple[str, str, str, Optional[str]]:
    """Token, actor principal, origin kind, person principal. The agent does not sign."""
    from core.agents.attribution import plugin_actor
    from core.auth import tool_principal

    actor = plugin_actor(run_kind)
    person_id: Optional[str] = None
    username: Optional[str] = None
    sid: Optional[str] = None
    if principal:
        try:
            claims = tool_principal.read(principal)
        except tool_principal.InvalidPrincipal:
            logger.warning("stored principal for %s did not verify", run_id)
            claims = None
        if claims is not None:
            person_id = claims.person_id
            username = claims.username
            sid = claims.sid
    if username and not person_id:
        from core.agents.attribution import principal_id_for_username

        person_id = principal_id_for_username(username)
    if person_id or username:
        origin = "person"
    elif trigger_type in ("schedule", "scheduled", "rollout"):
        origin = "schedule"
    else:
        origin = "alert"
    token = tool_principal.mint(
        username,
        person_id=person_id,
        agent_id=actor,
        run_id=run_id,
        origin=origin,
        ttl=tool_principal.RUN_TTL,
        sid=sid,
    )
    return token, actor, origin, person_id


def _record_start(
    run_id: str,
    actor: str,
    origin: str,
    person_id: Optional[str],
    spec: Dict[str, Any],
) -> None:
    from core.agents.attribution import TrailWriteError, note_run_start

    origin_value = (person_id or "") if origin == "person" else origin
    try:
        note_run_start(
            run_id=run_id,
            actor=actor,
            origin_kind=origin,
            origin=origin_value,
            spec=spec,
        )
    except TrailWriteError:
        raise
    except Exception:  # noqa: BLE001 - a database without the trail keeps today's path
        logger.warning("run %s started without a trail entry", run_id)


def _note_gap_run(proposal_ids: List[str], run_id: str) -> None:
    """Record on the gaps run's proposals the run they were handed to."""
    from core.detections.engineering import proposals

    try:
        proposals.gap_run_started(proposal_ids, run_id)
    except Exception as exc:  # noqa: BLE001 -- the run starts without the record
        logger.warning("could not record run %s on its gap proposals: %s", run_id, exc)


def insert_not_offered(*, finding_id: str, priority: str, reason: str) -> None:
    """Put the gate's no on the ledger. The row is never queued, so nothing drains it.

    An alert with a row already keeps it: Triage reads the latest row, and a person
    sending the alert to triage is a later word than this one.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    try:
        with get_db_manager().session_scope() as session:
            if session.query(IntakeTrigger.id).filter_by(finding_id=finding_id).first():
                return
            session.add(
                IntakeTrigger(
                    kind="detection",
                    state="not_offered",
                    reason=reason,
                    finding_id=finding_id,
                    priority=priority,
                    decided_at=utcnow(),
                )
            )
    except Exception as e:
        # The reason is a note on the alert; a miss costs the note, not the triage.
        logger.error("Failed to record why %s was not offered: %s", finding_id, e)


@dataclass(frozen=True)
class CaseSpec:
    """Case to mint in the launch transaction. ``case_id`` is assigned before plan files."""

    title: str
    finding_ids: List[str]
    priority: str
    case_id: Optional[str] = None
    # Who reads it, set when the case opens restricted; ``note`` joins its timeline.
    readers: Optional[List[str]] = None
    reader_teams: Optional[List[str]] = None
    note: Optional[str] = None


# What the lead list's findings run under: their own trigger type, so the shadow
# run and the related-case links, which key on "finding", skip them.
LEAD_TRIGGER = "insider_lead"


@dataclass(frozen=True)
class InsiderLead:
    """A finding the lead list says to hunt on: what the hunt asks, who reads the case."""

    accounts: List[str]
    hypothesis: str
    subjects: Dict[str, List[str]]
    readers: List[str]
    reader_teams: List[str]
    why: str


def _lead_readers() -> tuple[List[str], List[str]]:
    """The list's readers and teams still there. No actor is re-checked: nobody acts."""
    from core.auth import principals
    from core.storage.models import Principal, Team
    from core.workflows.insider_lead import configured_readers

    names, team_ids = configured_readers()
    with get_db_manager().session_scope() as session:
        live = set(
            session.scalars(
                select(Principal.principal_id).where(
                    Principal.principal_id.in_(names),
                    Principal.deleted_at.is_(None),
                    Principal.kind.in_((principals.PERSON, principals.SERVICE_ACCOUNT)),
                )
            )
        )
        teams = set(
            session.scalars(
                select(Team.team_id).where(
                    Team.team_id.in_(team_ids),
                    Team.archived_at.is_(None),
                    Team.is_default.is_(False),
                )
            )
        )
    return [n for n in names if n in live], [t for t in team_ids if t in teams]


def _new_case_id() -> str:
    return f"case-{utcnow().strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}"


def _human_ask_case_title(
    hypothesis: Optional[str], findings: List[Dict], workflow_id: str
) -> str:
    """Title for a Case minted from a Human Ask: hypothesis first, else the finding."""
    if hypothesis and str(hypothesis).strip():
        return case_title(str(hypothesis), 200)
    if findings:
        return _infer_title(findings[0], workflow_id)[:200]
    return _infer_title({}, workflow_id)[:200]


# Shadow adjudication (#880). The second run over an admitted finding is addressed
# from the investigation id like the real one, so the pair joins by recomputing
# rather than by a column. A uuid5 of the suffixed id rather than a suffixed uuid:
# agent_events.run_id is a uuid column and would refuse the string.
SHADOW_HYPOTHESIS_FILE = "shadow_hypothesis.txt"
# The detection proposals the daily gaps run opened for the run it queued.
GAP_PROPOSALS_FILE = "gap_proposals.json"


def shadow_run_id_for(investigation_id: str) -> str:
    return run_id_for(f"{investigation_id}-shadow")


def _shadow_hypothesis(findings: List[Dict]) -> str:
    """One line the adjudicator puts on its board: what intake read into the finding.

    The definition declares ``hypotheses: []`` and the hunt loop refuses a run with
    none stated, so this is required, not decorative. Title, the entities the
    finding names, and the tactic of its top predicted technique -- the same
    reading ``_infer_title`` makes, spelled out as a claim."""
    if not findings:
        return ""
    finding = findings[0]
    title = str(finding.get("title") or finding.get("description") or "").strip()
    subject = title or "the admitted finding"

    # All of them, because the same keys become the claim's subjects: a subject
    # set wider than what the line names would be a guess, not a statement.
    entities = finding_entity_keys([finding])
    where = f" involving {', '.join(entities)}" if entities else ""

    # iter_techniques spans every shape mitre_predictions is stored in.
    top = top_technique(iter_techniques(finding))
    intent = ""
    if top:
        tid, name, tactic = resolve_technique(top)
        if tid:
            technique = f"{tid} {name}" if name and name != tid else tid
            # The tactic when the taxonomy knows it; the technique alone when not.
            intent = (
                f" and is {technique} activity"
                if tactic == "Unknown"
                else f" and is {tactic} activity ({technique})"
            )
    return f"{subject}{where} is what intake says it is{intent}"


def _mint_case(session, spec: CaseSpec) -> str:
    """Insert a Case and its finding links on the caller's session. Does not commit."""
    case_id = spec.case_id or _new_case_id()
    now = utcnow()
    case = insert_case(
        session,
        case_id=case_id,
        title=(spec.title or "Investigation")[:200],
        description="",
        status="open",
        priority=spec.priority,
        timeline=[
            {"timestamp": now.isoformat() + "Z", "event": "Case created"},
            *(
                [{"timestamp": now.isoformat() + "Z", "event": spec.note}]
                if spec.note
                else []
            ),
        ],
        finding_ids=spec.finding_ids,
        readers=spec.readers,
        reader_teams=spec.reader_teams,
    )
    return case.case_id


# Intake rows only ever name Postgres findings, so these read Postgres directly
# rather than through DatabaseDataService, which answers from the demo service
# in demo mode and would never see them (#2236).
def _load_finding(finding_id: str) -> Optional[Dict]:
    """The finding as ``FindingSchema`` dumps it, or None if missing or unreadable."""
    from core.storage.schemas import FindingSchema
    from core.storage.service import DatabaseService

    try:
        finding = DatabaseService().get_finding(finding_id)
    except Exception as e:
        logger.error("Failed to read finding %s: %s", finding_id, e)
        return None
    return FindingSchema.dump(finding) if finding else None


def _reindex_case(case_id: Optional[str]) -> None:
    """Reconcile the Case's index rows in their own transaction; never raises."""
    if not case_id:
        return
    try:
        with get_db_manager().session_scope() as session:
            reconcile_case_safely(session, case_id)
    except Exception as e:
        logger.error("Failed to index IOCs for case %s: %s", case_id, e)


def _matching_cases(finding: Dict, *, open: bool) -> List[CaseMatch]:
    """Cases the finding belongs with, newest first. Raises on a failed read.

    A failed read must not become an empty answer: an empty answer opens a new Case.
    """
    with get_db_manager().session_scope() as session:
        return SharedIOCRepository(session).matching_cases(finding, open=open)


def _removed_from(finding_id: str) -> set:
    """Ids of the Cases a person removed this finding from. Raises on a failed read."""
    from core.storage.models import CaseFindingRemoval

    with get_db_manager().session_scope() as session:
        return set(
            session.scalars(
                select(CaseFindingRemoval.case_id).where(
                    CaseFindingRemoval.finding_id == finding_id
                )
            )
        )


def _link_note(match: CaseMatch) -> str:
    """What matched, without the key's kind prefix: the phrasing of attach notes and Triage."""
    kind, _, value = match.key.partition(":")
    if match.same_alert:
        return f"same alert on {value}"
    if kind == "src_ip":
        return f"same source IP {value}"
    if kind == "dst_ip":
        return f"{value} was a destination there"
    if kind == "ip":  # an attach recorded before roles were
        return f"IP {value} matched before roles were recorded"
    return f"same {kind} {value}"


def _related_note(match: CaseMatch, new_case: str) -> str:
    """The note on a related-Case link; true read from either Case."""
    kind, _, value = match.key.partition(":")
    seen = {
        "src_ip": "was a source",
        "dst_ip": "was a destination",
        "ip": "was seen, its role not recorded,",
    }.get(kind)
    if match.same_alert or seen is None:
        return _link_note(match)
    return (
        f"{value} {seen} on {match.case_id} and is the source of an alert on {new_case}"
    )


def _record_case_activity(
    case_id: str, activity_type: str, description: str, details: Dict
) -> None:
    """Add an entry to the Case's activity. Raises on failure."""
    from core.cases.agent_closure import activity_actor
    from core.storage.models import Case

    with get_db_manager().session_scope() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise LookupError(f"case {case_id} is gone")
        case.activities = [
            *(case.activities or []),
            {
                "timestamp": utcnow().isoformat() + "Z",
                "activity_type": activity_type,
                "description": description,
                "details": details,
                **activity_actor(ORCHESTRATOR_ACTOR, session),
            },
        ]


def _record_merge_activity(case_id: str, finding: Dict, match: CaseMatch) -> None:
    """Note on the Case that an alert joined it. Raises on failure."""
    title = finding.get("title") or finding.get("finding_id") or "alert"
    _record_case_activity(
        case_id,
        "alert_added",
        f"Alert `{title}` added: {_link_note(match)}",
        {
            "finding_id": finding.get("finding_id"),
            "matched_key": match.key,
            "same_alert": match.same_alert,
        },
    )


def _record_failure(case_id: str, description: str, details: Dict) -> None:
    """Put a failure where an analyst reads it: the Case's activity. Never raises."""
    try:
        _record_case_activity(case_id, "intake_failure", description, details)
    except Exception:
        logger.exception("Could not record on case %s: %s", case_id, description)


def _notify_joined(case_id: str, finding: Dict, match: CaseMatch) -> None:
    """Tell the Case's assignee and watchers an alert joined it. Raises on failure.

    One notice per person while an earlier join notice on this Case is unread.
    A watcher who switched ``alert_joined`` off gets none, and a person who may
    not read the Case gets none (``notify``'s ``about_case``). A Case with
    neither assignee nor watchers tells the active people on its team instead.
    """
    from urllib.parse import quote

    from core.cases.case_notification_service import principal_of
    from core.platform.notices import notify, send_notices
    from core.storage.models import Case, CaseNotification, CaseWatcher
    from core.teams import members_of

    title = finding.get("title") or finding.get("finding_id") or "alert"
    with get_db_manager().session_scope() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise LookupError(f"case {case_id} is gone")
        names = [case.assignee] if case.assignee else []
        watchers = list(
            session.scalars(select(CaseWatcher).where(CaseWatcher.case_id == case_id))
        )
        for watcher in watchers:
            if (watcher.notification_preferences or {}).get("alert_joined", True):
                names.append(watcher.user_id)
        # A person who is assignee and watcher is told once.
        principals = dict.fromkeys(
            p for name in names if (p := principal_of(session, name))
        )
        if not case.assignee and not watchers:
            # Nobody owns it: tell the team, active people only.
            principals = dict.fromkeys(
                u.principal_id
                for u in members_of(session, case.team_id)
                if u.is_active and not u.service_account
            )
        sent = []
        for principal in principals:
            unread = session.scalar(
                select(CaseNotification.notification_id)
                .where(
                    CaseNotification.user_id == principal,
                    CaseNotification.notification_type == "alert_joined",
                    CaseNotification.is_read.is_(False),
                    CaseNotification.notification_metadata["case_id"].astext == case_id,
                )
                .limit(1)
            )
            if unread is not None:
                continue
            notice_id = notify(
                principal,
                "alert_joined",
                f"Case {case_id}",
                f"Alert `{title}` joined this case: {_link_note(match)}.",
                link=f"/cases?case={quote(case_id, safe='')}",
                metadata={
                    "case_id": case_id,
                    "finding_id": finding.get("finding_id"),
                },
                session=session,
                about_case=case_id,
            )
            if notice_id is not None:
                sent.append(notice_id)
    # Mail goes out only once the rows have committed.
    send_notices(sent)


def _raise_case_priority(case_id: str, severity: Optional[str]) -> None:
    """Raise the Case to the alert's severity when that is higher; never lowers it."""
    from core.cases.case_workflow_service import preferred_merge_priority
    from core.storage.models import Case

    rated = (severity or "").strip().lower()
    with get_db_manager().session_scope() as session:
        case = session.get(Case, case_id)
        if case is None:
            return
        current = (case.priority or "").strip().lower() or "unknown"
        case.priority = preferred_merge_priority(current, rated)


def _attach_finding(case_id: str, finding_id: str) -> bool:
    """Link the finding to the case. False on any failure, never raises."""
    from core.auth.permissions import builtin_reader
    from core.storage.service import DatabaseService

    try:
        # Intake attaches to a restricted case too: a built-in reader (#4354).
        with builtin_reader():
            return bool(DatabaseService().add_finding_to_case(case_id, finding_id))
    except Exception as e:
        logger.error(
            "Failed to attach finding %s to case %s: %s", finding_id, case_id, e
        )
        return False


def _record_human_ask_document(
    session, case_id: Optional[str], document: Optional[str]
) -> None:
    """Record the Human Ask's document on the Case. No-op when either is missing.

    The string is opaque, so it goes in ``description`` (Text) and never in
    ``file_path``: that column is resolved against the evidence store and
    hashed, which is wrong for a URL and worse for a path a caller chose.

    Written straight onto the launch session rather than through
    ``CaseEvidenceService``: this path stores no file, so it has no use for the
    evidence store, and the row is the same either way.
    """
    if not case_id or not document:
        return
    from core.storage.models import CaseEvidence

    now = utcnow()
    session.add(
        CaseEvidence(
            case_id=case_id,
            evidence_type="document",
            name="Document on the Human Ask",
            description=str(document),
            collected_by="human_ask",
            collected_at=now,
            chain_of_custody=[
                {
                    "timestamp": now.isoformat(),
                    "action": "collected",
                    "user": "human_ask",
                    "notes": "Document supplied on the Human Ask",
                }
            ],
        )
    )


class _TriggerAlreadyDecided(Exception):
    """The CAS on a queued trigger matched zero rows."""


class Orchestrator:
    """Master agent that manages autonomous SOC investigations."""

    def __init__(
        self,
        config: OrchestratorConfig,
        workflows: Optional[WorkflowsService] = None,
    ):
        self.config = config
        self._enabled = config.enabled
        self._shutdown_event: Optional[asyncio.Event] = None
        # Set by the daemon: watch(name, task, shutdown_event) for /status loops.
        self.watch_loop: Optional[Callable[..., None]] = None
        # Set by the daemon: counts one intake-loop pass on /health.
        self.note_pass: Optional[Callable[[], None]] = None
        # Read for the run_kind a definition declares; the file cache needs no DB.
        self._workflows = workflows or WorkflowsService()

        self.workdir = WorkdirManager(config.workdir_base)

        self._hourly_paused = False

        self.stats = {
            "investigations_created": 0,
            "investigations_completed": 0,
            "investigations_failed": 0,
            "stuck_agents_killed": 0,
            "dedup_prevented": 0,
            "total_cost_usd": 0.0,
        }
        self._intake_surge_active = False
        # Where the last look for lost start jobs stopped, and when it ran.
        self._lost_cursor: Optional[Cursor] = None
        self._lost_checked_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def kill(self):
        """Emergency stop: cancel all running agents immediately."""
        self._enabled = False
        self._abandon_in_flight()
        logger.warning("Orchestrator KILLED - all agents stopped")

    async def run(self, shutdown_event: asyncio.Event):
        """Main orchestrator entry point, called by SOCDaemon."""
        self._shutdown_event = shutdown_event

        # The loops run whether AutoOps is on or off; only a launch reads the switch.
        loops = {
            "_intake_loop": self._intake_loop,
            "_supervision_loop": self._supervision_loop,
            "_distil_loop": self._distil_loop,
            "_run_retry_loop": self._run_retry_loop,
            "_indicator_catchup_loop": self._indicator_catchup_loop,
        }
        tasks = []
        for name, loop in loops.items():
            task = asyncio.create_task(loop(shutdown_event))
            if self.watch_loop:
                self.watch_loop(name, task, shutdown_event)
            tasks.append(task)

        was_enabled: Optional[bool] = None
        while not shutdown_event.is_set():
            self._sync_enabled_from_db()

            if self._enabled != was_enabled:
                was_enabled = self._enabled
                if self._enabled:
                    await self._on_enabled()
                else:
                    logger.info(
                        "Orchestrator disabled - bookkeeping only, no launches until re-enabled"
                    )

            await self._sleep(shutdown_event, 5)

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        self._abandon_in_flight()
        logger.info("Orchestrator shutdown complete")

    async def _on_enabled(self) -> None:
        logger.info("Orchestrator starting...")
        logger.info(f"  Max concurrent agents: {self.config.max_concurrent_agents}")
        logger.info(
            f"  Max cost/investigation: ${self.config.max_cost_per_investigation}"
        )
        logger.info(f"  Dry run: {self.config.dry_run}")

        try:
            await self._complete_legacy_review_rows()
        except Exception as e:
            logger.error("Completing legacy review rows failed: %s", e, exc_info=True)

    def _sync_enabled_from_db(self):
        """Read the enabled state from the single ``orchestrator.settings``
        SystemConfig row (set by the API/UI toggle or the Settings page)."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import SystemConfig

            with get_db_manager().session_scope() as session:
                cfg = (
                    session.query(SystemConfig)
                    .filter_by(key="orchestrator.settings")
                    .first()
                )
                if cfg and isinstance(cfg.value, dict):
                    db_enabled = bool(cfg.value.get("enabled", False))
                    if db_enabled != self._enabled:
                        self._enabled = db_enabled
                        logger.info(
                            f"Orchestrator {'ENABLED' if db_enabled else 'DISABLED'} (synced from DB)"
                        )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Intake Loop
    # -------------------------------------------------------------------------

    async def _intake_loop(self, shutdown_event: asyncio.Event):
        """Drain queued trigger rows and create new investigations."""
        while not shutdown_event.is_set():
            # Intake only. The bookkeeping loop in run, and supervision and
            # distil, do not count as this component making progress.
            if self.note_pass is not None:
                self.note_pass()
            try:
                await self._drain_intake(shutdown_event)
                if self._enabled:
                    await self._pickup_assigned_investigations(shutdown_event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Intake loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval)

    async def _drain_intake(self, shutdown_event: asyncio.Event):
        """Merge and expire every queued row, then, with AutoOps on, launch in rank order while a slot is free."""
        now = utcnow()
        launchable: List[Dict] = []
        for row in self._queued_intake_triggers():
            kept = self._resolve_intake_row(row, now)
            if kept is not None:
                launchable.append(kept)

        launchable.sort(
            key=lambda r: rank_intake_row(
                r,
                now=now,
                ttl_seconds=self.config.intake_ttl_seconds,
                promote_fraction=self.config.intake_ttl_promote_fraction,
            )
        )
        # Rows stay queued while the hour is at the cap; they launch once old
        # spend rolls out of the window.
        if self._enabled and not self._hourly_budget_exhausted():
            schedule_runs = self._schedule_runs_in_flight()
            for row in launchable:
                # Read fresh: turning AutoOps off stops the next launch, not the next tick.
                self._sync_enabled_from_db()
                if (
                    not self._enabled
                    or self._running() >= self.config.max_concurrent_agents
                ):
                    break
                is_schedule = row.get("kind") == "schedule"
                # Skipped, not a break: rows behind it may be detections.
                if is_schedule and schedule_runs >= SCHEDULE_RUN_CEILING:
                    continue
                await self._process_intake_row(row, shutdown_event)
                if is_schedule:
                    schedule_runs += 1

        depth = self._queued_intake_depth()
        if depth is not None:
            self._record_intake_depth(depth)

    def _schedule_runs_in_flight(self) -> int:
        try:
            return _count_schedule_runs_in_flight()
        except Exception as e:
            # Unknown reads as full: hold hunts rather than risk every slot.
            logger.error(f"Failed to count in-flight schedule runs: {e}")
            return SCHEDULE_RUN_CEILING

    def _queued_intake_depth(self) -> Optional[int]:
        try:
            return _count_queued_intake_rows()
        except Exception as e:
            logger.error(f"Failed to count intake queue: {e}")
            return None

    def _record_intake_depth(self, depth: int) -> None:
        """Notify once when queued depth crosses the surge constant."""
        threshold = self.config.intake_surge_depth
        was_active = getattr(self, "_intake_surge_active", False)
        now_active = depth > threshold
        self._intake_surge_active = now_active
        if now_active and not was_active:
            self._write_intake_surge_notification(depth)

    def _write_intake_surge_notification(self, depth: int) -> None:
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import CaseNotification

            with get_db_manager().session_scope() as session:
                session.add(
                    CaseNotification(
                        case_id=None,
                        user_id="admin",
                        notification_type="intake_surge",
                        title="Intake queue surge",
                        message=f"Intake queue depth is {depth}",
                        delivery_channel="ui",
                        priority="high",
                        notification_metadata={"queue_depth": depth},
                    )
                )
            logger.info("Intake surge notification written at depth %s", depth)
        except Exception as e:
            logger.error(f"Failed to create intake surge notification: {e}")

    def _resolve_intake_row(self, row: Dict, now: datetime) -> Optional[Dict]:
        """Expire or merge a queued row. Capacity does not wait on this pass."""
        if intake_age_seconds(row, now) >= self.config.intake_ttl_seconds:
            self._decide_trigger(
                row.get("id"),
                state="expired",
                reason="ttl_expired" if self._enabled else "ttl_expired_autoops_off",
                # The open Case it matched, so the expiry names what a link would have.
                merged_into=self._open_case_matched(row),
            )
            return None
        if row.get("kind") == "detection":
            finding = self._hydrate_detection_finding(row)
            row["_finding"] = finding
            # A detection queued before its address was excluded must not open
            # a case now; an unhydrated finding stays queued as before.
            if finding is not None and excluded_ips_of(finding, cached_active_ips()):
                self._decide_trigger(
                    row.get("id"), state="expired", reason="excluded_ip"
                )
                return None
            if finding is not None:
                held, lead = self._lead_or_attach(finding, row.get("id"))
                row["_lead"] = lead
                # A lead is never merged into an open case: it opens its own.
                if held or (
                    lead is None and self._merge_if_overlaps(finding, row.get("id"))
                ):
                    return None
        return row

    def _lead_or_attach(
        self, finding: Dict, trigger_id: Optional[int]
    ) -> tuple[bool, Optional[InsiderLead]]:
        """Whether the lead list makes this finding a lead: ``(held, lead)``.

        ``held`` is True when the row is not launching this tick (attached to a
        hunt already running on the account, or a read failed and the row stays
        queued). ``(False, None)`` is an ordinary finding. A lead matches the
        list and names a ``user`` Entity Key, with ``threat-hunt`` on, the finding
        on no open case, and a reader left to open the case to.
        """
        from core.workflows import insider_lead

        finding_id = finding.get("finding_id", "unknown")
        try:
            if not insider_lead.matches(finding):
                return False, None
            if not is_enabled(THREAT_HUNT_WORKFLOW_ID):
                logger.info(
                    "Finding %s matches the lead list; %s is off",
                    finding_id,
                    THREAT_HUNT_WORKFLOW_ID,
                )
                return False, None
            if not insider_lead.account_keys(finding):
                logger.info("Finding %s %s", finding_id, insider_lead.NO_ACCOUNT)
                return False, None
            if open_case_holding(finding_id):
                return False, None  # already held: the merge path says so
            lead = insider_lead.lead_for(finding_id, finding)
            keys = lead["accounts"]
            if self._attach_if_hunting(finding, keys, trigger_id):
                return True, None
            readers, teams = _lead_readers()
            if not readers and not teams:
                logger.warning(
                    "Finding %s matches the lead list but none of its readers is left; "
                    "not opening an unrestricted case",
                    finding_id,
                )
                return False, None
            row = insider_lead.leads()
            source = finding.get("data_source")
            by = (
                f"source {source}"
                if source in row["sources"]
                else f"rule name {finding.get('title')}"
            )
            proposal = lead["proposal"]
            return False, InsiderLead(
                accounts=keys,
                hypothesis=proposal["hypothesis"],
                subjects=proposal["hypothesis_subjects"],
                readers=readers,
                reader_teams=teams,
                why=f"The insider lead list opened this: its {by} matched",
            )
        except Exception as e:
            logger.error(
                "Failed to check finding %s against the lead list; leaving queued: %s",
                finding_id,
                e,
            )
            return True, None

    def _attach_if_hunting(
        self, finding: Dict, keys: List[str], trigger_id: Optional[int]
    ) -> bool:
        """Put a lead on the restricted case whose hunt is already running on one of
        its accounts, and start nothing. True: the row is not launching (a failed
        read or attach leaves it queued). Asked again at launch, so two leads on one
        account in one drain start one hunt."""
        from core.auth.permissions import builtin_reader, case_restricted
        from core.memory.prior_hunts import list_prior_hunts
        from core.storage.models import Case

        finding_id = finding.get("finding_id", "unknown")
        try:
            # A Case this lead was taken off is not one to rejoin. A failed
            # read leaves it queued: attaching would also clear the removal.
            removed = _removed_from(finding_id) if finding.get("finding_id") else set()
            # The built-in reader: a restricted run is what this must see.
            with builtin_reader():
                hunts = list_prior_hunts(keys)["in_flight"]
                with get_db_manager().session_scope() as session:
                    case_id = next(
                        (
                            h["case_id"]
                            for h in hunts
                            if h["matched_keys"]
                            and h["case_id"]
                            and h["case_id"] not in removed
                            and (case := session.get(Case, h["case_id"])) is not None
                            and case_restricted(case)
                        ),
                        None,
                    )
        except Exception as e:
            logger.error(
                "Failed to read hunts on %s for finding %s; leaving queued: %s",
                keys,
                finding_id,
                e,
            )
            return True
        if case_id is None:
            return False
        if not _attach_finding(case_id, finding_id):
            logger.warning(
                "Finding %s leads to the hunt on case %s but could not be attached; leaving queued",
                finding_id,
                case_id,
            )
            return True
        logger.info("Finding %s attached to the hunt on case %s", finding_id, case_id)
        why = f"Alert `{finding.get('title') or finding_id}` added: a hunt on {', '.join(keys)} is already running"
        try:
            _record_case_activity(
                case_id,
                "alert_added",
                why,
                {"finding_id": finding_id, "accounts": keys},
            )
        except Exception:
            logger.exception(
                "Finding %s joined case %s but could not note it", finding_id, case_id
            )
        self._count_dedup_prevented()
        self._decide_trigger(
            trigger_id, state="merged", reason=why, merged_into=case_id
        )
        return True

    def _open_case_matched(self, row: Dict) -> Optional[str]:
        """The newest open Case a queued detection matches, or None. Never raises."""
        if row.get("kind") != "detection":
            return None
        try:
            finding = self._hydrate_detection_finding(row)
            matches = _matching_cases(finding, open=True) if finding else []
            # A Case the alert was taken off is not what an expiry may name.
            if finding and finding.get("finding_id") and matches:
                removed = _removed_from(finding["finding_id"])
                matches = [m for m in matches if m.case_id not in removed]
        except Exception:
            logger.warning(
                "Failed to match expiring row %s", row.get("id"), exc_info=True
            )
            return None
        return matches[0].case_id if matches else None

    def _merge_if_overlaps(
        self,
        finding: Dict,
        trigger_id: Optional[int],
        links: Optional[List[CaseMatch]] = None,
    ) -> bool:
        """True when this row is not launching this tick (merged, or held).

        Open Cases the finding matches without attaching (an IP that was only a
        destination there, or whose role was never recorded) are added to
        ``links``, for the new Case to link to once it exists.

        A failed read or attach is not a merge: no ``dedup_prevented`` bump, no
        ``_decide_trigger``. Returning True still skips launch so the row
        stays ``queued`` and the next tick retries.

        A finding already on a not-closed Case merges into it (#2245), and so
        does one that matches an open Case's entities (``matching_cases``) as a
        source, hash or hostname, whether or not that Case's run has finished. A closed Case does not
        count: the finding reopens.
        """
        finding_id = finding.get("finding_id", "unknown")
        try:
            holding = open_case_holding(finding_id)
        except Exception as e:
            logger.error(
                "Failed to read open cases for finding %s; leaving queued: %s",
                finding_id,
                e,
            )
            return True
        if holding:
            self._count_dedup_prevented()
            self._decide_trigger(
                trigger_id,
                state="merged",
                reason="finding_on_open_case",
                merged_into=holding,
            )
            return True

        try:
            # A Case a person took this finding off is not a match: the next
            # match, or none, and the finding launches as it would unmatched.
            removed = _removed_from(finding_id)
            matches = [
                m
                for m in _matching_cases(finding, open=True)
                if m.case_id not in removed
            ]
        except Exception as e:
            logger.error(
                "Failed to match finding %s to open cases; leaving queued: %s",
                finding_id,
                e,
            )
            return True
        match = next((m for m in matches if m.attaches), None)
        if match is None:
            if links is not None:
                links += matches[:MAX_OPEN_LINKS]
            return False
        if not _attach_finding(match.case_id, finding_id):
            logger.warning(
                "Finding %s matches case %s but could not be attached; leaving queued",
                finding_id,
                match.case_id,
            )
            return True
        logger.info(
            "Finding %s attached to case %s, %s",
            finding_id,
            match.case_id,
            _link_note(match),
        )
        # The merge stands whatever happens below: the finding is on the Case.
        for what, write in (
            (
                "note it on the case",
                lambda: _record_merge_activity(match.case_id, finding, match),
            ),
            (
                "raise the case priority",
                lambda: _raise_case_priority(match.case_id, finding.get("severity")),
            ),
            (
                "tell the people on it",
                lambda: _notify_joined(match.case_id, finding, match),
            ),
        ):
            try:
                write()
            except Exception as e:
                logger.exception(
                    "Finding %s merged into case %s but could not %s",
                    finding_id,
                    match.case_id,
                    what,
                )
                if what == "note it on the case":
                    _record_failure(
                        match.case_id,
                        f"Alert `{finding.get('title') or finding_id}` was added "
                        f"({_link_note(match)}) but its note could not be written: {e}",
                        {"finding_id": finding_id, "matched_key": match.key},
                    )
        self._count_dedup_prevented()
        self._decide_trigger(
            trigger_id,
            state="merged",
            reason="entity_on_open_case",
            merged_into=match.case_id,
        )
        return True

    def _count_dedup_prevented(self) -> None:
        self.stats["dedup_prevented"] += 1
        if _dedup_prevented is not None:
            _dedup_prevented.add(1)

    async def _process_intake_row(self, row: Dict, shutdown_event: asyncio.Event):
        kind = row.get("kind")
        trigger_id = row.get("id")
        if kind == "detection":
            finding = (
                row["_finding"]
                if "_finding" in row
                else self._hydrate_detection_finding(row)
            )
            if finding is None:
                logger.warning(
                    "intake row %s has no finding to launch; leaving queued", trigger_id
                )
                return
            if "_lead" in row:
                lead = row["_lead"]
            else:
                held, lead = self._lead_or_attach(finding, trigger_id)
                if held:
                    return
            # An earlier row this drain may have started the hunt on its account.
            if lead is not None and self._attach_if_hunting(
                finding, lead.accounts, trigger_id
            ):
                return
            await self._create_investigation_for_finding(
                finding, shutdown_event, trigger_id=trigger_id, lead=lead
            )
        elif kind in ("schedule", "human_ask"):
            item = dict(row.get("payload") or {})
            item["priority"] = row.get("priority") or item.get("priority") or "medium"
            if kind == "schedule":
                item.setdefault("trigger_type", "scheduled")
            else:
                # Only the daily gaps run hands a run its gap proposals. A person's
                # Emulate (the MITRE analyst) is the one Human Ask with no Case.
                item.pop("gap_proposal_ids", None)
                if item.get("workflow_id") != EMULATE_WORKFLOW:
                    item.pop("no_case", None)
            await self._create_manual_investigation(
                item, shutdown_event, trigger_id=trigger_id
            )
        else:
            logger.warning(f"Unknown intake kind: {kind}")

    async def _create_investigation_for_finding(
        self,
        finding: Dict,
        shutdown_event: asyncio.Event,
        trigger_id: Optional[int] = None,
        lead: Optional[InsiderLead] = None,
    ):
        """Create an investigation for a finding, with dedup checks. A ``lead`` (the
        row's, checked before any merge) hunts on its account in a case of its own."""
        # Same band the ranker uses: empty or a name that is not a rating
        # is unknown, not an invented medium. A detection can arrive from
        # Gate 1 or from a live feed hit; neither path invents a medium.
        priority = intake_severity_band(
            "detection", finding_severity=finding.get("severity")
        )

        if lead is not None:
            finding_id = finding.get("finding_id")
            await self._create_investigation(
                workflow_id=THREAT_HUNT_WORKFLOW_ID,
                findings=[finding],
                trigger_type=LEAD_TRIGGER,
                priority=priority,
                mint_case=CaseSpec(
                    title=_infer_title(finding, THREAT_HUNT_WORKFLOW_ID)[:200],
                    finding_ids=[finding_id] if finding_id else [],
                    priority=priority,
                    readers=lead.readers,
                    reader_teams=lead.reader_teams,
                    note=lead.why,
                ),
                hypothesis=lead.hypothesis,
                hypothesis_subjects=lead.subjects,
                shutdown_event=shutdown_event,
                trigger_id=trigger_id,
                trigger_reason=lead.why,
            )
            return

        open_links: List[CaseMatch] = []
        if self._merge_if_overlaps(finding, trigger_id, open_links):
            return

        workflow_id = select_workflow(finding)
        if not is_enabled(workflow_id):
            logger.info(
                "workflow %s is turned off; routing finding %s to %s",
                workflow_id,
                finding.get("finding_id"),
                FALLBACK_WORKFLOW,
            )
            workflow_id = FALLBACK_WORKFLOW
        finding_id = finding.get("finding_id")
        await self._create_investigation(
            workflow_id=workflow_id,
            findings=[finding],
            trigger_type="finding",
            priority=priority,
            mint_case=CaseSpec(
                title=_infer_title(finding, workflow_id)[:200],
                finding_ids=[finding_id] if finding_id else [],
                priority=priority,
            ),
            shutdown_event=shutdown_event,
            trigger_id=trigger_id,
            open_links=open_links,
        )

    async def _create_manual_investigation(
        self,
        item: Dict,
        shutdown_event: asyncio.Event,
        trigger_id: Optional[int] = None,
    ):
        """Create an investigation from a manual request."""
        workflow_id = item.get("workflow_id", "incident-response")
        finding_ids = item.get("finding_ids") or []
        case_id = item.get("case_id") or None
        hypothesis = item.get("hypothesis")
        hypothesis_subjects = item.get("hypothesis_subjects")

        findings = [f for f in map(_load_finding, finding_ids) if f]

        mint = None
        # The daily gaps run keeps its record on the run and its detection
        # proposals, and an Emulate on the run's coverage report; every other
        # intake with no case_id gets a Case of its own.
        if not case_id and not item.get("no_case"):
            mint = CaseSpec(
                title=_human_ask_case_title(hypothesis, findings, workflow_id),
                finding_ids=list(finding_ids),
                priority=item.get("priority") or "medium",
            )

        await self._create_investigation(
            workflow_id=workflow_id,
            findings=findings,
            # The intake is shared, so what put the item on it is the item's to say.
            trigger_type=item.get("trigger_type") or "manual",
            priority=item.get("priority", "medium"),
            case_id=case_id,
            mint_case=mint,
            hypothesis=hypothesis,
            hypothesis_subjects=hypothesis_subjects,
            shutdown_event=shutdown_event,
            trigger_id=trigger_id,
            document=item.get("document"),
            gap_proposal_ids=item.get("gap_proposal_ids"),
        )

    async def _create_investigation(
        self,
        workflow_id: str,
        findings: List[Dict],
        trigger_type: str,
        priority: str,
        case_id: Optional[str] = None,
        mint_case: Optional[CaseSpec] = None,
        hypothesis: Optional[str] = None,
        hypothesis_subjects: Optional[Dict[str, List[str]]] = None,
        shutdown_event: Optional[asyncio.Event] = None,
        trigger_id: Optional[int] = None,
        document: Optional[str] = None,
        gap_proposal_ids: Optional[List[str]] = None,
        open_links: Optional[List[CaseMatch]] = None,
        trigger_reason: Optional[str] = None,
    ):
        """Core investigation creation logic."""
        if mint_case is not None:
            case_id = case_id or mint_case.case_id or _new_case_id()
            mint_case = replace(mint_case, case_id=case_id)
        if findings and not case_id:
            raise ValueError("a run opened on findings needs a case")

        inv_id = f"inv-{utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
        workdir = self.workdir.create(inv_id)

        state = generate_initial_state(inv_id, workflow_id, case_id, findings)
        self.workdir.write_state(inv_id, state)

        self.workdir.write_file(
            inv_id,
            "context.md",
            generate_initial_context(
                findings,
                case_id,
                document,
                # One agent on its own has no board; its goal rides in the brief.
                goal=hypothesis if workflow_id.startswith(AGENT_RUN_PREFIX) else None,
            ),
        )
        # What this run is about, so the harness's keyed read has something to ask
        # on. Written even when empty: an investigation whose findings name no
        # entity asked nothing rather than being handed keys nobody minted.
        self.workdir.write_file(
            inv_id, "recall_keys.json", json.dumps(finding_entity_keys(findings))
        )
        # Beside the context and read back the same way at enqueue: what the hunt was
        # opened to test reaches its board as a hypothesis, not as prose in the brief.
        if hypothesis:
            self.workdir.write_file(inv_id, "hypotheses.txt", hypothesis)
        # What each of those claims is about, keyed by the claim. Stated by the
        # caller or absent: nothing here reads a finding and guesses, because a
        # guessed subject and a stated one are the same bytes downstream, and a
        # Verdict recalled under an entity that was never in the claim is worse
        # than one recalled under nothing.
        if hypothesis and hypothesis_subjects:
            self.workdir.write_file(
                inv_id, "hypothesis_subjects.json", json.dumps(hypothesis_subjects)
            )
        # The daily gaps run's proposals, read back at enqueue onto the run's
        # trigger_context, where a rule the run saves finds the gap it answers.
        if gap_proposal_ids:
            self.workdir.write_file(
                inv_id, GAP_PROPOSALS_FILE, json.dumps(list(gap_proposal_ids))
            )
        # The shadow run's one hypothesis, derived here where the finding is in
        # hand and read back at enqueue, which sees only ids. Only a detection
        # finding gets a shadow; a manual or scheduled run has nothing to adjudicate.
        if trigger_type == "finding" and is_enabled(SHADOW_WORKFLOW_ID):
            try:
                line = _shadow_hypothesis(findings)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — an odd finding costs the shadow, not intake
                logger.warning("no shadow hypothesis for %s: %s", inv_id, exc)
                line = ""
            self.workdir.write_file(inv_id, SHADOW_HYPOTHESIS_FILE, line)

        shutting_down = shutdown_event is not None and shutdown_event.is_set()

        # Start root investigation span — will be the parent for all agent spans
        _inv_span = None
        _tp = ""
        try:
            if _tracer is not None:
                _inv_span = _tracer.start_span(
                    "investigation",
                    kind=SpanKind.INTERNAL,
                    attributes={
                        "vigil.investigation.id": inv_id,
                        "vigil.investigation.workflow_id": workflow_id,
                        "vigil.investigation.trigger_type": trigger_type,
                        "vigil.investigation.priority": priority,
                        "vigil.investigation.finding_count": len(findings),
                    },
                )
                _tp_carrier: Dict = {}
                inject_traceparent(_tp_carrier)
                _tp = _tp_carrier.get("traceparent", "")
        except Exception:
            pass

        inv_record = {
            "investigation_id": inv_id,
            "case_id": case_id,
            "workflow_id": workflow_id,
            "trigger_type": trigger_type,
            "trigger_ids": [
                f.get("finding_id") for f in findings if f.get("finding_id")
            ],
            "status": "assigned",
            "workdir": str(workdir),
            "current_step": 1,
            "priority": priority,
            "max_iterations": self.config.max_iterations_per_agent,
            "max_cost_usd": self._max_cost(),
            "max_runtime_seconds": self._max_runtime(),
            "otel_traceparent": _tp,
            "run_id": run_id_for(inv_id),
        }

        saved = self._save_investigation(
            inv_record,
            trigger_id=trigger_id,
            mint_case=mint_case,
            document=document,
            reason=trigger_reason,
        )
        if not saved:
            logger.warning(
                "intake row %s already decided; not launching %s",
                trigger_id,
                inv_id,
            )
            return

        _reindex_case(case_id)
        self.stats["investigations_created"] += 1
        if _inv_created is not None:
            _inv_created.add(1)

        try:
            if _inv_span is not None:
                _inv_span.end()
        except Exception:
            pass

        self.workdir.append_log(
            inv_id,
            {
                "event": "investigation_created",
                "workflow_id": workflow_id,
                "trigger_type": trigger_type,
                "finding_count": len(findings),
            },
        )

        logger.info(
            f"Created investigation {inv_id} (workflow={workflow_id}, priority={priority})"
        )

        if trigger_type == "finding" and inv_record.get("case_id"):
            for finding in findings:
                await self._link_related_cases(
                    inv_record["case_id"], finding, open_links or []
                )

        if not self.config.dry_run and not shutting_down:
            await self._enqueue_investigation(inv_record)

    async def _pickup_assigned_investigations(self, shutdown_event: asyncio.Event):
        """Re-enqueue assigned investigations after a restart."""
        if self.config.dry_run or self._hourly_budget_exhausted():
            return

        for inv in self._get_investigations_by_status("assigned"):
            inv_id = inv.get("investigation_id")
            if not inv_id:
                continue
            # Read fresh: the loop outlives the switch, so off stops the next enqueue.
            self._sync_enabled_from_db()
            if (
                not self._enabled
                or self._running() >= self.config.max_concurrent_agents
            ):
                return

            self._update_investigation_status(inv_id, "assigned")
            await self._enqueue_investigation(inv)

    # -------------------------------------------------------------------------
    # Supervision Loop
    # -------------------------------------------------------------------------

    async def _supervision_loop(self, shutdown_event: asyncio.Event):
        """Monitor running agents for stuck/runaway conditions."""
        while not shutdown_event.is_set():
            try:
                # Before anything reads a row: the row is a copy of the ledger,
                # and a supervisor deciding on a stale copy decides on nothing.
                # Paused rows too: a resume or the park TTL's terminal reaches
                # them only here. They are never stale- or cost-checked below.
                for status in ("executing", "waiting_approval", "paused"):
                    for inv in self._get_investigations_by_status(status):
                        reconciling = inv.get("investigation_id")
                        if reconciling:
                            await self._reconcile(reconciling)

                waiting = self._get_investigations_by_status("waiting_approval")
                for inv in waiting:
                    w_inv_id = inv.get("investigation_id")
                    if not w_inv_id:
                        continue
                    notified_key = f"approval_notified:{w_inv_id}"
                    if not hasattr(self, "_notified_approvals"):
                        self._notified_approvals: set = set()
                    if notified_key not in self._notified_approvals:
                        self._notified_approvals.add(notified_key)
                        self._send_notification(
                            w_inv_id,
                            "approval_required",
                            f"Approval required: {w_inv_id}",
                            f"Investigation {w_inv_id} is waiting for human approval of a restricted tool.",
                            priority="high",
                        )
                        await self._send_slack_for_notification(
                            f"Approval required: {w_inv_id}",
                            f"Investigation {w_inv_id} is paused pending human approval.",
                        )

                # A run whose start job was lost has no lease for the worker's
                # sweeper to find: ended here, as a failed enqueue is. About once a
                # minute, since it scans the runs table.
                if (
                    not self.config.dry_run
                    and time.monotonic() - self._lost_checked_at >= LOST_CHECK_EVERY
                ):
                    self._lost_checked_at = time.monotonic()
                    try:
                        self._lost_cursor = await end_lost_start_jobs(
                            self._lost_cursor, self._fail_lost_investigation
                        )
                    except Exception:  # noqa: BLE001 - must not skip the checks below
                        logger.exception("ending lost start jobs failed")

                executing = self._get_investigations_by_status("executing")
                now = utcnow()

                for inv_dict in executing:
                    inv_id = inv_dict.get("investigation_id")
                    if not inv_id:
                        continue

                    # Dump dicts carry "+00:00" strings; `now` is naive UTC.
                    last_activity = _as_naive_utc(inv_dict.get("last_activity_at"))
                    if last_activity:
                        idle_seconds = (now - last_activity).total_seconds()
                        if idle_seconds > self.config.stale_threshold:
                            logger.warning(
                                "supervisor.kill stale inv_id=%s idle_s=%.0f "
                                "iteration=%s current_activity=%r cost_usd=%.4f "
                                "stale_threshold=%s",
                                inv_id,
                                idle_seconds,
                                inv_dict.get("iteration_count"),
                                inv_dict.get("current_activity"),
                                inv_dict.get("cost_usd", 0.0),
                                self.config.stale_threshold,
                            )
                            self._update_investigation_status(
                                inv_id, "failed", "Stale: no activity"
                            )
                            self.stats["stuck_agents_killed"] += 1
                            if _stuck_agents is not None:
                                _stuck_agents.add(1)
                            self._send_notification(
                                inv_id,
                                "agent_stuck",
                                f"Agent stuck: {inv_id}",
                                f"Agent for investigation {inv_id} was idle for {idle_seconds:.0f}s and has been terminated.",
                                priority="high",
                            )
                            await self._send_slack_for_notification(
                                f"Agent stuck: {inv_id}",
                                f"Agent idle for {idle_seconds:.0f}s, terminated.",
                            )

                    cost = inv_dict.get("cost_usd", 0.0)
                    max_cost = inv_dict.get("max_cost_usd")
                    if max_cost is None:
                        max_cost = self._max_cost()
                    if cost >= max_cost:
                        logger.warning(
                            "supervisor.kill cost inv_id=%s cost_usd=%.4f "
                            "max_cost_usd=%.4f iteration=%s current_activity=%r",
                            inv_id,
                            cost,
                            max_cost,
                            inv_dict.get("iteration_count"),
                            inv_dict.get("current_activity"),
                        )
                        # The budget seam refuses the next call at the same
                        # ceiling; this is the record catching up, not the kill.
                        self._update_investigation_status(
                            inv_id, "failed", "Cost budget exceeded"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Supervision loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval // 2)

    # -------------------------------------------------------------------------
    # The run: enqueued here, driven by the agent worker, read back as a projection
    # -------------------------------------------------------------------------

    IN_FLIGHT = IN_FLIGHT_INVESTIGATION_STATUSES

    def _fail_lost_investigation(self, inv_id: str) -> None:
        try:
            self._update_investigation_status(
                inv_id, "failed", "Could not enqueue: the start job is no longer queued"
            )
        except Exception:  # noqa: BLE001 - the run is ended either way
            logger.exception("could not fail investigation %s", inv_id)

    def _in_flight(self) -> int:
        try:
            return _count_investigations_in_flight()
        except Exception as e:
            # Unknown reads as full: a failed count must not lift the cap.
            logger.error("Failed to count in-flight investigations: %s", e)
            return self.config.max_concurrent_agents

    def _running(self) -> int:
        """Rows holding a slot by actually running, for admission checks.

        `assigned` rows are excluded: resuming one does not change its
        in-flight count, so gating pickup or intake on `_in_flight()` lets
        stranded `assigned` rows hold every slot forever (#1874).
        """
        try:
            return _count_investigations_running()
        except Exception as e:
            # Unknown reads as full: a failed count must not lift the cap.
            logger.error("Failed to count running investigations: %s", e)
            return self.config.max_concurrent_agents

    # A run belongs to the worker, so nothing here stops one. The record is marked
    # and the run finishes or hits its ceiling; reaping a stalled worker is #633.
    def _abandon_in_flight(self) -> None:
        for status in self.IN_FLIGHT:
            for inv in self._get_investigations_by_status(status):
                inv_id = inv.get("investigation_id")
                if inv_id:
                    self._update_investigation_status(
                        inv_id,
                        "failed",
                        "Orchestrator stopped while the run was in flight",
                    )

    # Absent and unreadable answer the same, because both mean the run's inputs
    # are not there to be read, and a half-read investigation is not a better one.
    def _read_sidecar_json(self, inv_id: str, filename: str) -> Any:
        held = self.workdir.read_file(inv_id, filename)
        if not held:
            return None
        try:
            return json.loads(held)
        except ValueError:
            logger.warning("%s has an unreadable %s", inv_id, filename)
            return None

    # An empty map rather than None, because a JSON null reaches the agent layer
    # as a value where a missing key reads as unset.
    def _hypothesis_subjects(
        self, inv_id: str, stated: List[str]
    ) -> Dict[str, List[str]]:
        declared = self._read_sidecar_json(inv_id, "hypothesis_subjects.json")
        return {} if declared is None else kept_subjects(declared, stated)

    def _gap_proposal_ids(self, inv_id: str) -> List[str]:
        held = self._read_sidecar_json(inv_id, GAP_PROPOSALS_FILE)
        if not isinstance(held, list):
            return []
        return [i for i in held if isinstance(i, str) and i]

    # normalise_keys carries the absent, the non-list and the unusable to the same
    # empty answer, which is what a run that recalls nothing is.
    def _recall_keys(self, inv_id: str) -> List[str]:
        return normalise_keys(self._read_sidecar_json(inv_id, "recall_keys.json"))

    # WorkflowDefinition.run_kind, including its absent-field default of compose.
    # A missing workflow and a kind outside RUN_KINDS are refused, not coerced:
    # the caller's except marks the investigation failed rather than queueing a
    # run no worker has a loop for.
    def _declared_run_kind(self, workflow_id: str) -> str:
        workflow = self._workflows.get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"no such workflow: {workflow_id}")
        declared = workflow.run_kind
        if declared not in RUN_KINDS:
            raise ValueError(f"{workflow_id} declares unknown run_kind {declared!r}")
        return str(declared)

    async def _enqueue_investigation(self, inv_record: Dict) -> None:
        inv_id = inv_record["investigation_id"]
        run_id = inv_record.get("run_id") or run_id_for(inv_id)
        # One per line, as the console's run modal sends them. Empty for a workflow
        # that walks phases and has no board to put them on.
        hypotheses = [
            line.strip()
            for line in (
                self.workdir.read_file(inv_id, "hypotheses.txt") or ""
            ).splitlines()
            if line.strip()
        ]
        subjects = self._hypothesis_subjects(inv_id, hypotheses)
        request = {
            # The workflow resolves both layers, so no config path travels beside it.
            "playbook": f"workflow:{inv_record['workflow_id']}",
            "config": "",
            "arch": "",
            "prompt": self.workdir.read_file(inv_id, "context.md") or "",
            "hypotheses": hypotheses,
            "hypothesis_subjects": subjects,
            # What the run opens its episodic read on. A hunt derives its own from
            # the hypotheses being put up; an investigation has none, so its keys
            # are the entities the findings it was opened on carry.
            "recall_keys": self._recall_keys(inv_id),
            # ORCHESTRATOR_MAX_COST and ORCHESTRATOR_MAX_RUNTIME keep their meaning
            # as the ceilings the budget seam refuses the next call at.
            "overrides": {
                "budgets": {
                    "max_calls": self.config.max_iterations_per_agent,
                    # The run's own budgets, set from the live settings at
                    # creation; a record without them reads the live ones.
                    "max_cost_usd": (
                        inv_record.get("max_cost_usd") or self._max_cost()
                    ),
                    "max_wall_ms": (
                        inv_record.get("max_runtime_seconds") or self._max_runtime()
                    )
                    * 1000,
                }
            },
        }

        # Coverage, finalize_run and the listings read this row. A restart
        # re-enters with the same run id, and a refused write must not stop
        # the job being queued.
        runs = WorkflowRunService()
        row = None
        started_by, principal = _caller(inv_id)
        # A Human Ask whose sign-in ended for a stopping reason starts nothing.
        sid = _sid_of(principal)
        try:
            refused = bool(sid) and sign_ins.ended_for_compromise(sid)
        except Exception:  # noqa: BLE001 - an unreadable row is not a refusal
            logger.warning("could not read the sign-in of %s", inv_id)
            refused = False
        if refused:
            self._update_investigation_status(inv_id, "failed", sign_ins.STOP_REASON)
            return
        try:
            run_kind = self._declared_run_kind(inv_record["workflow_id"])
            # Loaded once: the row's display name, version and the job's metadata.
            workflow = self._workflows.get_workflow(inv_record["workflow_id"])
            try:
                if runs.get_run(run_id) is None:
                    context: Dict[str, Any] = {
                        "run_kind": run_kind,
                        "hypothesis_subjects": subjects,
                        "hypothesis": "\n".join(hypotheses),
                        "investigation_id": inv_id,
                    }
                    case_id = inv_record.get("case_id")
                    if case_id:
                        context["case_id"] = case_id
                    gap_proposal_ids = self._gap_proposal_ids(inv_id)
                    if gap_proposal_ids:
                        context["gap_proposal_ids"] = gap_proposal_ids
                    _stamp_phases(context, inv_record["workflow_id"], self._workflows)
                    row = runs.begin_run(
                        run_id=run_id,
                        workflow_id=inv_record["workflow_id"],
                        workflow_name=(
                            workflow.name if workflow else inv_record["workflow_id"]
                        ),
                        workflow_source="agent",
                        workflow_version=workflow.version if workflow else None,
                        trigger_context=context,
                        triggered_by=started_by or "orchestrator",
                        triggered_by_principal_id=_principal_of(started_by, principal),
                        sign_in_id=sid,
                    )
                    if row is None:
                        logger.warning("no workflow_runs row for %s", run_id)
                    elif gap_proposal_ids:
                        _note_gap_run(gap_proposal_ids, run_id)
                else:
                    logger.info("run %s already recorded; not rewriting", run_id)
            except Exception as exc:  # noqa: BLE001 — a refused row still enqueues
                logger.warning("no workflow_runs row for %s: %s", run_id, exc)
                row = None

            token, actor, origin, person = _start_principal(
                run_id, run_kind, started_by, principal, inv_record.get("trigger_type")
            )
            if row is not None:
                _record_start(run_id, actor, origin, person, request)
            # A custom agent's manifest and skills ride the job, not the trail entry.
            job = build_start_job(
                run_id,
                run_kind,
                {
                    **request,
                    **await asyncio.to_thread(
                        operator_request,
                        workflow.metadata if workflow else {},
                        run_kind,
                    ),
                },
                enqueued_by=started_by or "orchestrator",
                principal=token,
                actor=actor,
                origin=origin,
                case_id=inv_record.get("case_id"),
            )
            await enqueue_run(job)
        except Exception as exc:  # noqa: BLE001 — a queue that refuses is not a crash
            logger.error("could not enqueue %s: %s", inv_id, exc)
            # Otherwise coverage stays "running" for a hunt that never started.
            if row is not None:
                runs.finalize_run(
                    run_id, status="failed", error=f"Could not enqueue: {exc}"
                )
                from core.agents.attribution import note_terminal

                note_terminal(run_id, "failed", "")
            self._update_investigation_status(
                inv_id, "failed", f"Could not enqueue: {exc}"
            )
            return

        self._update_investigation_status(inv_id, "executing")
        logger.info("enqueued investigation %s as run %s", inv_id, run_id)

        # After the real run is queued and its row says so: a shadow that fails
        # must never mark the investigation failed or hold up its transition.
        try:
            await self._enqueue_shadow_adjudication(
                inv_record, request, started_by=started_by, principal=principal
            )
        except Exception as exc:  # noqa: BLE001 — the shadow is best-effort
            logger.error(
                "could not enqueue shadow adjudication for %s: %s", inv_id, exc
            )

    # Shadow mode (#880): one more job over the same finding, on the adjudicate
    # loop, that executes nothing and journals which workflow it would have run.
    # No investigations row and no AIDecisionLog entry: the ledger is its record,
    # and the workflow_runs row is what lets Workflows History and replay reach it.
    async def _enqueue_shadow_adjudication(
        self,
        inv_record: Dict,
        request: Dict[str, Any],
        started_by: Optional[str] = None,
        principal: Optional[str] = None,
    ) -> None:
        if not is_enabled(SHADOW_WORKFLOW_ID):
            return
        if inv_record.get("trigger_type") != "finding":
            return
        inv_id = inv_record["investigation_id"]
        hypothesis = (
            self.workdir.read_file(inv_id, SHADOW_HYPOTHESIS_FILE) or ""
        ).strip()
        if not hypothesis:
            # The definition refuses a run with none stated; nothing here reads the
            # finding back from the DB to invent one.
            logger.info("no shadow hypothesis for %s; no shadow run", inv_id)
            return

        intake_workflow = inv_record["workflow_id"]
        shadow_id = shadow_run_id_for(inv_id)
        # The same brief, budgets and recall keys the real run got, so the two
        # decisions are over the same evidence; only the framing differs.
        shadow = copy.deepcopy(request)
        shadow["playbook"] = f"workflow:{SHADOW_WORKFLOW_ID}"
        shadow["prompt"] = (
            f"{request['prompt']}\n\n## Intake's choice\n\n"
            f"Intake admitted this finding and chose the `{intake_workflow}` "
            "workflow. Decide independently whether the finding is what intake "
            "says it is and which catalogue workflow it warrants; execute nothing."
        )
        shadow["hypotheses"] = [hypothesis]
        # Stated, not guessed: the claim names these very entities.
        keys = list(request.get("recall_keys") or [])
        shadow["hypothesis_subjects"] = {hypothesis: keys} if keys else {}

        runs = WorkflowRunService()
        # A restart re-enqueues assigned investigations; a shadow already on
        # record is not started twice.
        if runs.get_run(shadow_id) is not None:
            logger.info("shadow run %s already recorded; not re-enqueued", shadow_id)
            return
        # Row first, as the API does: an answerable checkpoint needs it to exist.
        # begin_run returns None on a DB failure rather than raising.
        row = runs.begin_run(
            run_id=shadow_id,
            workflow_id=SHADOW_WORKFLOW_ID,
            workflow_name=SHADOW_WORKFLOW_ID,
            workflow_source="agent",
            workflow_version=self._workflows.version_of(SHADOW_WORKFLOW_ID),
            trigger_context={
                "run_kind": "adjudicate",
                "investigation_id": inv_id,
                "intake_workflow_id": intake_workflow,
            },
            triggered_by="orchestrator",
            triggered_by_principal_id=_principal_of(started_by, principal),
        )
        if row is None:
            logger.warning("no workflow_runs row for shadow run %s", shadow_id)

        # The shadow is the system's second opinion, not the person's run.
        token, actor, origin, person = _start_principal(
            shadow_id, "adjudicate", None, None, "finding"
        )
        if row is not None:
            try:
                _record_start(shadow_id, actor, origin, person, shadow)
            except (
                Exception
            ):  # noqa: BLE001 - a refused trail must not leave the row running
                runs.finalize_run(
                    shadow_id,
                    status="failed",
                    error="the action could not be recorded",
                )
                raise
        job = build_start_job(
            shadow_id,
            "adjudicate",
            # The skills an operator added ride the job, not the trail entry.
            {**shadow, **await asyncio.to_thread(added_request, "adjudicate")},
            enqueued_by=started_by or "orchestrator",
            principal=token,
            actor=actor,
            origin=origin,
        )
        try:
            await enqueue_run(job, job_id=shadow_id)
        except Exception as exc:
            # Otherwise the row reads as running forever with no job behind it.
            if row is not None:
                runs.finalize_run(
                    shadow_id, status="failed", error=f"Could not enqueue: {exc}"
                )
                from core.agents.attribution import note_terminal

                note_terminal(shadow_id, "failed", "")
            raise
        logger.info("enqueued shadow adjudication of %s as run %s", inv_id, shadow_id)

    # The ledger is the record and this row is the copy an operator reads, so the
    # copy is reconciled from the projection rather than written alongside it.
    async def _reconcile(self, inv_id: str) -> None:
        projection = await read_projection(run_id_for(inv_id))
        if projection is None:
            return

        self._record_progress(inv_id, projection)
        checkpoint = projection.get("open_checkpoint")
        if checkpoint:
            self._raise_run_approval(inv_id, checkpoint)
            self._update_investigation_status(inv_id, "waiting_approval")
            return

        # A hunt parked without a checkpoint (out of turns or budget) waits on a
        # person: paused, holding no slot, and announced once per park.
        if _parked_on_a_person(projection):
            reason = projection.get("reason") or "Parked"
            was = self._update_investigation_status(inv_id, "paused", activity=reason)
            if was is not None and was != "paused":
                await self._notify_parked(inv_id, reason)
            return

        if projection.get("status") != "terminal":
            self._update_investigation_status(inv_id, "executing")
            return

        outcome = projection.get("outcome")
        reason = projection.get("reason") or ""
        if outcome == "completed":
            await self._complete_investigation(inv_id, reason)
        else:
            self._update_investigation_status(inv_id, "failed", reason or str(outcome))

    async def _notify_parked(self, inv_id: str, reason: str) -> None:
        title = f"Run paused: {inv_id}"
        body = f"Investigation {inv_id} is paused and waiting on a person: {reason}"
        self._send_notification(inv_id, "run_paused", title, body, priority="high")
        await self._send_slack_for_notification(title, body)

    # The heartbeat. A failure here is logged loudly on purpose (#147): swallowed
    # quietly, last_activity_at stops moving and the supervisor stale-kills a run
    # that is perfectly healthy. A run parked on a person is not stamped, with or
    # without a checkpoint, so what it spent leaves the hourly window instead of
    # pausing intake until someone answers. The first reconcile after a resume
    # stamps it again.
    def _record_progress(self, inv_id: str, projection: Dict[str, Any]) -> None:
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .first()
                )
                if inv is None:
                    logger.warning("progress for %s: row not found", inv_id)
                    return
                # Hunt folds report `iteration`; lead folds report `iterations`.
                counted = projection.get("iterations")
                inv.iteration_count = (
                    projection.get("iteration", 0) if counted is None else counted
                )
                if not (
                    _parked_on_a_person(projection) or projection.get("open_checkpoint")
                ):
                    inv.last_activity_at = utcnow()
                # Null is "the gateway priced nothing", which is not zero spent.
                cost = projection.get("cost_usd")
                if cost is not None:
                    inv.cost_usd = float(cost)
        except Exception:
            logger.error("DB update for %s failed", inv_id, exc_info=True)

    def _raise_run_approval(self, inv_id: str, checkpoint: Dict[str, Any]) -> None:
        question = checkpoint.get("question") or "Approve this action?"
        raise_for_checkpoint(
            run_id=run_id_for(inv_id),
            checkpoint_id=str(checkpoint.get("checkpoint_id")),
            title=f"Approval required: {inv_id}",
            description=question,
            reason="The investigation parked on a call that needs a human",
            parameters={"investigation_id": inv_id},
        )

    def _hourly_cost(self) -> Optional[float]:
        """Recorded cost of investigations active in the last hour; None if unreadable.

        Keyed on last_activity_at, which every reconcile of an in-flight run
        stamps, so in-flight runs count in full and a finished run drops out an
        hour after its last update. An unpriced run is stored as 0.0 (#985).
        """
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            cutoff = utcnow() - timedelta(hours=1)
            with get_db_manager().session_scope() as session:
                total = (
                    session.query(func.sum(Investigation.cost_usd))
                    .filter(Investigation.last_activity_at >= cutoff)
                    .scalar()
                )
            return float(total or 0.0)
        except Exception as e:
            logger.error(f"Failed to read hourly cost: {e}")
            return None

    def _live_setting(self, name: str, cast, fallback):
        """One value as saved in ``orchestrator.settings``, else startup config.

        Read live so a saved change (Settings, or an applied INTENT.md
        revision) reaches the running daemon at once.
        """
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import SystemConfig

            with get_db_manager().session_scope() as session:
                cfg = (
                    session.query(SystemConfig)
                    .filter_by(key="orchestrator.settings")
                    .first()
                )
                if cfg and isinstance(cfg.value, dict):
                    saved = cfg.value.get(name)
                    if saved is not None:
                        return cast(saved)
        except Exception as e:
            logger.warning("Orchestrator %s read failed, using config: %s", name, e)
        return fallback

    def _hourly_cost_limit(self) -> float:
        return self._live_setting(
            "max_total_hourly_cost", float, self.config.max_total_hourly_cost
        )

    def _max_cost(self) -> float:
        return self._live_setting(
            "max_cost_per_investigation",
            float,
            self.config.max_cost_per_investigation,
        )

    def _max_runtime(self) -> int:
        return self._live_setting(
            "max_runtime_per_investigation",
            int,
            self.config.max_runtime_per_investigation,
        )

    def _hourly_budget_exhausted(self) -> bool:
        """Gate intake on the rolling hour; logs once per pause/resume transition.

        Not a write to _enabled: _sync_enabled_from_db would undo it within 5s.
        """
        spent = self._hourly_cost()
        limit = self._hourly_cost_limit()
        paused = self._hourly_pause_decision(spent, limit)
        if spent is None:
            return paused
        if paused != getattr(self, "_hourly_paused", False):
            self._hourly_paused = paused
            if paused:
                logger.warning(
                    f"Hourly cost ${spent:.4f} reached limit ${limit:.4f}, pausing intake"
                )
            else:
                logger.info(
                    f"Hourly cost ${spent:.4f} below limit ${limit:.4f}, resuming intake"
                )
        return paused

    def _hourly_pause_decision(self, spent: Optional[float], limit: float) -> bool:
        # Unknown spend reads as over the cap, including on a cold start.
        return spent is None or spent >= limit

    # Its own loop rather than a step of the supervision one: the Distils are not
    # supervising anything, they read terminals and closures the rest of the
    # system has already left behind. Slower than the others because nothing
    # waits on either — memory is read at the start of the next run, so a write
    # that lands minutes later is a write that lands in time.
    async def _distil_loop(self, shutdown_event: asyncio.Event):
        """Turn finished hunts and closed cases into episodic memory (#731, #733)."""
        from core.memory.distil import distil_once

        while not shutdown_event.is_set():
            try:
                # Both on this tick rather than in a loop of its own: neither is
                # supervising anything, both poll for work already finished, and
                # a second loop would be a second interval to keep in step.
                await self._case_distil_tick()

                written = await distil_once()
                if written["investigations"]:
                    logger.info(
                        "Distilled %s investigation(s): %s sightings, %s verdicts, %s gaps",
                        written["investigations"],
                        written["sightings"],
                        written["verdicts"],
                        written["gaps"],
                    )
                # Surfaced here and not only where they happened: one refusal in
                # a log is a line nobody reads, and a tick that wrote nothing
                # because everything failed must not look like a tick with
                # nothing to do.
                if written["refused"] or written["unreadable"] or written["failed"]:
                    logger.warning(
                        "Distil left %s investigation(s) undistilled: "
                        "%s refused, %s unreadable, %s failed",
                        written["refused"] + written["unreadable"] + written["failed"],
                        written["refused"],
                        written["unreadable"],
                        written["failed"],
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Logged and retried rather than swallowed into a quiet stop: a
                # Distil that dies silently is a memory that reads as empty
                # rather than as broken, and empty is what an entity nobody has
                # looked at also reads as.
                logger.error(f"Distil loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval)

    async def _run_retry_loop(self, shutdown_event: asyncio.Event):
        """Run again each case investigation that failed for a passing reason."""
        while not shutdown_event.is_set():
            try:
                # A dry run opens no runs, so a retry would never stop being due.
                if self._enabled and not self.config.dry_run:
                    settings = get_settings()
                    await self._run_retry_tick(
                        timedelta(seconds=settings.case_run_retry_after_seconds),
                        settings.case_run_retries,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Run retry loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval)

    async def _indicator_catchup_loop(self, shutdown_event: asyncio.Event):
        """Reconcile existing, hand-opened and drifted Cases into the shared IOC index (#4734).

        Runs whether AutoOps is on or off: intake joins alerts through the index.
        """
        from core.storage.indicator_catchup import catch_up_once

        while not shutdown_event.is_set():
            try:
                started = time.monotonic()
                done = await asyncio.to_thread(catch_up_once)
                if done.reconciled or done.failed:
                    logger.info(
                        "Indicator catch-up: %s reconciled, %s failed, %s skipped in %.2fs",
                        done.reconciled,
                        done.failed,
                        done.skipped,
                        time.monotonic() - started,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Indicator catch-up loop error: {e}", exc_info=True)

            await self._sleep(shutdown_event, self.config.loop_interval)

    async def _run_retry_tick(self, after: timedelta, attempts: int) -> int:
        """Launch again each case investigation that failed for a passing reason
        (core/cases/run_retry.py), through the same path intake uses, while there
        is capacity and budget."""
        from core.cases.run_retry import note_retry, retries_due

        def due():
            with get_db_manager().session_scope() as session:
                return retries_due(session, after, attempts)

        launched = 0
        for item in await asyncio.to_thread(due):
            if self._running() >= self.config.max_concurrent_agents:
                break
            if self._hourly_budget_exhausted():
                break
            findings = [
                lift_ai_enrichment(f)
                for f in await asyncio.to_thread(
                    lambda: [_load_finding(i) for i in item["finding_ids"]]
                )
                if f
            ]
            if not findings:
                continue
            workflow_id = item["workflow_id"]
            if not is_enabled(workflow_id):
                workflow_id = FALLBACK_WORKFLOW
            await self._create_investigation(
                workflow_id=workflow_id,
                findings=findings,
                trigger_type="finding",
                priority=item["priority"],
                case_id=item["case_id"],
                shutdown_event=self._shutdown_event,
            )

            def note():
                with get_db_manager().session_scope() as session:
                    note_retry(session, item)

            try:
                await asyncio.to_thread(note)
            except Exception:  # noqa: BLE001 — the run launched either way
                logger.warning("could not note the retry on %s", item["case_id"])
            logger.info(
                "Case %s run %s failed (%s); launched again",
                item["case_id"],
                item["run_id"],
                str(item["reason"])[:120],
            )
            launched += 1
        return launched

    async def _case_distil_tick(self):
        """Turn closed Cases into Verdicts (#733).

        Its own method so that a failure here costs the Case Distil and not the
        hunt one: they write different rows from different sources, and a
        closure this job cannot map must not stop a finished hunt reaching
        memory.
        """
        from core.memory.case_distil import case_distil_once

        try:
            written = await case_distil_once()
        except Exception as e:
            logger.error(f"Case Distil error: {e}", exc_info=True)
            return

        if written["cases"]:
            logger.info(
                "Distilled %s closed case(s): %s verdicts",
                written["cases"],
                written["verdicts"],
            )
        if written["withdrawn"]:
            logger.info(
                "Withdrew %s Verdict(s) from reopened case(s)", written["withdrawn"]
            )
        if written["refused"] or written["failed"]:
            logger.warning(
                "Case Distil left %s case(s) undistilled: %s refused, %s failed",
                written["refused"] + written["failed"],
                written["refused"],
                written["failed"],
            )

    async def _complete_legacy_review_rows(self) -> None:
        """Finish rows a pre-#2324 daemon left in ``review_submitted``."""
        for inv in self._get_investigations_by_status("review_submitted"):
            inv_id = inv.get("investigation_id")
            if not inv_id:
                continue
            # One bad row must not keep the loops from starting.
            try:
                await self._complete_investigation(inv_id)
            except Exception:
                logger.error("Completing %s failed", inv_id, exc_info=True)

    async def _complete_investigation(
        self, inv_id: str, notes: Optional[str] = None
    ) -> None:
        """Finish a run whose terminal outcome is ``completed``.

        That outcome is the review (#1085); there is no second one. The status
        is claimed first so a later reconcile of the same run does nothing.
        """
        if not self._claim_completion(inv_id, notes):
            return

        state = self.workdir.read_state(inv_id)

        self.stats["investigations_completed"] += 1
        if _inv_completed is not None:
            _inv_completed.add(1)
        _reindex_case(state.get("case_id"))

        self.workdir.append_log(inv_id, {"event": "investigation_completed"})

        logger.info(f"Investigation {inv_id} COMPLETED")

        self._send_notification(
            inv_id,
            "investigation_complete",
            f"Investigation {inv_id} completed",
            "Investigation completed.",
            priority="normal",
        )

    # -------------------------------------------------------------------------
    # Related-Case links
    # -------------------------------------------------------------------------

    async def _link_related_cases(
        self, case_id: str, finding: Dict, open_links: List[CaseMatch]
    ):
        """Link a new Case to the Cases its finding matches without attaching.

        The most recent closed ones (looked up now), then the open ones intake
        carried: one that closed since is already linked as closed, counted there.
        Never raises: a failed lookup or link costs the link, not the Case, and is
        recorded on the new Case.
        """
        try:
            closed = await asyncio.to_thread(_matching_cases, finding, open=False)
        except Exception as e:
            logger.warning(
                "Failed to look up closed cases for %s", case_id, exc_info=True
            )
            await asyncio.to_thread(
                _record_failure,
                case_id,
                f"Could not look up closed cases to link: {e}",
                {"finding_id": finding.get("finding_id")},
            )
            closed = []
        closed = [m for m in closed if m.case_id != case_id][:MAX_CLOSED_LINKS]
        tried = {m.case_id for m in closed}
        for match in closed + [m for m in open_links if m.case_id not in tried]:
            try:
                note = _related_note(match, case_id)
                await asyncio.to_thread(_link_cases, case_id, match.case_id, note)
            except Exception as e:
                logger.warning(
                    "Failed to link case %s to %s",
                    case_id,
                    match.case_id,
                    exc_info=True,
                )
                await asyncio.to_thread(
                    _record_failure,
                    case_id,
                    f"Could not link to case {match.case_id}: {e}",
                    {"related_case_id": match.case_id, "matched_key": match.key},
                )

    # -------------------------------------------------------------------------
    # Notifications
    # -------------------------------------------------------------------------

    def _send_notification(
        self,
        inv_id: str,
        notification_type: str,
        title: str,
        message: str,
        priority: str = "normal",
    ):
        """Create a CaseNotification record for the investigation."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import CaseNotification, Investigation

            with get_db_manager().session_scope() as session:
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .first()
                )
                case_id = inv.case_id if inv else None
                from core.cases.case_notification_service import may_receive
                from core.storage.models import Case

                # A restricted case's notices go only to its readers.
                if case_id and not may_receive(
                    session, session.get(Case, case_id), "admin"
                ):
                    return

                notif = CaseNotification(
                    case_id=case_id,
                    user_id="admin",
                    notification_type=notification_type,
                    title=title,
                    message=message,
                    delivery_channel="ui",
                    priority=priority,
                    notification_metadata={"investigation_id": inv_id},
                )
                session.add(notif)
            logger.debug(f"Notification created for {inv_id}: {notification_type}")
        except Exception as e:
            logger.error(f"Failed to create notification for {inv_id}: {e}")

    async def _send_slack_for_notification(
        self, title: str, message: str, severity: str = "high"
    ):
        """Optionally forward urgent notifications to Slack."""
        try:
            if get_settings().daemon_slack_enabled is not True:
                return
            # resolve returns secrets even when the integration is disabled.
            if not is_integration_enabled("slack"):
                return

            config = resolve(SLACK)
            token = config.get("bot_token")
            channel = config.get("default_channel") or "#soc-alerts"
            if not token:
                return

            color_map = {"critical": "#ff0000", "high": "#ff9900", "medium": "#ffcc00"}
            payload = {
                "channel": channel,
                "attachments": [
                    {
                        "color": color_map.get(severity, "#36a64f"),
                        "title": title,
                        "text": message,
                        "footer": "AI SOC Orchestrator",
                    }
                ],
            }
            await asyncio.to_thread(
                httpx.post,
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
                follow_redirects=True,
            )
        except Exception as e:
            logger.debug(f"Slack notification failed: {e}")

    # -------------------------------------------------------------------------
    # Database Helpers
    # -------------------------------------------------------------------------

    def _save_investigation(
        self,
        inv_record: Dict,
        trigger_id: Optional[int] = None,
        mint_case: Optional[CaseSpec] = None,
        document: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Save a new investigation; with a trigger id, CAS it launched in the same transaction.

        When ``mint_case`` is given, the Case (and ``case_findings``) are written
        in this session so a crash cannot leave a Case with no run, or a run
        with no Case. A document on a Human Ask is recorded on that Case here.
        """
        try:
            from sqlalchemy import update

            from core.storage.connection import get_db_manager
            from core.storage.models import IntakeTrigger, Investigation

            with get_db_manager().session_scope() as session:
                if mint_case is not None:
                    inv_record["case_id"] = _mint_case(session, mint_case)
                _record_human_ask_document(session, inv_record.get("case_id"), document)
                inv = Investigation(
                    investigation_id=inv_record["investigation_id"],
                    case_id=inv_record.get("case_id"),
                    workflow_id=inv_record["workflow_id"],
                    trigger_type=inv_record["trigger_type"],
                    trigger_ids=inv_record.get("trigger_ids", []),
                    status=inv_record.get("status", "assigned"),
                    workdir=inv_record["workdir"],
                    current_step=inv_record.get("current_step", 0),
                    priority=inv_record.get("priority", "medium"),
                    max_iterations=inv_record.get(
                        "max_iterations", self.config.max_iterations_per_agent
                    ),
                    # Set by the caller from the live budgets; the fallbacks
                    # are the startup config.
                    max_cost_usd=inv_record.get(
                        "max_cost_usd", self.config.max_cost_per_investigation
                    ),
                    max_runtime_seconds=inv_record.get(
                        "max_runtime_seconds",
                        self.config.max_runtime_per_investigation,
                    ),
                )
                session.add(inv)
                if trigger_id is None:
                    return True
                session.flush()
                claimed = session.execute(
                    update(IntakeTrigger)
                    .where(
                        IntakeTrigger.id == trigger_id,
                        IntakeTrigger.state == "queued",
                    )
                    .values(
                        state="launched",
                        investigation_id=inv_record["investigation_id"],
                        case_id=inv_record.get("case_id"),
                        decided_at=utcnow(),
                        **({"reason": reason} if reason else {}),
                    )
                    .returning(IntakeTrigger.id)
                ).scalar_one_or_none()
                if claimed is None:
                    raise _TriggerAlreadyDecided()
            return True
        except _TriggerAlreadyDecided:
            return False
        except Exception as e:
            logger.error(f"Failed to save investigation to DB: {e}")
            return False

    def _queued_intake_triggers(self) -> List[Dict]:
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import IntakeTrigger
            from core.storage.schemas import IntakeTriggerSchema

            with get_db_manager().session_scope() as session:
                rows = (
                    session.query(IntakeTrigger)
                    .filter_by(state="queued")
                    .order_by(IntakeTrigger.created_at.asc())
                    .all()
                )
                return IntakeTriggerSchema.dump_many(rows)
        except Exception as e:
            logger.error(f"Failed to read intake queue: {e}")
            return []

    def _hydrate_detection_finding(self, row: Dict) -> Optional[Dict]:
        finding_id = row.get("finding_id")
        if not finding_id:
            return None
        finding = _load_finding(finding_id)
        return lift_ai_enrichment(finding) if finding else None

    def _decide_trigger(
        self,
        trigger_id: Optional[int],
        *,
        state: str,
        reason: Optional[str] = None,
        merged_into: Optional[str] = None,
    ) -> None:
        if trigger_id is None:
            return
        try:
            from sqlalchemy import update

            from core.storage.connection import get_db_manager
            from core.storage.models import IntakeTrigger, case_findings

            with get_db_manager().session_scope() as session:
                row = session.get(IntakeTrigger, trigger_id)
                if row is None or row.state != "queued":
                    return
                # Record a merge only while this transaction still sees the
                # alert on that Case. The removal locks the same link row, so
                # one of the two commits first (#4850).
                if state == "merged" and merged_into and row.finding_id:
                    held = session.execute(
                        select(case_findings.c.finding_id)
                        .where(
                            case_findings.c.case_id == merged_into,
                            case_findings.c.finding_id == row.finding_id,
                        )
                        .with_for_update()
                    ).first()
                    if held is None:
                        logger.info(
                            "intake row %s stays queued: finding %s is not on case %s",
                            trigger_id,
                            row.finding_id,
                            merged_into,
                        )
                        return
                session.execute(
                    update(IntakeTrigger)
                    .where(
                        IntakeTrigger.id == trigger_id,
                        IntakeTrigger.state == "queued",
                    )
                    .values(
                        state=state,
                        reason=reason,
                        merged_into=merged_into,
                        decided_at=utcnow(),
                    )
                )
        except Exception as e:
            logger.error(f"Failed to decide intake row {trigger_id}: {e}")

    def _get_investigations_by_status(self, status: str) -> List[Dict]:
        """Query investigations by status from the database."""
        from core.storage.connection import get_db_manager
        from core.storage.models import Investigation
        from core.storage.schemas import InvestigationSchema

        with get_db_manager().session_scope() as session:
            results = session.query(Investigation).filter_by(status=status).all()
            return InvestigationSchema.dump_many(results)

    def get_all_investigations(self, status: Optional[str] = None) -> List[Dict]:
        """Get all investigations, optionally filtered by status."""
        from core.storage.connection import get_db_manager
        from core.storage.models import Investigation
        from core.storage.schemas import InvestigationSchema

        with get_db_manager().session_scope() as session:
            query = session.query(Investigation)
            if status:
                query = query.filter_by(status=status)
            return InvestigationSchema.dump_many(
                query.order_by(Investigation.created_at.desc()).all()
            )

    def get_investigation(self, inv_id: str) -> Optional[Dict]:
        from core.storage.connection import get_db_manager
        from core.storage.models import Investigation
        from core.storage.schemas import InvestigationSchema

        with get_db_manager().session_scope() as session:
            inv = (
                session.query(Investigation).filter_by(investigation_id=inv_id).first()
            )
            return InvestigationSchema.dump(inv) if inv else None

    # Returns the status the row had, or None when it was not read. Activity is
    # the park reason a paused row shows; any other status clears it.
    def _update_investigation_status(
        self,
        inv_id: str,
        status: str,
        notes: Optional[str] = None,
        activity: Optional[str] = None,
    ) -> Optional[str]:
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                # Locked so two writers cannot both see the row unfinished.
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .with_for_update()
                    .first()
                )
                if inv is None:
                    return None
                was = inv.status
                inv.status = status
                inv.current_activity = activity
                if notes:
                    inv.master_review_notes = notes
                if status == "completed":
                    inv.completed_at = utcnow()
                # Counted on the transition only, so a second kill of the same
                # row (stale and budget in one tick) adds nothing.
                newly_failed = (
                    status == "failed" and was not in FINISHED_INVESTIGATION_STATUSES
                )
                cost = inv.cost_usd or 0.0
        except Exception as e:
            logger.error(f"Failed to update investigation status: {e}")
            return None
        if newly_failed:
            self.stats["investigations_failed"] += 1
            self.stats["total_cost_usd"] += cost
            if _inv_failed is not None:
                _inv_failed.add(1)
        return was

    def _claim_completion(self, inv_id: str, notes: Optional[str]) -> bool:
        """Move a row to ``completed`` in one locked step; False if already there."""
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import Investigation

            with get_db_manager().session_scope() as session:
                inv = (
                    session.query(Investigation)
                    .filter_by(investigation_id=inv_id)
                    .with_for_update()
                    .first()
                )
                if inv is None or inv.status == "completed":
                    return False
                inv.status = "completed"
                inv.current_activity = None
                inv.completed_at = utcnow()
                if notes:
                    inv.master_review_notes = notes
                cost = inv.cost_usd or 0.0
        except Exception as e:
            logger.error(f"Failed to complete investigation {inv_id}: {e}")
            return False
        self.stats["total_cost_usd"] += cost
        return True

    def get_cost_summary(self) -> Dict[str, Any]:
        """Get cost breakdown across all investigations."""
        all_inv = self.get_all_investigations()
        total = sum(i.get("cost_usd", 0) for i in all_inv)
        active_cost = sum(
            i.get("cost_usd", 0)
            for i in all_inv
            if i.get("status") in ("assigned", "executing")
        )
        spent = self._hourly_cost()
        limit = self._hourly_cost_limit()
        hourly = spent or 0.0
        return {
            "total_cost_usd": round(total, 4),
            "active_cost_usd": round(active_cost, 4),
            "hourly_cost_usd": round(hourly, 4),
            "hourly_budget_remaining": round(limit - hourly, 4),
            # /status reports `enabled` from the settings row, which a pause
            # never touches, so the pause is reported here.
            "hourly_paused": self._hourly_pause_decision(spent, limit),
            "hourly_limit_usd": round(limit, 4),
            "per_investigation_limit": self._max_cost(),
        }

    async def purge_all_investigations(self) -> Dict[str, Any]:
        """Stop all running agents, delete every investigation row, and wipe
        the on-disk workdir tree. Used by the Settings UI's "Clear All
        Investigations" button as a hard reset for the auto-investigate
        subsystem.
        """
        self._abandon_in_flight()

        deleted = 0
        try:
            from sqlalchemy import exists

            from core.storage.connection import get_db_manager, table_present
            from core.storage.models import Investigation, InvestigationLog

            # investigation_logs no longer cascades (#3309): an investigation
            # that still has old log rows stays, and so do they. Once the
            # upgrade drops the table (#3310), every investigation goes.
            logged = exists().where(
                InvestigationLog.investigation_id == Investigation.investigation_id
            )
            with get_db_manager().session_scope() as session:
                query = session.query(Investigation)
                if table_present(session, "investigation_logs"):
                    query = query.filter(~logged)
                deleted = query.delete(synchronize_session=False)
        except Exception as e:
            logger.error(f"Failed to delete investigations from DB: {e}")
            raise

        try:
            base = self.workdir.base_dir
            if base.is_dir():
                import shutil

                shutil.rmtree(base, ignore_errors=True)
            base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Workdir cleanup failed after purge: {e}")

        logger.warning(
            f"Purged {deleted} investigations and reset workdir tree at {self.workdir.base_dir}"
        )
        return {"deleted": deleted}

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    async def _sleep(self, shutdown_event: asyncio.Event, seconds: int):
        """Sleep that respects shutdown events."""
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
