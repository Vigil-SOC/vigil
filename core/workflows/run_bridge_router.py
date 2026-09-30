# The projection of a run, written by the agent layer. Phase rows are what the UI
# reads; approval decisions travel back the other way for that layer to journal.

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from core.agents.internal_auth import authorise
from core.deps import provide_approvals, provide_workflow_runs
from core.response.approval_service import ApprovalService
from core.response.checkpoints import raise_for_checkpoint, withdraw_for_run
from core.routing import Auth, RouterMeta
from core.time import utcnow
from core.workflows.workflow_run_service import WorkflowRunService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/internal/runs",
    tags=["internal-runs"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "A shared secret: the caller is the agent layer, not a session. Reachability\n"
        "is the NetworkPolicy's job since ADR 0014, not a loopback check."
    ),
)
logger = logging.getLogger(__name__)

WAITING = "pending_approval"

# What a run's status becomes when a phase reports one. A step that is waiting is
# the only one that says anything about the run as a whole.
RUN_STATUS = {WAITING: "paused", "running": "running", "completed": "running"}

# A run nobody stopped and nothing broke. Aborted and abandoned both read as
# crashes under "failed", and only one of the three is worth paging over.
# budget_exhausted is the same mistake one step along: a hunt that stopped at the
# ceiling its operator set did what it was told.
TERMINAL_STATUS = {
    "completed": "completed",
    "budget_exhausted": "completed",
    "aborted": "cancelled",
    "abandoned": "cancelled",
}


class PhaseUpdate(BaseModel):
    phase_id: str
    agent: str
    name: str
    order: int
    status: str
    approval_state: Optional[str] = None
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    checkpoint_id: Optional[str] = None
    question: Optional[str] = None


class TerminalHandoff(BaseModel):
    case_id: str
    title: str
    markdown: str = ""


class TerminalUpdate(BaseModel):
    outcome: str
    reason: str = ""
    summary: str = ""
    cost_usd: Optional[float] = None
    handoffs: List[TerminalHandoff] = Field(default_factory=list)


class CheckpointRaised(BaseModel):
    checkpoint_id: str
    checkpoint_class: str
    question: str
    raised_at: str = ""
    run_kind: str = ""
    context: Optional[Dict[str, Any]] = None


class Decision(BaseModel):
    checkpoint_id: str
    actor: str
    answer: str = Field(..., description="approve or reject")
    text: str = ""
    resolved_at: str


class Decisions(BaseModel):
    decisions: List[Decision] = []


@router.post("/{run_id}/phases", status_code=204)
def record_phase(
    run_id: str,
    update: PhaseUpdate,
    authorization: Optional[str] = Header(default=None),
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
    approvals: ApprovalService = Depends(provide_approvals),
) -> None:
    authorise(authorization, "run progress")

    now = utcnow()
    run_service.upsert_phase(
        run_id,
        update.phase_id,
        phase_order=update.order,
        agent_id=update.agent,
        status=update.status,
        output=update.output,
        error=update.error,
        # The agent layer says how a gate was answered; a waiting step is the only
        # state this side can name on its own.
        approval_state=update.approval_state
        or ("pending" if update.status == WAITING else None),
        started_at=now if update.status == "running" else None,
        finished_at=now if update.status in ("completed", "failed") else None,
    )
    run_service.set_status(run_id, RUN_STATUS.get(update.status, "running"))

    if update.status == WAITING and update.checkpoint_id:
        _raise_approval(run_id, update, approvals)


@router.post("/{run_id}/terminal", status_code=204)
def record_terminal(
    run_id: str,
    update: TerminalUpdate,
    authorization: Optional[str] = Header(default=None),
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
    approvals: ApprovalService = Depends(provide_approvals),
) -> None:
    authorise(authorization, "run outcome")

    withdraw_for_run(
        run_id,
        f"the run ended before this was answered: {update.reason or update.outcome}",
        approvals,
    )

    status = TERMINAL_STATUS.get(update.outcome, "failed")
    run_service.finalize_run(
        run_id,
        status=status,
        result_summary=update.summary or None,
        # Only a failure writes the error column, which the console renders under a
        # red heading. Every outcome still stores its reason beside the status: a
        # run stopped at its ceiling has a reason, not an error.
        error=update.reason if status == "failed" else None,
        cost_usd=update.cost_usd,
        outcome=update.outcome,
        reason=update.reason or None,
    )

    # The case the run was started from, so its report reaches the case rather than
    # living only in the run row.
    origin = _origin_case(run_id, run_service)
    if origin:
        _record_report(origin, run_id, update)

    # A threat hunt that proved a compromise tees up the backward root-cause run,
    # parked for the operator's go-ahead. Computed once: an RCA's own handoff
    # (run_kind root_cause) returns False, so a root cause never spawns another.
    # The forward hunt files each handoff the moment it lands (see the /handoff
    # route), so by the time its terminal arrives the case and RCA are usually
    # already teed up; the case is keyed on the handoff and the RCA on the trigger,
    # so re-sending them here is a safety net rather than a second of either.
    start_rca = _source_is_hunt(run_id)
    for handoff in update.handoffs:
        _process_handoff(run_id, handoff, origin, start_rca, run_service)


# A handoff pushed the moment the hunt journals it, ahead of the terminal that will
# carry it again. A forward hunt escalates and keeps hunting -- its terminal can be
# an hour of parking away, or never arrive -- so the case IR receives, and the
# root-cause run it tees up, are filed here rather than left waiting on that end.
@router.post("/{run_id}/handoff", status_code=204)
def record_handoff(
    run_id: str,
    handoff: TerminalHandoff,
    authorization: Optional[str] = Header(default=None),
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
) -> None:
    authorise(authorization, "run handoff")
    origin = _origin_case(run_id, run_service)
    _process_handoff(run_id, handoff, origin, _source_is_hunt(run_id), run_service)


# Opens the case a handoff hands over and, for a hunt, tees up the backward run.
# Both the /handoff push and the terminal that re-carries it land here, so each half
# guards itself: the case on the handoff it files (_open_case), the root-cause run on
# the handoff it traces back from (_rca_exists). One gate for both would tie the case
# to a decision that is not about it, and would open a second case for every handoff
# that tees up no root-cause run at all.
def _process_handoff(
    run_id: str,
    handoff: TerminalHandoff,
    origin: str,
    start_rca: bool,
    run_service: WorkflowRunService,
) -> None:
    opened_case = _open_case(run_id, handoff, origin)
    if start_rca and not _rca_exists(run_id, handoff, run_service):
        _start_root_cause(run_id, handoff, opened_case)


def _origin_case(run_id: str, run_service: WorkflowRunService) -> str:
    run = run_service.get_run(run_id) or {}
    case_id = (run.get("trigger_context") or {}).get("case_id")
    return str(case_id) if case_id else ""


# Appended rather than written over: a case accumulates what was done to it, and the
# description is the analyst's own. activities is the list the case UI reads.
def _add_activity(
    case_id: str, activity_type: str, description: str, details: Dict[str, Any]
) -> None:
    from core.storage.database_data_service import DatabaseDataService

    data = DatabaseDataService()
    case = data.get_case(case_id)
    if not case:
        logger.warning("case %s is gone; %s not recorded on it", case_id, activity_type)
        return

    activities = list(case.get("activities") or [])
    activities.append(
        {
            "timestamp": utcnow().isoformat() + "Z",
            "activity_type": activity_type,
            "description": description,
            "details": details,
        }
    )
    data.update_case(case_id, activities=activities)


def _record_report(case_id: str, run_id: str, update: TerminalUpdate) -> None:
    try:
        _add_activity(
            case_id,
            "agent_run_report",
            update.summary or update.reason or update.outcome,
            {"run_id": run_id, "outcome": update.outcome, "cost_usd": update.cost_usd},
        )
    except Exception:  # noqa: BLE001 — the run ended either way
        logger.exception(
            "could not record the report of %s on case %s", run_id, case_id
        )


# The Vigil case a handoff opens, named for the handoff rather than for the moment
# it arrived. Derived rather than looked up because there is nothing to look it up
# by: create_case mints its own id, so a handoff arriving twice -- pushed the moment
# the hunt journals it, then again on the terminal that re-carries it -- would open
# two cases with nothing able to tell they are the same escalation. No date in the
# key, so two arrivals either side of midnight still land on one case.
#
# 16 hex characters, not 8. A collision here does not merely reuse an id: the second
# escalation finds the first one's case already open and returns, so its case file is
# discarded with nothing said. 32 bits makes that a coin flip at seventy-odd thousand
# escalations; 64 makes it unreachable, and cases.case_id is String(50) against the
# 29 this spends, so the width is free.
def _handoff_case_id(source_run_id: str, handoff: TerminalHandoff) -> str:
    digest = hashlib.sha256(f"{source_run_id}:{handoff.case_id}".encode()).hexdigest()
    return f"case-handoff-{digest[:16]}"


def _case_exists(data: Any, case_id: str) -> bool:
    try:
        return bool(data.get_case(case_id))
    except Exception:  # noqa: BLE001 — a lookup that failed is not a case that exists
        logger.exception("could not check whether %s is already open", case_id)
        return False


# A run that ended by handing work over opens the case that receives it. The agent
# layer holds no case table, so the document travels and this side files it.
#
# Idempotent per handoff, and on its own account rather than the root-cause run's: a
# handoff that tees up no backward run still arrives twice, and the second arrival
# must find the case the first opened.
def _open_case(
    run_id: str, handoff: TerminalHandoff, origin: str = ""
) -> Optional[str]:
    from core.storage.database_data_service import DatabaseDataService

    case_id = _handoff_case_id(run_id, handoff)
    data = DatabaseDataService()
    if _case_exists(data, case_id):
        return case_id

    try:
        opened = data.create_case(
            title=handoff.title[:200],
            finding_ids=[],
            priority="high",
            description=_with_origin(handoff.markdown, origin),
            case_id=case_id,
        )
    except Exception:  # noqa: BLE001 — the run ended either way
        logger.exception("could not open %s handed off by %s", handoff.case_id, run_id)
        return None

    opened_id = (opened or {}).get("case_id", "")
    if opened_id:
        # Both directions, so neither case is a dead end. Once only: the second
        # arrival returned above, so the origin case records one escalation.
        if origin:
            _record_handoff(origin, run_id, handoff, opened_id)
        return opened_id

    # Nothing came back after the lookup said there was no such case: the concurrent
    # arrival lost on the primary key, which is the race working rather than failing.
    # The case is open either way, so it is still what the root-cause run files onto.
    return case_id if _case_exists(data, case_id) else None


def _source_is_hunt(run_id: str) -> bool:
    """True only when ``run_id``'s ledger opened as run_kind 'hunt' — the one kind
    whose handoff tees up a backward root-cause run. An RCA's own ledger opened as
    'root_cause' and returns False, so a root cause never spawns another.

    Read off the ledger rather than resolved through the run's workflow row, because
    the worker that pushes a handoff early decides to do so from this same value.
    Going through the row asks a different question and can get a different answer:
    a hunt started from file paths is filed under its loop ('hunt') rather than its
    definition ('threat-hunt'), so the definition lookup finds nothing and the run
    reads as not-a-hunt while the worker pushes its handoffs anyway.
    """
    from core.workflows.run_resume import run_kind_of
    from core.workflows.workflows_service import HUNT_RUN_KIND

    try:
        return run_kind_of(run_id) == HUNT_RUN_KIND
    except Exception:  # noqa: BLE001 — an unreadable ledger just means no RCA
        logger.exception("could not read the run kind of %s", run_id)
        return False


# The join key tying a backward root-cause run to the handoff it traces back from.
# One spelling, since both the dedup check and the enqueue key off it.
def _triggered_by(source_run_id: str, handoff: TerminalHandoff) -> str:
    return f"handoff:{source_run_id}:{handoff.case_id}"


# A run row that exists but never ran. execute_workflow persists the row before it
# enqueues, and finalises it as failed when the queue refuses -- so a row alone does
# not mean a root-cause run happened, and counting one would retire the terminal's
# retry for the one outage the retry is there for.
DEAD_RUN_STATUSES = frozenset({"failed", "cancelled"})


# Whether the backward run for this handoff was already teed up, keyed by the
# handoff it traces back from. The RCA's own gate and nothing else's: the case a
# handoff opens is keyed on the handoff (_handoff_case_id), so a second arrival is
# already a no-op there without asking this. Fail-open: a missed dedup is a second
# root-cause run, better than none at all.
def _rca_exists(
    source_run_id: str, handoff: TerminalHandoff, run_service: WorkflowRunService
) -> bool:
    try:
        run = run_service.find_run_by_trigger(_triggered_by(source_run_id, handoff))
    except Exception:  # noqa: BLE001 — a missed dedup is better than no RCA
        logger.exception("could not check for an existing root-cause run")
        return False
    if not run:
        return False
    return run.get("status") not in DEAD_RUN_STATUSES


# A proven hunt hands off; the RCA that traces how it started is teed up here rather
# than left for someone to remember. It parks at its hypothesis_approval checkpoint
# (root-cause-analysis declares it "ask"), so it waits in the same approvals inbox a
# hunt uses for the operator to go ahead. The backward hypothesis is derived from the
# handoff finding, which already carries the confirmed claim — not the later report.
# _process_handoff has already guarded on _rca_exists, so this is the sole tee-up.
def _start_root_cause(
    source_run_id: str,
    handoff: TerminalHandoff,
    opened_case: Optional[str],
) -> None:
    # Only what the run actually reads. An "agent_id" sat here naming a worker, which
    # nothing consumed: the roster and its prompts are rootcause.yaml's, and the lead
    # dispatches whichever of them a question needs. "source_run_id" was the same --
    # unread, and already spelled in the triggered_by this is enqueued under.
    params = {
        "hypothesis": _rca_hypothesis(handoff),
        "context": _rca_context(handoff),
        # Files the RCA's report back onto the IR case the hunt opened, and lets it
        # read that case's finding as target context.
        "case_id": opened_case or handoff.case_id,
    }
    try:
        result = asyncio.run(
            _enqueue_root_cause(params, _triggered_by(source_run_id, handoff))
        )
    except Exception:  # noqa: BLE001 — the case it opened is the deliverable
        logger.exception(
            "could not tee up a root-cause run for handoff %s of %s",
            handoff.case_id,
            source_run_id,
        )
        return
    # Read rather than assumed: execute_workflow refuses by returning, not by
    # raising, so nothing above catches a queue outage or a refused definition. The
    # row it left behind is finalised as failed, which _rca_exists does not count,
    # so the terminal that re-carries this handoff will try again.
    if not result.get("success"):
        logger.error(
            "root-cause run for handoff %s of %s was not queued: %s",
            handoff.case_id,
            source_run_id,
            result.get("error"),
        )


async def _enqueue_root_cause(
    params: Dict[str, Any], triggered_by: str
) -> Dict[str, Any]:
    from core.agents.queue import close_run_queue
    from core.workflows.workflows_service import WorkflowsService

    try:
        return await WorkflowsService().execute_workflow(
            "root-cause-analysis",
            params,
            triggered_by=triggered_by,
            # The join key is not a person. Without this the run event and the
            # approval an operator is asked to answer both name the escalation's
            # hash where every other run names api, watchdog or a username.
            actor="handoff",
        )
    finally:
        # asyncio.run closes the loop this ran on, and _run_queue caches one Queue
        # per loop. Without this the Queue's Redis connection is dropped unclosed,
        # once per handoff.
        await close_run_queue()


# Unnamed on purpose. The hypothesis reads better naming the host, but nothing here
# knows which one it is: the handoff carries a title and a rendered case file, and
# the case file inlines every linked record's payload as JSON, so any pattern run
# over it is as likely to name SHA-256, CVE-2024 or US-EAST as a host -- and the
# subject is the first clause of the claim the whole backward run argues from. The
# agent layer has no host to state either: entity extraction carries no host pattern
# and no definition declares a scope entity, so its own focus is an ip or a domain,
# which for a confirmed C2 is as often the attacker's address as the victim's.
#
# Nothing is lost by leaving it generic. _rca_context hands the run the confirmed
# finding verbatim, so the host is in front of the model on turn 0 either way. A
# hypothesis that names the wrong machine is the one thing that could not be
# recovered from.
def _rca_hypothesis(handoff: TerminalHandoff) -> str:
    return (
        "the confirmed-compromised host was compromised via an initial-access vector "
        f"that led to the confirmed threat escalated as {handoff.case_id}; establish "
        "how the attacker first got onto this host — the initial-access vector and "
        "patient zero."
    )


# What a confirmed finding is worth carrying into the backward run's brief. The
# handoff's markdown is the whole rendered case file, and renderCaseFile inlines
# every linked record's payload as pretty-printed JSON -- unbounded by anything on
# either side. It goes into trigger_context as jsonb and verbatim into the run's
# target context, so with no ceiling one escalation's evidence trail can be the
# whole of what the lead reads on turn 0. The head is the part that carries the
# claim and its verdict; the trail below it is what the run is about to re-derive
# for itself anyway.
_RCA_FINDING_CHARS = 8000


def _rca_finding(handoff: TerminalHandoff) -> str:
    finding = handoff.markdown.strip() or handoff.title
    if len(finding) <= _RCA_FINDING_CHARS:
        return finding
    # No id named: handoff.case_id is the agent's own, not the Vigil case anyone
    # could open. The IR case this run reports onto holds the file in full.
    return (
        f"{finding[:_RCA_FINDING_CHARS].rstrip()}\n\n"
        f"[truncated here: {len(finding) - _RCA_FINDING_CHARS} more characters of the "
        "case file, which the IR case this run reports onto carries in full]"
    )


def _rca_context(handoff: TerminalHandoff) -> str:
    finding = _rca_finding(handoff)
    return (
        "This run follows a CONFIRMED compromise handed to incident response. The "
        "confirmed finding to work backward from:\n\n"
        f"{finding}\n\n"
        "Work BACKWARD to the initial-access vector: find the earliest malicious "
        "activity that PRECEDES the confirmed compromise, what was delivered to the "
        "user and how, and when. Report the initial-access vector confirmed / "
        "refuted / inconclusive with the specific artifact, delivery method, and "
        "timestamp."
    )


def _with_origin(markdown: str, origin: str) -> str:
    return f"{markdown}\n\n_Escalated from case {origin}._\n" if origin else markdown


def _record_handoff(
    case_id: str, run_id: str, handoff: TerminalHandoff, opened: str
) -> None:
    try:
        _add_activity(
            case_id,
            "agent_run_handoff",
            f"Escalated to incident response as "
            f"{opened or handoff.case_id}: {handoff.title}",
            {"run_id": run_id, "case_id": opened, "handoff_id": handoff.case_id},
        )
    except Exception:  # noqa: BLE001 — the case it opened is the deliverable
        logger.exception("could not link %s back to case %s", opened, case_id)


# A run parked on a checkpoint, as a question in the approvals inbox. Only the
# compose path raised these before, so a hunt parked where nobody could see it.
@router.post("/{run_id}/checkpoints", status_code=204)
def record_checkpoint(
    run_id: str,
    raised: CheckpointRaised,
    authorization: Optional[str] = Header(default=None),
    approvals: ApprovalService = Depends(provide_approvals),
) -> None:
    authorise(authorization, "run checkpoint")

    # Idempotent per checkpoint inside raise_for_checkpoint, because a parked run
    # is announced on every sweep and would otherwise queue the question each time.
    raise_for_checkpoint(
        run_id=run_id,
        checkpoint_id=raised.checkpoint_id,
        title=raised.question[:120] or raised.checkpoint_class,
        description=raised.question,
        reason=f"The run parked on a {raised.checkpoint_class} checkpoint",
        parameters={
            "checkpoint_class": raised.checkpoint_class,
            "run_kind": raised.run_kind,
            **(raised.context or {}),
        },
        approvals=approvals,
    )


# The approvals table is the inbox. An analyst answers there, the agent layer
# reads the answer here, and that layer alone writes it onto the ledger.
@router.get("/{run_id}/decisions", response_model=Decisions)
def list_decisions(
    run_id: str,
    authorization: Optional[str] = Header(default=None),
    approvals: ApprovalService = Depends(provide_approvals),
) -> Decisions:
    from core.response.approval_service import ActionStatus

    authorise(authorization, "run decisions")

    decided: List[Decision] = []
    for status, answer in (
        (ActionStatus.APPROVED, "approve"),
        (ActionStatus.REJECTED, "reject"),
    ):
        for action in approvals.list_actions(status=status, workflow_run_id=run_id):
            checkpoint = (action.parameters or {}).get("checkpoint_id")
            if not checkpoint:
                continue
            decided.append(
                Decision(
                    checkpoint_id=str(checkpoint),
                    actor=action.approved_by or "analyst",
                    answer=answer,
                    text=action.rejection_reason or "",
                    resolved_at=action.approved_at or utcnow().isoformat(),
                )
            )
    return Decisions(decisions=decided)


# Named by the checkpoint the agent layer raised, so the decision travels back
# addressed to the step that is waiting and to no other.
def _raise_approval(
    run_id: str, update: PhaseUpdate, approvals: ApprovalService
) -> None:
    raise_for_checkpoint(
        run_id=run_id,
        checkpoint_id=str(update.checkpoint_id),
        title=update.question or f"Approve {update.name}",
        description=update.question or f"Phase {update.order}: {update.name}",
        reason="The playbook marks this phase approval_required",
        parameters={"phase_id": update.phase_id, "agent_id": update.agent},
        phase_id=update.phase_id,
        approvals=approvals,
    )
