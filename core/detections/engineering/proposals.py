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
  not here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.detections.engineering import lint as lint_mod
from core.detections.engineering import siem_replay, sigma_text
from core.detections.overlay import Refused
from core.storage.models import (
    Case,
    DetectionProposal,
    DetectionProposalEvent,
    DetectionRule,
    DetectionRuleStage,
    DetectionRuleVersion,
)
from core.storage.models.detection_proposal import (
    ACTOR_KINDS,
    TRANSITIONS,
    InvalidProposalMove,
    ProposalState,
)
from core.storage.unit_of_work import unit_of_work

PERSON, AGENT = "person", "agent"
# The SIEMs a person can paste a native copy for, as the overlay saves them.
COPY_LANGUAGES = ("splunk", "elastic", "wazuh")


class NotFound(LookupError):
    pass


def _normal_technique(technique: Optional[str]) -> Optional[str]:
    value = (technique or "").strip().upper()
    if not value:
        return None
    if not sigma_text.TECHNIQUE_ID.match(value):
        raise Refused(f"{technique!r} is not an ATT&CK technique id such as T1059.001.")
    return value


def _locked(session: Session, proposal_id: str) -> DetectionProposal:
    # Two people can race the same move.
    proposal = session.scalar(
        select(DetectionProposal)
        .where(DetectionProposal.proposal_id == proposal_id)
        .with_for_update()
    )
    if proposal is None:
        raise NotFound(f"No proposal {proposal_id}.")
    return proposal


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


def _view(session: Session, proposal: DetectionProposal) -> Dict[str, Any]:
    events = _events(session, proposal)
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
        latest[step] = _event_view(event, proposal.sigma) if event else None
    return {
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "technique": proposal.technique,
        "case_id": proposal.case_id,
        "origin": proposal.origin,
        "sigma": proposal.sigma,
        "copies": proposal.copies,
        "state": proposal.state.value,
        "rule_id": proposal.rule_id,
        "created_by": proposal.created_by,
        "created_at": proposal.created_at.isoformat(),
        "updated_at": proposal.updated_at.isoformat(),
        "allowed": sorted(s.value for s in TRANSITIONS[proposal.state]),
        "latest": latest,
        "events": [_event_view(e, proposal.sigma) for e in events],
        "stages": _stage_view(session, [proposal.rule_id]) if proposal.rule_id else [],
    }


def get(proposal_id: str) -> Dict[str, Any]:
    with unit_of_work() as session:
        proposal = session.get(DetectionProposal, proposal_id)
        if proposal is None:
            raise NotFound(f"No proposal {proposal_id}.")
        return _view(session, proposal)


def listing(state: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first, each with its latest replay's counts, oldest detail left to ``get``."""
    with unit_of_work() as session:
        statement = select(DetectionProposal).order_by(
            DetectionProposal.created_at.desc()
        )
        if state:
            statement = statement.where(DetectionProposal.state == ProposalState(state))
        proposals = session.scalars(statement).all()
        replays = _latest_replays(session, [p.proposal_id for p in proposals])
        return [
            {
                "proposal_id": p.proposal_id,
                "title": p.title,
                "technique": p.technique,
                "case_id": p.case_id,
                "state": p.state.value,
                "origin": p.origin,
                "created_by": p.created_by,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
                "replay": replays.get(p.proposal_id),
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
    if not reads_findings:
        view = _without_finding_content(view)
    if case_id and not reads_cases:
        view = {**_without_case(view, case_id), "case_id": None}
        if isinstance(view.get("origin"), dict):
            view["origin"] = {**view["origin"], "case_id": None}
    return view


def case_of(proposal_id: str) -> Optional[str]:
    with unit_of_work() as session:
        proposal = session.get(DetectionProposal, proposal_id)
        if proposal is None:
            raise NotFound(f"No proposal {proposal_id}.")
        return proposal.case_id


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
    if case is None:
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
    """What a replay reads: the text, its Case, and the events named last time."""
    with unit_of_work() as session:
        proposal = session.get(DetectionProposal, proposal_id)
        if proposal is None:
            raise NotFound(f"No proposal {proposal_id}.")
        if proposal.state not in (ProposalState.LINTED, ProposalState.REPLAYED):
            raise InvalidProposalMove(proposal.state, ProposalState.REPLAYED)
        previous = _latest_step(_events(session, proposal), "replay")
        return {
            "sigma": proposal.sigma,
            "case_id": proposal.case_id,
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
        target["sigma"], case_id=target["case_id"], named=named, days=days
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


def reject(
    proposal_id: str, actor: str, reason: str, kind: str = PERSON
) -> Dict[str, Any]:
    if not reason.strip():
        raise Refused("Say why this proposal is rejected: it stays on the record.")
    with unit_of_work() as session:
        proposal = _locked(session, proposal_id)
        proposal.move_to(
            ProposalState.REJECTED,
            actor=actor,
            actor_kind=_kind(kind),
            evidence={"step": "reject"},
            reason=reason,
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
        existing = session.scalar(
            select(DetectionProposal).where(
                DetectionProposal.rule_id == rule_id,
                DetectionProposal.state != ProposalState.REJECTED,
            )
        )
        if existing is not None:
            return _view(session, existing)
        content = session.scalar(
            select(DetectionRuleVersion.content).where(
                DetectionRuleVersion.rule_id == rule_id,
                DetectionRuleVersion.version == rule.latest_version,
            )
        )
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
