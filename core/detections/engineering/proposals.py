"""A proposal's steps: draft, lint, replay, tune, convert, shadow, live, each one an event.

The state machine is ``TRANSITIONS`` on the model, and ``move_to`` is its one writer. A step that
ran but did not move the state (a lint that failed, a target that would not convert, an edit of a
draft) appends an event with ``from_state`` equal to ``to_state``, so the record of what was
tried stays beside the record of what passed. Every event's evidence names its ``step``.

Tools run only when someone asks. Nothing here writes to a SIEM.

Ported from 1.x's core/detections/engineering/proposals.py (#4157). On main:

- replay reads each SIEM Vigil reads (siem_replay), not log_events, which main lacks;
  the SIEM reads run outside the proposal's row lock, and the result is kept only when the
  rule's text is still the one replayed;
- convert attaches the native copies a person pastes (Splunk YAML, Elastic TOML, Wazuh XML),
  parsed and linted as the overlay will save them, in place of pySigma conversion;
- each step names who took it: a signed-in person, an agent run, or Vigil itself;
- shadow and live come in a later change (#4157 step 4), and drafting with the model is
  not here;
- the daily gaps run opens one proposal per gap a rule can answer, recording the gap and
  its evidence, and the copies its agent run saves are attached to it (``for_gaps``,
  ``attach_saved``). Those copies keep the automatic rollout every agent draft has: the
  proposal records them, it does not own them. While one of those runs is still working,
  or a rule for the technique is in shadow, the gaps run holds the technique back
  (``worked_techniques``).
- a hunt's run reads the proposals the gaps run raised from it, and why each proven
  hypothesis that got none did not (``for_hunt``); a rule reads the proposals it answers
  (``for_rule``). Both are worked out on read from ``origin.gap.runs``, the attach events
  and the hunt's distil marker; nothing is stored.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from core.audit import trail
from core.auth.permissions import case_read_clause, may_read_case
from core.detections import gap_run, gaps
from core.detections.engineering import lint as lint_mod
from core.detections.engineering import siem_replay, sigma_text
from core.detections.inventory.readers import OVERLAY_SOURCE
from core.detections.overlay import Refused
from core.storage.models import (
    Case,
    DetectionProposal,
    DetectionProposalEvent,
    DetectionRule,
    DetectionRuleStage,
    DetectionRuleVersion,
    EpisodicDistilMarker,
    EpisodicVerdict,
    IntakeTrigger,
    Investigation,
    WorkflowRun,
)
from core.storage.models.detection_proposal import (
    ACTOR_KINDS,
    TRANSITIONS,
    InvalidProposalMove,
    ProposalState,
)
from core.storage.models.workflow import (
    LIVE_INVESTIGATION_STATUSES,
    PARKED_INVESTIGATION_STATUSES,
)
from core.storage.unit_of_work import unit_of_work
from core.workflows.enablement import is_enabled
from core.workflows.routing import DETECTION_GAPS_WORKFLOW
from core.workflows.workflow_run_service import run_hidden

logger = logging.getLogger(__name__)

PERSON, AGENT, SYSTEM = "person", "agent", "system"
# The SIEMs a person can paste a native copy for, as the overlay saves them.
COPY_LANGUAGES = ("splunk", "elastic", "wazuh")

# A proposal the daily gaps run opened. Its rollout stays automatic: the copies its run
# saves go to shadow and promote on the exit criteria like any agent draft's, so a
# proposal of this origin never owns its copies' rollout.
GAPS_RUN = "gaps run"
AUTOMATIC_ROLLOUT = "automatic"
# The gaps a rule can answer. A blind spot is a missing data source: no rule is drafted.
_DRAFTABLE_GAPS = (gap_run.MISSED, gap_run.PAPER, gap_run.INTEL)


class NotFound(LookupError):
    pass


def _normal_technique(technique: Optional[str]) -> Optional[str]:
    value = (technique or "").strip().upper()
    if not value:
        return None
    if not sigma_text.TECHNIQUE_ID.match(value):
        raise Refused(f"{technique!r} is not an ATT&CK technique id such as T1059.001.")
    return value


def _hidden():
    """A proposal drawn from a restricted case the caller can't read (#4359): not
    found, by id or in the list, as its case is."""
    return exists().where(
        Case.case_id == DetectionProposal.case_id, ~case_read_clause()
    )


def _readable(session: Session, proposal_id: str, *, lock: bool = False):
    statement = select(DetectionProposal).where(
        DetectionProposal.proposal_id == proposal_id, ~_hidden()
    )
    proposal = session.scalar(statement.with_for_update() if lock else statement)
    if proposal is None:
        raise NotFound(f"No proposal {proposal_id}.")
    return proposal


def _locked(session: Session, proposal_id: str) -> DetectionProposal:
    # Two people can race the same move.
    return _readable(session, proposal_id, lock=True)


def _note(
    proposal: DetectionProposal,
    actor: str,
    kind: str,
    evidence: Dict[str, Any],
    reason: str = "",
    sigma: Optional[str] = None,
) -> None:
    """A step that left the state where it was."""
    if sigma is not None:
        proposal.sigma = sigma
    proposal.record(actor=actor, actor_kind=kind, evidence=evidence, reason=reason)


def _kind(kind: str) -> str:
    if kind not in ACTOR_KINDS:
        raise Refused(
            f"A step is taken by one of {', '.join(ACTOR_KINDS)}, not {kind!r}."
        )
    return kind


def _events(
    session: Session, proposal: DetectionProposal
) -> List[DetectionProposalEvent]:
    session.flush()
    return list(session.scalars(proposal.events.select()))


def _latest_step(
    events: List[DetectionProposalEvent], step: str
) -> Optional[DetectionProposalEvent]:
    for event in reversed(events):
        if event.evidence.get("step") == step:
            return event
    return None


def _event_view(event: DetectionProposalEvent, sigma: str) -> Dict[str, Any]:
    return {
        "id": event.id,
        "from_state": event.from_state.value if event.from_state else None,
        "to_state": event.to_state.value,
        "actor": event.actor,
        "actor_kind": event.actor_kind,
        "reason": event.reason,
        "at": event.at.isoformat(),
        "evidence": event.evidence,
        "sigma": event.sigma,
        "judges_current_text": event.sigma == sigma,
    }


def _stage_view(session: Session, rule_ids: List[int]) -> List[Dict[str, Any]]:
    rows = session.scalars(
        select(DetectionRuleStage)
        .where(DetectionRuleStage.rule_id.in_(rule_ids))
        .order_by(DetectionRuleStage.id)
    )
    return [
        {
            "rule_id": row.rule_id,
            "version": row.version,
            "stage": row.stage,
            "actor": row.actor,
            "reason": row.reason,
            "at": row.at.isoformat(),
        }
        for row in rows
    ]


def _links(
    proposal: DetectionProposal, evidence: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """The run a proposal was handed to and the rules attached to it, from its record."""
    run_id: Optional[str] = None
    rule_ids: List[int] = []
    for item in evidence:
        if item.get("step") not in ("run", "attach"):
            continue
        run_id = item.get("run_id") or run_id
        rule_ids += [r for r in item.get("rule_ids") or () if r not in rule_ids]
    if proposal.rule_id is not None and proposal.rule_id not in rule_ids:
        rule_ids.append(proposal.rule_id)
    return {"run_id": run_id, "rule_ids": rule_ids}


def _run_statuses(session: Session, run_ids: Iterable[Optional[str]]) -> Dict[str, str]:
    """Each run's status (running, paused, completed, failed or cancelled), so a
    proposal handed to a run that ended without a rule reads as ended."""
    wanted = {r for r in run_ids if r}
    if not wanted:
        return {}
    return dict(
        session.execute(
            select(WorkflowRun.run_id, WorkflowRun.status).where(
                WorkflowRun.run_id.in_(wanted)
            )
        ).all()
    )


def _link_steps(session: Session, ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    rows = session.execute(
        select(DetectionProposalEvent.proposal_id, DetectionProposalEvent.evidence)
        .where(
            DetectionProposalEvent.proposal_id.in_(ids),
            DetectionProposalEvent.evidence["step"].astext.in_(("run", "attach")),
        )
        .order_by(DetectionProposalEvent.id)
    ).all()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for proposal_id, evidence in rows:
        out.setdefault(proposal_id, []).append(evidence)
    return out


def _gap_case_ids(
    origins: Iterable[Optional[Dict[str, Any]]], kind: str = "case"
) -> Set[str]:
    """The ids of a gaps run's evidence runs of one kind ("case" or "hunt")."""
    return {
        key.partition(":")[2]
        for origin in origins
        for key in ((origin or {}).get("gap") or {}).get("runs") or ()
        if key.startswith(f"{kind}:")
    }


def _readable_cases(session: Session, case_ids: Set[str]) -> Set[str]:
    if not case_ids:
        return set()
    return set(
        session.scalars(
            select(Case.case_id).where(Case.case_id.in_(case_ids), case_read_clause())
        )
    )


def _origin_shown(origin: Dict[str, Any], readable: Set[str]) -> Dict[str, Any]:
    """A gaps run's evidence without the Cases this reader can't read."""
    gap = (origin or {}).get("gap")
    if not isinstance(gap, dict):
        return origin
    runs = list(gap.get("runs") or ())
    kept = [
        k for k in runs if not k.startswith("case:") or k.partition(":")[2] in readable
    ]
    if len(kept) == len(runs):
        return origin
    withheld = len(runs) - len(kept)
    return {**origin, "gap": {**gap, "runs": kept, "runs_withheld": withheld}}


def _without_gap_cases(value: Any, case_ids: Iterable[str]) -> Any:
    """A replay's ``attack_basis`` names the gap's runs: not the Cases a reader can't read."""
    for case_id in case_ids:
        value = _without_case(value, f"case:{case_id}")
    return value


def _view(session: Session, proposal: DetectionProposal) -> Dict[str, Any]:
    events = _events(session, proposal)
    links = _links(proposal, (e.evidence for e in events))
    gap_cases = _gap_case_ids([proposal.origin])
    readable = _readable_cases(session, gap_cases)
    hidden = gap_cases - readable
    latest: Dict[str, Any] = {}
    for step in (
        "draft",
        "lint",
        "replay",
        "tune",
        "convert",
        "shadow",
        "live",
        "reject",
    ):
        event = _latest_step(events, step)
        latest[step] = (
            _without_gap_cases(_event_view(event, proposal.sigma), hidden)
            if event
            else None
        )
    return {
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "technique": proposal.technique,
        "case_id": proposal.case_id,
        "origin": _origin_shown(proposal.origin, readable),
        "sigma": proposal.sigma,
        "copies": proposal.copies,
        "state": proposal.state.value,
        "rule_id": proposal.rule_id,
        **links,
        "run_status": _run_statuses(session, [links["run_id"]]).get(links["run_id"]),
        "run_working": proposal.proposal_id in _unfinished_gap_runs(session),
        "created_by": proposal.created_by,
        "created_at": proposal.created_at.isoformat(),
        "updated_at": proposal.updated_at.isoformat(),
        "allowed": sorted(s.value for s in TRANSITIONS[proposal.state]),
        "latest": latest,
        "events": _without_gap_cases(
            [_event_view(e, proposal.sigma) for e in events], hidden
        ),
        "stages": (
            _stage_view(session, links["rule_ids"]) if links["rule_ids"] else []
        ),
        # Each "hunt:<id>" in its evidence, by hunt id: its run, or withheld.
        "hunts": _hunt_runs(session, _gap_case_ids([proposal.origin], "hunt")),
    }


def get(proposal_id: str) -> Dict[str, Any]:
    with unit_of_work() as session:
        return _view(session, _readable(session, proposal_id))


def listing(state: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first, each with its latest replay's counts, oldest detail left to ``get``."""
    with unit_of_work() as session:
        statement = (
            select(DetectionProposal)
            .where(~_hidden())
            .order_by(DetectionProposal.created_at.desc())
        )
        if state:
            statement = statement.where(DetectionProposal.state == ProposalState(state))
        proposals = session.scalars(statement).all()
        ids = [p.proposal_id for p in proposals]
        replays = _latest_replays(session, ids)
        steps = _link_steps(session, ids)
        readable = _readable_cases(session, _gap_case_ids(p.origin for p in proposals))
        links = {
            p.proposal_id: _links(p, steps.get(p.proposal_id, ())) for p in proposals
        }
        statuses = _run_statuses(session, (link["run_id"] for link in links.values()))
        unfinished = _unfinished_gap_runs(session)
        return [
            {
                "proposal_id": p.proposal_id,
                "title": p.title,
                "technique": p.technique,
                "case_id": p.case_id,
                "state": p.state.value,
                "origin": _origin_shown(p.origin, readable),
                "created_by": p.created_by,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
                "replay": replays.get(p.proposal_id),
                # The copy its stages follow: a SIEM's, else the Sigma form.
                "rule_id": p.rule_id,
                **links[p.proposal_id],
                "run_status": statuses.get(links[p.proposal_id]["run_id"]),
                # Still working as the gaps run counts it, which can outlast run_status:
                # the investigation the run's intake row launched settles later.
                "run_working": p.proposal_id in unfinished,
            }
            for p in proposals
        ]


def replay_proves(evidence: Dict[str, Any]) -> bool:
    """siem_replay proves a rule only on whole reads; anything else proved nothing."""
    return bool(evidence.get("evaluated")) and bool(evidence.get("proven"))


_SAMPLE_KEYS = frozenset({"hit_samples", "false_positive_samples", "samples"})


def _without_finding_content(value: Any) -> Any:
    if isinstance(value, dict):
        kept: Dict[str, Any] = {}
        for key, item in value.items():
            if key in _SAMPLE_KEYS:
                kept[key] = []
                kept["samples_withheld"] = True
            elif key == "finding_id":
                kept[key] = None
            else:
                kept[key] = _without_finding_content(item)
        return kept
    if isinstance(value, list):
        return [_without_finding_content(item) for item in value]
    return value


def _without_case(value: Any, case_id: str) -> Any:
    if isinstance(value, dict):
        return {k: _without_case(item, case_id) for k, item in value.items()}
    if isinstance(value, list):
        return [_without_case(item, case_id) for item in value]
    return value.replace(case_id, "a case") if isinstance(value, str) else value


def shown_to(
    view: Dict[str, Any], *, reads_findings: bool, reads_cases: bool
) -> Dict[str, Any]:
    """A proposal as this reader may see it. Rows of logs and findings need findings.read, and
    which case a rule came from needs cases.read."""
    case_id = view.get("case_id")
    view = _gap_shown_to(view, reads_findings=reads_findings, reads_cases=reads_cases)
    if not reads_findings:
        view = _without_finding_content(view)
    if case_id and not reads_cases:
        view = {**_without_case(view, case_id), "case_id": None}
        if isinstance(view.get("origin"), dict):
            view["origin"] = {**view["origin"], "case_id": None}
    return view


def _gap_shown_to(
    view: Dict[str, Any], *, reads_findings: bool, reads_cases: bool
) -> Dict[str, Any]:
    """A gaps run's alerts are Findings, and its ``case:`` runs are Cases."""
    origin = view.get("origin")
    gap = origin.get("gap") if isinstance(origin, dict) else None
    if not isinstance(gap, dict):
        return view
    gap = dict(gap)
    if not reads_findings and gap.get("alerts"):
        gap["alerts"], gap["alerts_withheld"] = [], True
    if not reads_cases:
        runs = list(gap.get("runs") or ())
        kept = [k for k in runs if not k.startswith("case:")]
        if len(kept) != len(runs):
            gap["runs"] = kept
            gap["runs_withheld"] = gap.get("runs_withheld", 0) + len(runs) - len(kept)
            hidden = _gap_case_ids([origin])
            view = {
                **view,
                "events": _without_gap_cases(view.get("events"), hidden),
                "latest": _without_gap_cases(view.get("latest"), hidden),
            }
    return {**view, "origin": {**origin, "gap": gap}}


def case_of(proposal_id: str) -> Optional[str]:
    with unit_of_work() as session:
        return _readable(session, proposal_id).case_id


def _latest_replays(session: Session, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    rows = session.execute(
        select(DetectionProposalEvent.proposal_id, DetectionProposalEvent.evidence)
        .where(
            DetectionProposalEvent.proposal_id.in_(ids),
            DetectionProposalEvent.evidence["step"].astext == "replay",
        )
        .order_by(DetectionProposalEvent.id)
    ).all()
    out: Dict[str, Dict[str, Any]] = {}
    for proposal_id, evidence in rows:
        out[proposal_id] = {
            key: evidence.get(key)
            for key in (
                "evaluated",
                "hits",
                "attack_rows",
                "false_positives",
                "benign_rows",
                "benign_holding_fields",
                "proven",
                "unproven_because",
            )
        }
    return out


def _case_or_refuse(session: Session, case_id: Optional[str]) -> Optional[Case]:
    if not case_id:
        return None
    case = session.get(Case, case_id)
    # One the caller can't read answers as no case at all (#4359).
    if case is None or not may_read_case(case):
        raise Refused(f"No case {case_id}.")
    return case


def create(
    *,
    sigma: str,
    actor: str,
    kind: str = PERSON,
    technique: Optional[str] = None,
    case_id: Optional[str] = None,
    title: Optional[str] = None,
    origin: Optional[Dict[str, Any]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    rule_id: Optional[int] = None,
) -> Dict[str, Any]:
    """A draft from text a person pasted, or an agent or the inventory supplied."""
    if not sigma.strip():
        raise Refused("A proposal needs Sigma text.")
    technique = _normal_technique(technique)
    text, fixes = sigma_text.fill_in(sigma, technique)
    doc = sigma_text.parse(text) or {}
    tagged = sigma_text.techniques(doc)
    source = "pasted by a person" if kind == PERSON else f"saved by an {kind}"
    with unit_of_work() as session:
        _case_or_refuse(session, case_id)
        proposal = DetectionProposal.draft(
            title=(title or str(doc.get("title") or "")).strip() or "Untitled rule",
            sigma=text,
            actor=actor,
            actor_kind=_kind(kind),
            evidence={
                "step": "draft",
                "source": source,
                **(evidence or {}),
                "filled_in": fixes,
            },
            technique=technique or (tagged[0] if tagged else None),
            case_id=case_id,
            origin={"source": source, **(origin or {})},
        )
        proposal.rule_id = rule_id
        session.add(proposal)
        return _view(session, proposal)


def edit(
    proposal_id: str, sigma: str, actor: str, reason: str = "", kind: str = PERSON
) -> Dict[str, Any]:
    """New text from a person. Back to draft: the checks it passed judged the old text."""
    if not sigma.strip():
        raise Refused("A proposal needs Sigma text.")
    with unit_of_work() as session:
        proposal = _locked(session, proposal_id)
        _to_draft(proposal, sigma, actor, _kind(kind), {"step": "edit"}, reason)
        return _view(session, proposal)


def _to_draft(
    proposal: DetectionProposal,
    sigma: str,
    actor: str,
    kind: str,
    evidence: Dict[str, Any],
    reason: str,
) -> None:
    # The copies it held were checked against the old text: convert again.
    proposal.copies = {}
    if proposal.state is ProposalState.DRAFT:
        _note(proposal, actor, kind, evidence, reason, sigma=sigma)
    else:
        proposal.move_to(
            ProposalState.DRAFT,
            actor=actor,
            actor_kind=kind,
            evidence=evidence,
            reason=reason,
            sigma=sigma,
        )


def _lint_step(proposal: DetectionProposal, actor: str, kind: str) -> bool:
    if proposal.state is not ProposalState.DRAFT:
        raise InvalidProposalMove(proposal.state, ProposalState.LINTED)
    report = lint_mod.lint_rule(proposal.sigma)
    evidence = {
        "step": "lint",
        "tool": "core.detections.engineering.lint.lint_rule",
        **report,
    }
    if report["passed"]:
        proposal.move_to(
            ProposalState.LINTED, actor=actor, actor_kind=kind, evidence=evidence
        )
    else:
        _note(proposal, actor, kind, evidence)
    return report["passed"]


def lint(proposal_id: str, actor: str, kind: str = PERSON) -> Dict[str, Any]:
    with unit_of_work() as session:
        proposal = _locked(session, proposal_id)
        _lint_step(proposal, actor, _kind(kind))
        return _view(session, proposal)


def _replay_target(proposal_id: str) -> Dict[str, Any]:
    """What a replay reads: the text, its Case (else its gap's runs), and the events
    named last time."""
    with unit_of_work() as session:
        proposal = _readable(session, proposal_id)
        if proposal.state not in (ProposalState.LINTED, ProposalState.REPLAYED):
            raise InvalidProposalMove(proposal.state, ProposalState.REPLAYED)
        previous = _latest_step(_events(session, proposal), "replay")
        return {
            "sigma": proposal.sigma,
            "case_id": proposal.case_id,
            # A gaps-run proposal has no Case: the runs its missed gap names.
            "runs": (
                []
                if proposal.case_id
                else list(((proposal.origin or {}).get("gap") or {}).get("runs") or ())
            ),
            "named": previous.evidence.get("named") if previous else None,
            "days": (previous.evidence.get("days") if previous else None)
            or siem_replay.WINDOW_DAYS,
        }


def _record_replay(
    proposal_id: str,
    sigma: str,
    actor: str,
    kind: str,
    report: Dict[str, Any],
    evidence_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Store a replay on the proposal, if its text is still the text replayed."""
    with unit_of_work() as session:
        proposal = _locked(session, proposal_id)
        if proposal.sigma != sigma:
            raise Refused(
                "The rule changed while it was being replayed. Replay the new text."
            )
        evidence = {**report, **(evidence_extra or {})}
        if report.get("evaluated") and proposal.state is ProposalState.LINTED:
            proposal.move_to(
                ProposalState.REPLAYED, actor=actor, actor_kind=kind, evidence=evidence
            )
        elif proposal.state in (ProposalState.LINTED, ProposalState.REPLAYED):
            _note(proposal, actor, kind, evidence)
        else:
            raise InvalidProposalMove(proposal.state, ProposalState.REPLAYED)
        return _view(session, proposal)


async def replay(
    proposal_id: str,
    actor: str,
    *,
    kind: str = PERSON,
    named: Optional[Mapping[str, Sequence[str]]] = None,
    days: Optional[int] = None,
) -> Dict[str, Any]:
    """Replay the rule on the SIEMs over the window. The reads run outside the row lock;
    a proposal edited meanwhile keeps its new text and the stale replay is refused."""
    kind = _kind(kind)
    target = _replay_target(proposal_id)
    named = named or target["named"] or None
    days = days or target["days"]
    report = await siem_replay.run(
        target["sigma"],
        case_id=target["case_id"],
        runs=target["runs"],
        named=named,
        days=days,
    )
    return _record_replay(
        proposal_id,
        target["sigma"],
        actor,
        kind,
        report,
        {"named": dict(named) if named else None, "days": days},
    )


async def tune(
    proposal_id: str,
    exclusion: sigma_text.Exclusion,
    actor: str,
    reason: str,
    kind: str = PERSON,
) -> Dict[str, Any]:
    """A person's exclusion, then lint and replay again with the same attack events."""
    if not reason.strip():
        raise Refused("Say why this exclusion is safe: it stays on the record.")
    kind = _kind(kind)
    with unit_of_work() as session:
        proposal = _locked(session, proposal_id)
        if proposal.state in (ProposalState.LIVE, ProposalState.REJECTED):
            raise InvalidProposalMove(proposal.state, ProposalState.DRAFT)
        events = _events(session, proposal)
        before = _latest_step(events, "replay")
        try:
            text = sigma_text.add_exclusion(proposal.sigma, exclusion)
        except sigma_text.NotARule as exc:
            raise Refused(str(exc)) from exc
        _to_draft(
            proposal,
            text,
            actor,
            kind,
            {
                "step": "tune",
                "exclusion": {
                    "field": exclusion.field,
                    "match": exclusion.match,
                    "values": list(exclusion.values),
                },
                "before": _counts(before.evidence) if before else None,
            },
            reason,
        )
        named = before.evidence.get("named") if before else None
        days = (before.evidence.get("days") if before else None) or None
        passed = _lint_step(proposal, actor, kind)
    if not passed:
        return get(proposal_id)
    return await replay(proposal_id, actor, kind=kind, named=named, days=days)


def _counts(evidence: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "hits",
        "attack_rows",
        "false_positives",
        "benign_rows",
        "benign_holding_fields",
        "proven",
    )
    return {key: evidence.get(key) for key in keys}


def _current_replay(
    events: List[DetectionProposalEvent], sigma: str
) -> Optional[Dict[str, Any]]:
    event = _latest_step(events, "replay")
    if event is None or event.sigma != sigma or not event.evidence.get("evaluated"):
        return None
    return event.evidence


def _copy_row(language: str, text: str, actor: str) -> Dict[str, Any]:
    """One pasted copy as the overlay will read and lint it, without writing a file."""
    from core.detections import overlay

    row: Dict[str, Any] = {"target": language, "pasted_by": actor}
    try:
        ((_, prepared, record),) = overlay._prepare({language: text})
    except Refused as exc:
        return {**row, "status": "refused", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- whatever the reader rejects
        return {**row, "status": "refused", "reason": f"{language} copy: {exc}"}
    return {
        **row,
        "status": "pasted",
        "text": prepared,
        "native_key": record.native_key,
        "name": record.name,
    }


def convert(
    proposal_id: str,
    actor: str,
    *,
    pasted: Optional[Dict[str, str]] = None,
    accept_reason: str = "",
    kind: str = PERSON,
) -> Dict[str, Any]:
    """One row per pasted copy, refusals included. Moves when at least one copy reads.

    A replay that did not prove the rule takes a person's written reason, and the heat map
    still counts the rule unproven.
    """
    kind = _kind(kind)
    with unit_of_work() as session:
        proposal = _locked(session, proposal_id)
        if proposal.state not in (ProposalState.REPLAYED, ProposalState.CONVERTED):
            raise InvalidProposalMove(proposal.state, ProposalState.CONVERTED)
        replayed = _current_replay(_events(session, proposal), proposal.sigma)
        if replayed is None:
            raise Refused("Replay this text before converting it.")
        if not replay_proves(replayed) and not accept_reason.strip():
            raise Refused(
                "The replay did not prove this rule: "
                f"{replayed.get('unproven_because')} Tune it, or give a reason to go on.",
                replay=_counts(replayed),
            )
        rows = []
        for language, query in (pasted or {}).items():
            if language not in COPY_LANGUAGES:
                raise Refused(f"A {language!r} copy cannot be pasted here.")
            if query.strip():
                rows.append(_copy_row(language, query, actor))
        if not rows:
            raise Refused("Paste a Splunk, Elastic or Wazuh copy of the rule.")
        usable = {r["target"]: r["text"] for r in rows if r["status"] == "pasted"}
        evidence: Dict[str, Any] = {
            "step": "convert",
            "tool": "core.detections.overlay._prepare",
            "targets": [{k: v for k, v in r.items() if k != "text"} for r in rows],
            "siem_written": False,
        }
        if accept_reason.strip():
            evidence["accepted_unproven"] = {
                "by": actor,
                "reason": accept_reason.strip(),
                "replay": _counts(replayed),
            }
        if usable:
            proposal.copies = {**proposal.copies, **usable}
        if usable and proposal.state is ProposalState.REPLAYED:
            proposal.move_to(
                ProposalState.CONVERTED, actor=actor, actor_kind=kind, evidence=evidence
            )
        else:
            _note(proposal, actor, kind, evidence)
        return _view(session, proposal)


REJECTED = "detections.proposal_rejected"


def reject(
    proposal_id: str, actor: str, reason: str, kind: str = PERSON
) -> Dict[str, Any]:
    """Rejects a proposal and writes ``detections.proposal_rejected`` in the same
    transaction. ``actor`` is a username, or the agent id of a run no person started."""
    from core.agents.attribution import principal_id_for_username

    if not reason.strip():
        raise Refused("Say why this proposal is rejected: it stays on the record.")
    with unit_of_work() as session:
        proposal = _locked(session, proposal_id)
        # A gap draft whose run is still working: the run's save would attach to it, and a
        # rejected draft frees the gap for the next gaps run, which raises it again.
        gap_draft = (proposal.origin or {}).get("source") == GAPS_RUN
        if gap_draft and proposal_id in _unfinished_gap_runs(session):
            raise Refused(
                "Its run is still working on this gap: try again once the run ends."
            )
        proposal.move_to(
            ProposalState.REJECTED,
            actor=actor,
            actor_kind=_kind(kind),
            evidence={"step": "reject"},
            reason=reason,
        )
        if trail.in_use(session):
            trail.append(
                session,
                {
                    trail.ACTION: REJECTED,
                    trail.ACTOR: principal_id_for_username(actor) or actor,
                    trail.OBJECT: proposal_id,
                    "proposal_id": proposal_id,
                    "technique": proposal.technique,
                    "reason": reason,
                    "username": actor,
                    "by": kind,
                },
            )
        return _view(session, proposal)


def from_rule(rule_id: int, actor: str, kind: str = PERSON) -> Dict[str, Any]:
    """A proposal to prove an inventory Sigma rule: the replay runs its Sigma form."""
    with unit_of_work() as session:
        rule = session.get(DetectionRule, rule_id)
        if rule is None:
            raise NotFound(f"No rule {rule_id}.")
        if rule.language != "sigma":
            raise Refused(
                f"This is a {rule.language} rule. Only a Sigma rule can be replayed here."
            )
        content = session.scalar(
            select(DetectionRuleVersion.content).where(
                DetectionRuleVersion.rule_id == rule_id,
                DetectionRuleVersion.version == rule.latest_version,
            )
        )
        if isinstance(content, dict) and "correlation" in content:
            raise Refused(
                "This is a Sigma correlation rule: it correlates other rules' "
                "matches and cannot be replayed on its own."
            )
        existing = session.scalar(
            select(DetectionProposal).where(
                DetectionProposal.rule_id == rule_id,
                DetectionProposal.state != ProposalState.REJECTED,
            )
        )
        if existing is not None:
            return _view(session, existing)
        version = rule.latest_version
        name = rule.name
    return create(
        sigma=sigma_text.dump(content or {}),
        actor=actor,
        kind=kind,
        title=name,
        rule_id=rule_id,
        origin={"source": "inventory", "rule_id": rule_id, "rule_version": version},
        evidence={"source": "inventory", "rule_id": rule_id, "rule_version": version},
    )


def _gap_record(gap: gap_run.Gap, technique: str) -> Dict[str, Any]:
    return {
        "kind": gap.kind,
        "technique": technique,
        "count": gap.count,
        "confirmed": gap.confirmed,
        # The missed runs by run key ("hunt:<id>", "case:<id>"), the alerts on a
        # paper-only technique by finding id, and the intrusion sets and reports that
        # name an intel gap's technique.
        "runs": list(gap.evidence) if gap.kind == gap_run.MISSED else [],
        "alerts": list(gap.evidence) if gap.kind == gap_run.PAPER else [],
        "sources": list(gap.sources),
    }


def for_gaps(gaps: Iterable[gap_run.Gap]) -> List[str]:
    """One draft per raised gap a rule can answer, holding the gap and its evidence; a
    blind spot gets none. A draft has no text until its run saves a rule for it."""
    ids: List[str] = []
    with unit_of_work() as session:
        for gap in gaps:
            if gap.kind not in _DRAFTABLE_GAPS:
                continue
            technique = gap.subject.strip().upper()
            if not sigma_text.TECHNIQUE_ID.match(technique):
                logger.warning("gaps run: %r is not a technique id", gap.subject)
                continue
            record = _gap_record(gap, technique)
            proposal = DetectionProposal.draft(
                title=gap.line(),
                sigma="",
                actor=SYSTEM,
                actor_kind=SYSTEM,
                # The ids stay on the origin alone, where a reader's view withholds the
                # Cases and Findings they can't read; the event log is shown as written.
                evidence={
                    "step": "draft",
                    "source": f"raised by the {GAPS_RUN}",
                    "gap": {
                        k: record[k]
                        for k in ("kind", "technique", "count", "confirmed")
                    },
                },
                technique=technique,
                origin={
                    "source": GAPS_RUN,
                    "rollout": AUTOMATIC_ROLLOUT,
                    "gap": record,
                },
            )
            session.add(proposal)
            session.flush()
            ids.append(proposal.proposal_id)
    return ids


def _gap_proposals(
    session: Session, ids: Sequence[str], *, lock: bool = True
) -> List[DetectionProposal]:
    """The gaps run's own proposals among ``ids``, locked unless only read: a save and a
    person can race."""
    if not ids:
        return []
    query = (
        select(DetectionProposal)
        .where(
            DetectionProposal.proposal_id.in_(list(ids)),
            DetectionProposal.origin["source"].astext == GAPS_RUN,
        )
        .order_by(DetectionProposal.proposal_id)
    )
    return list(session.scalars(query.with_for_update() if lock else query))


def withdraw_gaps(proposal_ids: Sequence[str], reason: str) -> None:
    """Reject the drafts of a gaps run that was never queued; the next run raises them again."""
    with unit_of_work() as session:
        for proposal in _gap_proposals(session, proposal_ids):
            if ProposalState.REJECTED in TRANSITIONS[proposal.state]:
                proposal.move_to(
                    ProposalState.REJECTED,
                    actor=SYSTEM,
                    actor_kind=SYSTEM,
                    evidence={"step": "reject"},
                    reason=reason,
                )


def gap_run_started(proposal_ids: Sequence[str], run_id: str) -> None:
    """Record the run the gaps run's drafts were handed to."""
    with unit_of_work() as session:
        for proposal in _gap_proposals(session, proposal_ids):
            _note(proposal, SYSTEM, SYSTEM, {"step": "run", "run_id": run_id})


def _run_gap_ids(session: Session, run_id: str) -> List[str]:
    """The proposals the gaps run put on the run's ``trigger_context``."""
    context = session.scalar(
        select(WorkflowRun.trigger_context).where(WorkflowRun.run_id == run_id)
    )
    return [
        i for i in (context or {}).get("gap_proposal_ids") or () if isinstance(i, str)
    ]


def _answers(copy: Mapping[str, Any], technique: Optional[str]) -> bool:
    """A copy answers a gap whose technique it carries exactly (a parent or
    subtechnique only touches it, as identify_gaps counts)."""
    return technique in {str(t).strip().upper() for t in copy.get("techniques") or ()}


def _gap_drafts(session: Session, rule_ids: Sequence[int]) -> Set[int]:
    """The overlay copies among ``rule_ids`` a gaps run drafted: those whose first
    version was first seen after the earliest gap proposal that attached them was
    opened. A rule that was there before, such as a person's own the run re-saved
    under its id to answer the gap, is not one."""
    if not rule_ids:
        return set()
    seen = dict(
        session.execute(
            select(DetectionRuleVersion.rule_id, DetectionRuleVersion.first_seen)
            .join(DetectionRule, DetectionRule.id == DetectionRuleVersion.rule_id)
            .where(
                DetectionRuleVersion.rule_id.in_(list(rule_ids)),
                DetectionRuleVersion.version == 1,
                DetectionRule.source == OVERLAY_SOURCE,
            )
        ).all()
    )
    drafts: Set[int] = set()
    for rule_id, first_seen in seen.items():
        opened = session.scalar(
            select(func.min(DetectionProposal.created_at))
            .join(
                DetectionProposalEvent,
                DetectionProposalEvent.proposal_id == DetectionProposal.proposal_id,
            )
            .where(
                DetectionProposal.origin["source"].astext == GAPS_RUN,
                DetectionProposalEvent.evidence.contains(
                    {"step": "attach", "rule_ids": [rule_id]}
                ),
            )
        )
        if opened is not None and opened <= first_seen:
            drafts.add(rule_id)
    return drafts


def _earlier_copies(
    session: Session, proposal: DetectionProposal, run_ids: Sequence[str]
) -> Dict[int, Tuple[str, str]]:
    """The copies the gaps run drafted for the same technique before ``proposal`` that
    a new copy would replace, per language: those attached to the latest earlier
    proposal that attached one of that language. ``{rule_id: (language, native_key)}``.

    Earlier is opened before ``proposal`` and not handed to the same run. One whose run
    saved nothing in a language is passed over, for that language, for the one before
    it; one a person rejected after its run saved is not, as its copies are still the
    drafts Vigil made for the gap. Of the chosen copies only the gap drafts count
    (``_gap_drafts``): a person's own rule the run re-saved stays nobody's predecessor.
    """
    rows = session.execute(
        select(DetectionProposalEvent.proposal_id, DetectionProposalEvent.evidence)
        .join(
            DetectionProposal,
            DetectionProposal.proposal_id == DetectionProposalEvent.proposal_id,
        )
        .where(
            DetectionProposal.origin["source"].astext == GAPS_RUN,
            DetectionProposal.technique == proposal.technique,
            DetectionProposal.created_at < proposal.created_at,
            DetectionProposal.proposal_id.notin_(list(run_ids)),
            DetectionProposalEvent.evidence["step"].astext == "attach",
        )
        .order_by(
            DetectionProposal.created_at.desc(),
            DetectionProposal.proposal_id.desc(),
            DetectionProposalEvent.id,
        )
    ).all()
    latest: Dict[str, str] = {}
    chosen: Set[int] = set()
    for proposal_id, evidence in rows:
        for c in evidence.get("copies") or ():
            if not isinstance(c, dict) or not isinstance(c.get("rule_id"), int):
                continue
            if latest.setdefault(str(c.get("language")), proposal_id) == proposal_id:
                chosen.add(c["rule_id"])
    drafts = _gap_drafts(session, sorted(chosen))
    if not drafts:
        return {}
    return {
        rid: (language, key)
        for rid, language, key in session.execute(
            select(
                DetectionRule.id, DetectionRule.language, DetectionRule.native_key
            ).where(DetectionRule.id.in_(sorted(drafts)))
        ).all()
    }


def replaced_copies(
    run_id: str, saving: Sequence[Mapping[str, Any]]
) -> Dict[str, List[int]]:
    """Each copy a run is saving and the earlier copies it replaces, by native key.

    A copy that will attach to one of the run's gap proposals (``attach_saved``'s
    match) replaces the earlier copies of its language the gaps run drafted for that
    technique (``_earlier_copies``): the gap was raised again, so this draft takes
    their place, whether they were switched off, removed, or are still in shadow or
    live. Only copies a gaps run drafted count, never a person's own rule, and never a
    copy under one of this save's native keys (saved under its own id, it is a new
    version of that rule). Two drafts one run saves are not linked to each other: a
    second rule for the same gap may as well stand beside the first. Each copy is
    ``{language, native_key, techniques}``; a run the gaps run did not queue gets none.
    """
    links: Dict[str, List[int]] = {}
    with unit_of_work() as session:
        run_ids = _run_gap_ids(session, run_id)
        proposals = _gap_proposals(session, run_ids, lock=False)
        keys = {c["native_key"] for c in saving}
        for proposal in proposals:
            matched = [c for c in saving if _answers(c, proposal.technique)]
            if not matched:
                continue
            earlier = _earlier_copies(session, proposal, run_ids)
            for copy in matched:
                replaced = links.setdefault(copy["native_key"], [])
                for rid, (language, key) in sorted(earlier.items()):
                    if (
                        language == copy["language"]
                        and key not in keys
                        and rid not in replaced
                    ):
                        replaced.append(rid)
    return {key: ids for key, ids in links.items() if ids}


# The states a gaps run's save with a new Sigma text takes back to draft.
_REVISABLE = (ProposalState.LINTED, ProposalState.REPLAYED)


def attach_saved(
    run_id: str, actor: str, saved: Sequence[Mapping[str, Any]]
) -> List[str]:
    """Attach the copies a run saved to the gaps it was handed, by ATT&CK technique.

    ``run_id`` comes from the save's signed claims, and the proposals are the ones the
    gaps run put on that run's ``trigger_context``, never ones the model named. Each
    copy is ``{rule_id, language, native_key, techniques, text}``; one attaches to a
    proposal whose technique it carries exactly (a parent or subtechnique only touches
    it, as identify_gaps counts). Returns the proposals attached to: none for a run
    the gaps run did not queue, and none for a child run, which has a run id of its
    own (the gaps run's built-in agent run starts none).
    """
    with unit_of_work() as session:
        attached: List[str] = []
        for proposal in _gap_proposals(session, _run_gap_ids(session, run_id)):
            matched = [c for c in saved if _answers(c, proposal.technique)]
            if not matched:
                continue
            rule_ids = [c["rule_id"] for c in matched]
            # The saved Sigma form is the draft's text, so lint and replay judge it.
            saved_sigma = next(
                (c["text"] for c in matched if c["language"] == "sigma"), None
            )
            # A draft takes the saved copies and text on, and so does one only linted
            # or replayed when the run saved a new Sigma text (it revised its rule).
            # One a person converted or rejected keeps the copies checked against its
            # text: the save is recorded.
            applied = proposal.state is ProposalState.DRAFT or (
                proposal.state in _REVISABLE and saved_sigma is not None
            )
            sigma = saved_sigma if applied else None
            if applied:
                proposal.copies = {
                    **proposal.copies,
                    **{
                        c["language"]: c["text"]
                        for c in matched
                        if c["language"] in COPY_LANGUAGES
                    },
                }
                # The copy the stages follow: a SIEM's, else the Sigma form.
                proposal.rule_id = next(
                    (c["rule_id"] for c in matched if c["language"] != "sigma"),
                    rule_ids[0],
                )
            evidence = {
                "step": "attach",
                "run_id": run_id,
                "rule_ids": rule_ids,
                "copies": [
                    {k: c[k] for k in ("rule_id", "language", "native_key")}
                    for c in matched
                ],
                "applied": applied,
                "rollout": AUTOMATIC_ROLLOUT,
            }
            reason = "saved by the run the gaps run queued"
            if applied and proposal.state is not ProposalState.DRAFT:
                # The checks it passed judged the old text: back to draft.
                proposal.move_to(
                    ProposalState.DRAFT,
                    actor=actor,
                    actor_kind=AGENT,
                    evidence=evidence,
                    reason=reason,
                    sigma=sigma,
                )
            else:
                _note(proposal, actor, AGENT, evidence, reason=reason, sigma=sigma)
            attached.append(proposal.proposal_id)
        return attached


# A gaps run still working: queued, or launched and running. One parked on a person
# (waiting on an approval, or paused) may never resume, so it holds nothing.
_RUNNING_INVESTIGATION_STATUSES = tuple(
    s for s in LIVE_INVESTIGATION_STATUSES if s not in PARKED_INVESTIGATION_STATUSES
)


def _unfinished_gap_rows(*columns: Any) -> Any:
    """``columns`` of each gaps-run intake row whose run is still working: the row is
    still queued, or the investigation it launched is assigned or executing."""
    return (
        select(*columns)
        .outerjoin(
            Investigation,
            Investigation.investigation_id == IntakeTrigger.investigation_id,
        )
        .where(
            IntakeTrigger.kind == "schedule",
            IntakeTrigger.payload.has_key("gap_proposal_ids"),
            or_(
                IntakeTrigger.state == "queued",
                and_(
                    IntakeTrigger.state == "launched",
                    Investigation.status.in_(_RUNNING_INVESTIGATION_STATUSES),
                ),
            ),
        )
    )


def _unfinished_gap_runs(session: Session) -> Set[str]:
    """The gaps-run proposals whose run is still working (``_unfinished_gap_rows``)."""
    payloads: Iterable[Optional[Dict[str, Any]]] = session.scalars(
        _unfinished_gap_rows(IntakeTrigger.payload)
    )
    return {
        i
        for payload in payloads
        for i in (payload or {}).get("gap_proposal_ids") or ()
        if isinstance(i, str)
    }


def _shadow_copies(session: Session) -> Dict[int, List[str]]:
    """Each active Splunk, Elastic or Wazuh copy whose latest stage is shadow, with the
    techniques it names. A live copy is not here, nor one off, nor one never staged
    (only Vigil's own copies are); the Sigma form runs nowhere."""
    s = DetectionRuleStage
    latest = (
        select(s.rule_id, s.stage)
        .distinct(s.rule_id)
        .order_by(s.rule_id, s.at.desc(), s.id.desc())
        .subquery()
    )
    return {
        rule_id: list(techniques or ())
        for rule_id, techniques in session.execute(
            select(DetectionRule.id, DetectionRule.techniques)
            .join(latest, latest.c.rule_id == DetectionRule.id)
            .where(
                DetectionRule.status == "active",
                DetectionRule.language.in_(COPY_LANGUAGES),
                latest.c.stage == "shadow",
            )
        )
    }


def unfinished_gap_run() -> Optional[Tuple[int, Optional[str]]]:
    """The intake row id and investigation id (None while queued) of a gaps run still
    working, the oldest first; None when there is none."""
    with unit_of_work() as session:
        row = session.execute(
            _unfinished_gap_rows(
                IntakeTrigger.id, IntakeTrigger.investigation_id
            ).order_by(IntakeTrigger.id)
        ).first()
    return None if row is None else (row[0], row[1])


def worked_techniques() -> Set[str]:
    """The techniques a rule is already proposed or in shadow for, which the gaps run
    does not raise again:

    - a gaps-run proposal not rejected (or withdrawn) whose run is still working;
    - a copy in shadow, whatever drafted it: the techniques it names, and for a copy
      attached to a gaps-run proposal, that proposal's technique too, rejected or
      not (a reject does not stop the copies' rollout).

    So a run that ended (or parked) without saving a rule frees the technique, and so
    does switching off or removing every copy in shadow. A live copy holds nothing: a
    gap that still shows once its rule is live is that rule missing it."""
    with unit_of_work() as session:
        shadow = _shadow_copies(session)
        worked = {
            t.strip().upper() for techniques in shadow.values() for t in techniques if t
        }
        rows = session.scalars(
            select(DetectionProposal).where(
                DetectionProposal.origin["source"].astext == GAPS_RUN,
                DetectionProposal.technique.isnot(None),
            )
        ).all()
        steps = _link_steps(session, [p.proposal_id for p in rows])
        unfinished = _unfinished_gap_runs(session)
        for p in rows:
            attached = _links(p, steps.get(p.proposal_id, ()))["rule_ids"]
            if any(r in shadow for r in attached) or (
                p.proposal_id in unfinished and p.state != ProposalState.REJECTED
            ):
                worked.add((p.technique or "").strip().upper())
        return worked


# --- Hunts, and the proposals and rules they led to (#4698) -----------------

# Why a proven hypothesis got no proposal, in the order they are checked.
OUTSIDE_WINDOW, NO_TECHNIQUE, UNKNOWN, CAUGHT = (
    "outside_window",
    "no_technique",
    "unknown",
    "caught",
)
WORKFLOW_OFF, HELD, WAITING, BELOW_FLOOR = (
    "workflow_off",
    "held",
    "waiting",
    "below_floor",
)


def _hunt_rows(hunt_ids: Iterable[str]) -> List[Dict[str, str]]:
    return [{"investigation_kind": "hunt", "investigation_id": h} for h in hunt_ids]


def _hunt_runs(session: Session, hunt_ids: Set[str]) -> Dict[str, Dict[str, Any]]:
    """Each hunt's run from its distil marker, as ``prior_hunts`` reads it; a hunt run on
    a Case the reader can't read is withheld (#4359). A hunt with no marker is absent.
    """
    if not hunt_ids:
        return {}
    hidden = gaps._unreadable_runs(session, _hunt_rows(hunt_ids))
    rows = session.execute(
        select(
            EpisodicDistilMarker.investigation_id, EpisodicDistilMarker.origin_run_id
        ).where(
            EpisodicDistilMarker.investigation_kind == "hunt",
            EpisodicDistilMarker.investigation_id.in_(list(hunt_ids)),
        )
    ).all()
    return {
        hunt_id: (
            {"run_id": None, "withheld": True}
            if ("hunt", hunt_id) in hidden
            else {"run_id": str(run_id) if run_id else None, "withheld": False}
        )
        for hunt_id, run_id in rows
    }


def _latest_stages(session: Session, rule_ids: Sequence[int]) -> Dict[int, Dict]:
    latest: Dict[int, Dict[str, Any]] = {}
    for row in _stage_view(session, list(rule_ids)):
        latest[row["rule_id"]] = row
    return latest


def _proposal_rows(
    session: Session, proposals: Sequence[DetectionProposal]
) -> List[Dict[str, Any]]:
    """Gaps-run proposals in brief, batched as ``listing`` reads them: state, technique,
    the run they were handed to, and each attached rule copy with its current stage."""
    ids = [p.proposal_id for p in proposals]
    steps = _link_steps(session, ids)
    links = {p.proposal_id: _links(p, steps.get(p.proposal_id, ())) for p in proposals}
    rule_ids = sorted({r for link in links.values() for r in link["rule_ids"]})
    stages = _latest_stages(session, rule_ids)
    rules = {
        r.id: r
        for r in session.scalars(
            select(DetectionRule).where(DetectionRule.id.in_(rule_ids))
        )
    }
    statuses = _run_statuses(session, (link["run_id"] for link in links.values()))
    unfinished = _unfinished_gap_runs(session)
    hunts = _hunt_runs(session, _gap_case_ids((p.origin for p in proposals), "hunt"))
    out = []
    for p in proposals:
        link = links[p.proposal_id]
        keys = _gap_case_ids([p.origin], "hunt")
        out.append(
            {
                "proposal_id": p.proposal_id,
                "title": p.title,
                "technique": p.technique,
                "state": p.state.value,
                "created_at": p.created_at.isoformat(),
                **link,
                "run_status": statuses.get(link["run_id"]),
                "run_working": p.proposal_id in unfinished,
                # The copies attached, each with its stage; a removed one has no name.
                "rules": [
                    {
                        "rule_id": rid,
                        "name": rules[rid].name if rid in rules else None,
                        "language": rules[rid].language if rid in rules else None,
                        "status": rules[rid].status if rid in rules else "removed",
                        "version": (
                            stages[rid]["version"]
                            if rid in stages
                            else rules[rid].latest_version if rid in rules else None
                        ),
                        "stage": stages[rid]["stage"] if rid in stages else None,
                    }
                    for rid in link["rule_ids"]
                ],
                "hunts": {h: hunts[h] for h in sorted(keys) if h in hunts},
            }
        )
    return out


def _gap_proposals_naming(session: Session, keys: Sequence[str]):
    """Every gaps-run proposal, rejected or withdrawn too, whose evidence names one of
    ``keys`` ("hunt:<id>"), oldest first."""
    if not keys:
        return []
    return session.scalars(
        select(DetectionProposal)
        .where(
            DetectionProposal.origin["source"].astext == GAPS_RUN,
            or_(
                *(
                    DetectionProposal.origin.contains({"gap": {"runs": [k]}})
                    for k in keys
                )
            ),
            ~_hidden(),
        )
        .order_by(DetectionProposal.created_at)
    ).all()


def _missed_runs(
    window_start: datetime, raised: Mapping[str, str], techniques: Set[str]
) -> Dict[str, Tuple[Set[str], Set[str]]]:
    """Each technique's new missed runs as the gaps run counts them: proven Verdicts in
    its window, by run key, that no run raised yet and that reconstruct as missed (or
    LogLM only), as ``missed_runs_report`` groups them; and those a person confirmed.
    Only Verdicts on ``techniques`` are reconstructed."""
    out: Dict[str, Tuple[Set[str], Set[str]]] = {t: (set(), set()) for t in techniques}
    for v in gaps.read_ledger(gap_run.WINDOW_DAYS):
        key = gap_run._run_key(v)
        on = techniques & v["techniques"]
        if not on or v["outcome"] != "proven" or key in raised:
            continue
        if gaps._naive_utc(v["concluded_at"]) < gaps._naive_utc(window_start):
            continue
        _, record = gaps.reconstruct_verdict(v, gaps.read_window_findings)
        if record.get("verdict") not in ("missed", "loglm"):
            continue
        for t in on:
            out[t][0].add(key)
            if v["trust"] == "analyst":
                out[t][1].add(key)
    return out


def _why_none(
    v: Dict[str, Any],
    techniques: Set[str],
    *,
    window_start: datetime,
    named: bool,
    context: Dict[str, Any],
) -> Tuple[str, str]:
    """One reason a proven Verdict got no proposal, and its detail. ``techniques`` are
    its listable techniques no proposal for this hunt covers; ``named`` is whether any
    proposal names the hunt; ``context`` caches the reads shared across Verdicts."""
    if gaps._naive_utc(v["concluded_at"]) < gaps._naive_utc(window_start):
        return OUTSIDE_WINDOW, (
            f"Concluded more than {gap_run.WINDOW_DAYS} days ago, before the window "
            "the daily gaps run reads."
        )
    if not techniques:
        return NO_TECHNIQUE, (
            "It names no ATT&CK technique a rule could be drafted for."
            if not v["techniques"]
            else "It names no active ATT&CK technique: "
            + ", ".join(sorted(v["techniques"]))
            + "."
        )
    reason, record = gaps.reconstruct_verdict(v, gaps.read_window_findings)
    if reason:
        return UNKNOWN, f"{reason[:1].upper()}{reason[1:]}."
    if record["verdict"] in ("rule", "both"):
        names = sorted(
            {c["rule_name"] for c in record["citations"] if c.get("rule_name")}
        )
        alerts = len(record["citations"])
        return CAUGHT, (
            f"A rule alerted on it: {alerts} alert{'' if alerts == 1 else 's'} in its "
            "window name its hosts, IPs or users"
            + (f" ({', '.join(names)})." if names else ".")
        )
    if "enabled" not in context:
        context["enabled"] = is_enabled(DETECTION_GAPS_WORKFLOW)
    if not context["enabled"]:
        return WORKFLOW_OFF, "The daily detection gaps run is turned off."
    if "raised" not in context:
        context["raised"] = gap_run.read_raised().runs
    listed = ", ".join(sorted(techniques))
    if gap_run._run_key(v) in context["raised"]:
        if "worked" not in context:
            context["worked"] = worked_techniques()
        if not named or techniques & context["worked"]:
            return HELD, (
                f"The gaps run held it back: a rule for {listed} was already proposed "
                "or in shadow."
            )
        # Counted once, for another technique's proposal: a run is never counted twice.
        return BELOW_FLOOR, (
            "The gaps run counted this hunt once, for the proposal that names it, and "
            f"{listed} was below the floor then; it does not count a hunt twice."
        )
    counts = context.setdefault("counts", {})
    wanted = techniques - counts.keys()
    if wanted:
        counts.update(_missed_runs(window_start, context["raised"], wanted))
    counted = max(len(counts[t][0]) for t in techniques)
    if any(counts[t][1] for t in techniques):
        return WAITING, (
            "A person confirmed a run on it that no rule caught, so the next daily "
            "gaps run raises it."
        )
    if counted >= gap_run.GAP_FLOOR:
        return WAITING, (
            f"{counted} runs on it in the window that no rule caught: the next daily "
            "gaps run raises it."
        )
    return BELOW_FLOOR, (
        f"{counted} run{'' if counted == 1 else 's'} on it in the window that no rule "
        "caught, none confirmed by a person: the gaps run raises a technique at "
        f"{gap_run.GAP_FLOOR} runs, or one a person confirmed."
    )


def for_hunt(run_id: str) -> Dict[str, Any]:
    """What a hunt run led to for detection: the proposals the gaps run raised from it
    (rejected and withdrawn ones too), each with its rule copies and their stage, and
    for each proven hypothesis on a technique none of them covers, why not.

    The hunt is the run's distil marker; a run on a Case the caller can't read is not
    found (#4359). Nothing here is stored: the reasons are worked out on every read.
    """
    window_start = datetime.now(timezone.utc) - timedelta(days=gap_run.WINDOW_DAYS)
    with unit_of_work() as session:
        if run_hidden(session, run_id):
            raise NotFound(f"No run {run_id}.")
        hunt_ids = sorted(
            session.scalars(
                select(EpisodicDistilMarker.investigation_id).where(
                    EpisodicDistilMarker.investigation_kind == "hunt",
                    cast(EpisodicDistilMarker.origin_run_id, String) == run_id,
                )
            )
        )
        found = _gap_proposals_naming(session, [f"hunt:{h}" for h in hunt_ids])
        rows = _proposal_rows(session, found)
        # Read directly, not through the ledger's window: one older is said to be so.
        verdicts = [
            {**gaps.verdict_row(v), "statement": v.statement}
            for v in session.scalars(
                select(EpisodicVerdict)
                .where(
                    EpisodicVerdict.investigation_kind == "hunt",
                    EpisodicVerdict.investigation_id.in_(hunt_ids),
                )
                .order_by(EpisodicVerdict.concluded_at, EpisodicVerdict.id)
            )
        ]
    covered = {(p.technique or "").strip().upper() for p in found}
    named = _gap_case_ids((p.origin for p in found), "hunt")
    context: Dict[str, Any] = {}
    without = []
    for v in verdicts:
        if v["outcome"] != "proven":
            continue  # benign and ruled-out hypotheses get no row
        listable = {t for t in v["techniques"] if gaps._is_listable(t)}
        if listable and listable <= covered:
            continue
        v["entities"] = gaps._subject_entities(v["subjects"])
        code, detail = _why_none(
            v,
            listable - covered,
            window_start=window_start,
            named=v["investigation_id"] in named,
            context=context,
        )
        without.append(
            {
                "hunt_id": v["investigation_id"],
                "hypothesis_id": v["hypothesis_id"],
                "statement": v["statement"],
                "techniques": sorted(v["techniques"]),
                "trust": v["trust"],
                "concluded_at": v["concluded_at"].isoformat(),
                "reason": code,
                "detail": detail,
            }
        )
    return {
        "run_id": run_id,
        "hunt_ids": hunt_ids,
        "proposals": rows,
        "without": without,
    }


def for_rule(rule_id: int) -> List[Dict[str, Any]]:
    """The gaps-run proposals a rule copy answers (an attach event names it, or it is the
    copy their stages follow), oldest first, each with the hunts behind it."""
    with unit_of_work() as session:
        attached = exists().where(
            DetectionProposalEvent.proposal_id == DetectionProposal.proposal_id,
            DetectionProposalEvent.evidence.contains(
                {"step": "attach", "rule_ids": [rule_id]}
            ),
        )
        found = session.scalars(
            select(DetectionProposal)
            .where(
                DetectionProposal.origin["source"].astext == GAPS_RUN,
                or_(DetectionProposal.rule_id == rule_id, attached),
                ~_hidden(),
            )
            .order_by(DetectionProposal.created_at)
        ).all()
        return _proposal_rows(session, found)
