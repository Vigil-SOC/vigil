"""The Case Distil (#733): a closed Case becomes a Verdict.

A Case is a Hypothesis, implicitly. Opening one asserts that the activity is
real and malicious; closing it answers that. So the case id is the
``hypothesis_id``, the title is the statement, and the closure category is the
outcome. A closure is a conclusion and not an observation -- what was *observed*
were the Case's Findings -- so this writes a Verdict and its sources, and never a
Sighting.

**Why this polls state rather than hooking a close.** There is no single
category-bearing close in this codebase. Analysts close by PATCHing ``status``
to ``closed``; the dedicated ``POST /cases/{id}/close`` records a category and
is not what the console calls; the MCP tool closes on its own; ``merge_cases``
closes the source of a merge. A hook on any one of them would miss the path the
issue exists for. Polling the closed *state* catches every writer that exists
and every writer added later, needs no change to any of them, and makes a
re-close or a re-categorised closure a re-derive rather than a duplicate.

The mapping's whole input is the Case: its closure row, its Findings and its
IOCs. Nothing is asked of a model and nothing is extracted from prose. That is
deliberate -- the entity extractor lives in the harness (TypeScript) and a second
implementation here would drift while only one is gated, which is the same
argument ``core/memory/distil.py`` makes about the fold.

Idempotency, the marker and the delete-then-insert are shared with the hunt
Distil, and the marker is keyed by investigation rather than by run, which is
what lets a Case take one at all.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from core.cases.closure import ClosedByKind, ClosureCategory
from core.memory.distil import (
    DEFAULT_BATCH,
    FailureKey,
    clear_failure,
    clear_investigation,
    write_marker,
    write_with_retry,
)
from core.memory.entity_keys import entity_key
from core.memory.recall_contract import (
    ENTITY_KEY_TYPES,
    VERDICT_SUBJECT_CAP,
    InvestigationKind,
    Stance,
    Trust,
    VerdictOutcome,
    WindowSource,
    bounded_prose,
)
from core.memory.source_tier import InvestigationKind as TierKind
from core.memory.source_tier import SourceTier, resolve_source_tier
from core.storage.models import (
    Case,
    CaseClosureInfo,
    CaseIOC,
    EpisodicDistilMarker,
    EpisodicVerdict,
    EpisodicVerdictSource,
    Finding,
    case_findings,
)
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# Bumped when the mapping below changes what it writes. Separate from the hunt
# Distil's version: re-deriving is delete-then-insert scoped to one
# investigation, and a change to how a Case maps must not re-offer every hunt.
CASE_DISTIL_MAPPING_VERSION = 1

# What each closure category concluded. ``duplicate`` is absent on purpose: a
# Case closed as a duplicate concluded nothing about the activity, it said this
# record is the same record as another one. Writing an inconclusive Verdict for
# it would put a claim in memory that nobody made, and writing a proven one
# would count one determination twice.
_OUTCOMES: Mapping[ClosureCategory, VerdictOutcome] = {
    ClosureCategory.RESOLVED: VerdictOutcome.PROVEN,
    ClosureCategory.FALSE_POSITIVE: VerdictOutcome.FALSE_POSITIVE,
    ClosureCategory.UNABLE_TO_RESOLVE: VerdictOutcome.INCONCLUSIVE,
    ClosureCategory.UNSPECIFIED: VerdictOutcome.INCONCLUSIVE,
}

# Cases whose episodic rows disagree with their current state, in both
# directions: closed ones whose Verdict is missing, stale, or derived at another
# version, and reopened ones still holding a Verdict they have retracted.
#
# The second direction is why the marker is joined without the version and the
# version compared in the predicate instead. Matching it in the join would make
# a marker at any other version invisible, and a reopened Case would keep a
# Verdict nothing could see to withdraw.
#
# ``closed_at`` and ``updated_at`` are naive UTC on these tables and
# ``concluded_at`` is timestamptz, so the close instant is cast rather than
# compared across the two -- an implicit cast reads a naive value in the
# server's zone and silently shifts the comparison.
#
# The later of the two, not the closure's alone: what a Verdict says about a
# Case is drawn from the Case's Findings as much as from its closure, and a
# merge moves Findings across without touching either closure. Keyed on
# ``closed_at`` where one exists, that edit is unreachable and both Cases keep
# a window their Findings no longer support. ``GREATEST`` ignores nulls here,
# so a Case with no closure row still falls back to ``updated_at``.
#
# The failure join is the hunt poll's, against this table -- see
# ``core/memory/distil.py`` for what it is for. Two things are this poll's own.
#
# The predicate is ANDed over both directions: a withdrawal that keeps failing
# is as much a subject to stop hammering as a closure that does.
#
# And the failure only counts while the Case has not moved since it was
# recorded. A hunt's input is a frozen fold, so "this will not become mappable
# by being read again" holds and a refusal there waits forever. A Case's input
# is live rows, and this module polls state precisely so that a re-close or a
# re-categorisation is a re-derive. Without this clause a Case that failed --
# refused, and so parked at ``RETRY_NEVER`` -- would stay unwritten through every
# later edit that fixed it, and the staleness test above would be unreachable
# behind the failure predicate. Cast for the same reason that test is: these
# columns are naive UTC and ``last_failed_at`` is timestamptz.
_CANDIDATES = text("""
    SELECT c.case_id AS case_id
    FROM cases c
    LEFT JOIN case_closure_info i ON i.case_id = c.case_id
    LEFT JOIN episodic_distil_markers m
           ON m.investigation_kind = 'case'
          AND m.investigation_id = c.case_id
    LEFT JOIN episodic_distil_failures f
           ON f.investigation_kind = 'case'
          AND f.failure_key = c.case_id
          AND f.distil_version = :version
          AND f.last_failed_at >= (GREATEST(i.closed_at, c.updated_at) AT TIME ZONE 'UTC')
    WHERE (f.failure_key IS NULL OR f.next_attempt_at <= now())
      AND (
        (
          c.status = 'closed'
          AND (
            m.investigation_id IS NULL
            OR m.distil_version <> :version
            OR m.concluded_at < (GREATEST(i.closed_at, c.updated_at) AT TIME ZONE 'UTC')
          )
        )
        OR (c.status <> 'closed' AND m.investigation_id IS NOT NULL)
      )
    ORDER BY GREATEST(i.closed_at, c.updated_at) DESC, c.case_id
    LIMIT :limit
    """)


class CaseDistilRefused(RuntimeError):
    """A Case this job will not map. Raised rather than written around."""


@dataclass(frozen=True)
class ClosedCase:
    """One closed Case and everything a Verdict built from it needs.

    Read once, refused once if the Case records no instant it closed at, and
    then carried whole: no row can be built from a subset of these, and passing
    them separately invites a caller to build one from the wrong Case's closure.

    That the Case is closed at all is the caller's question, settled before this
    is built -- a Case that is not closed is withdrawn rather than mapped.
    """

    case: Case
    closure: Optional[CaseClosureInfo]
    concluded_at: datetime

    @property
    def category(self) -> Optional[ClosureCategory]:
        """What the closure determined, or None if it said something else.

        A Case closed without a closure row -- a status edit that predates the
        console recording one, or a direct write to the status column -- is not
        a Case with an unknown determination. It is a Case closed with no
        determination stated, which is exactly what ``unspecified`` says.

        None is the other thing: the API states this vocabulary and rejects
        anything outside it, but the column is free text and rows predating that
        exist, so a value nobody here can map is representable rather than a
        crash. The caller writes no Verdict for it and says so.
        """
        if self.closure is None:
            return ClosureCategory.UNSPECIFIED
        recorded = (self.closure.closure_category or "").strip()
        if not recorded:
            return ClosureCategory.UNSPECIFIED
        try:
            return ClosureCategory(recorded)
        except ValueError:
            return None

    @property
    def trust(self) -> Trust:
        """Who concluded, from what the close recorded rather than from a name.

        Absent -- a legacy row, or a close that never wrote one -- reads as
        ``agent``. That understates rather than overstates: ``analyst`` is the
        highest-trust record this system produces, and a close nobody can
        attribute has not earned it.
        """
        kind = (self.closure.closed_by_kind if self.closure is not None else "") or ""
        return (
            Trust.ANALYST if kind.strip() == ClosedByKind.ANALYST.value else Trust.AGENT
        )


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """A naive column value read as the UTC it was written as.

    These tables store naive UTC (``core.time.utcnow``) and the episodic columns
    are ``timestamptz``. Attaching the zone here is what stops a deployment
    outside UTC writing a window shifted by its own offset.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _rationale(closure: Optional[CaseClosureInfo]) -> str:
    """Why the Case closed, in the order a reader would want it.

    Empty is a known absence rather than a null: the Case was closed without a
    stated reason, which is true, and worth recording as such. A Verdict that
    refused to be written for want of prose would lose the determination too.
    """
    if closure is None:
        return ""
    for candidate in (
        closure.false_positive_reason,
        closure.root_cause,
        closure.executive_summary,
        closure.closure_notes,
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _subjects(session: Session, case_id: str) -> List[str]:
    """The entities the Case names, from its IOCs.

    Not from the title. ADR 0016 draws a Verdict's subjects from its Hypothesis
    statement, extracted with the harness's typed extractor -- which is
    TypeScript, and re-implementing it here is the second implementation that
    drifts while only one is gated. A Case has something the harness has to
    infer: IOCs an analyst attached, already carrying their type.

    Two bounds the extractor gets for free and this does not. ``ioc_type`` is
    free text, so a type outside the Entity Key vocabulary is dropped -- a
    `mutex` key is one no reader will ever query, and an unqueryable key reads
    as an entity nobody has looked at rather than as a bad write. And an IOC
    list has no sentence to bound it, where the extractor works on one, so the
    cap ADR 0016 assumes is stated here and logged when it bites.

    **Known limitation.** Nothing populates ``case_iocs`` on any close path:
    they arrive only from an analyst using the console's IOC form or an agent
    calling ``add_case_ioc``, and across the live estate the table is empty. So
    an ordinary Case names no subjects today and its Verdict is reachable only
    by narrative recall, exactly as #731's hunt Verdicts are. This is not an
    argument for the alternatives -- extracting a Case title yields nothing
    either, and scooping ``Finding.entity_context`` across a Case's Findings
    yields the shared infrastructure ADR 0016 exists to keep off a Verdict. How
    a Case names its subjects is an open question with its own issue; this is
    the half of it that is decided, and it is right whenever the IOCs exist.
    """
    rows = (
        session.query(CaseIOC.ioc_type, CaseIOC.value)
        .filter(CaseIOC.case_id == case_id)
        .order_by(CaseIOC.ioc_id)
        .all()
    )
    keys: List[str] = []
    seen = set()
    for ioc_type, value in rows:
        kind = str(ioc_type or "").strip().lower()
        if kind not in ENTITY_KEY_TYPES:
            continue
        key = entity_key(kind, str(value or ""))
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    if len(keys) > VERDICT_SUBJECT_CAP:
        logger.warning(
            "Case Distil: %s names %s entities, keeping the first %s (ADR 0016)",
            case_id,
            len(keys),
            VERDICT_SUBJECT_CAP,
        )
    return keys[:VERDICT_SUBJECT_CAP]


def _window(
    session: Session, closed: ClosedCase
) -> Tuple[datetime, datetime, WindowSource]:
    """When the activity happened, and whether that was observed or asserted.

    A Case's Findings observed it: their ``timestamp`` and ``data_source`` are
    both non-null, so the min and max of them is a real window. A Case with no
    Findings -- a human-created one -- has only its own open and close dates,
    and says so, so that nothing claims precision it does not have. Ranking can
    discount an asserted window, and the error direction is the safe one: it
    reads as recent, so it ranks up rather than disappearing.
    """
    bounds = (
        session.query(Finding.timestamp)
        .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
        .filter(case_findings.c.case_id == closed.case.case_id)
        # Both halves, as the spec states them: a Finding that names no system
        # did not observe anything this Verdict can claim was observed, and it
        # contributes no source row either -- so counting its timestamp would
        # produce an `observed` window with nothing behind it.
        .filter(Finding.timestamp.isnot(None))
        .filter(func.trim(Finding.data_source) != "")
        .all()
    )
    stamps = [_utc(stamp) for (stamp,) in bounds if stamp is not None]
    if stamps:
        return min(stamps), max(stamps), WindowSource.OBSERVED

    opened = _utc(closed.case.created_at) or closed.concluded_at
    return min(opened, closed.concluded_at), closed.concluded_at, WindowSource.ASSERTED


def _sources(session: Session, case_id: str) -> List[Dict[str, str]]:
    """One row per system that produced a Finding on the Case, all supporting.

    Every source takes Stance ``supports`` because the Case asserted the
    activity was real -- that is what grouping those Findings into one claimed.
    Which makes a false-positive closure a clean record of every one of those
    sources having been wrong: the calibration signal, arriving from live
    operation rather than from a backfill.

    Tier stamping follows ``distil._sources_of``: written at write time, never
    joined at read time, and a ``not_evidence`` source logged rather than
    dropped. What differs is only where the Stance comes from -- a hunt takes
    one per source from its fold, and a Case has one answer for all of them.
    """
    rows = (
        session.query(Finding.data_source)
        .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
        .filter(case_findings.c.case_id == case_id)
        .distinct()
        .order_by(Finding.data_source)
        .all()
    )
    sources: List[Dict[str, str]] = []
    for (system,) in rows:
        name = str(system or "").strip()
        if not name:
            continue
        tier = resolve_source_tier(name, TierKind.CASE)
        if tier is SourceTier.NOT_EVIDENCE:
            logger.warning(
                "Case Distil: %s cites %r, which is not evidence", case_id, name
            )
        sources.append(
            {
                "source_system": name,
                "stance": Stance.SUPPORTS.value,
                "source_tier": tier.value,
            }
        )
    return sources


def _accept(session: Session, case: Case) -> ClosedCase:
    """A closed Case with its closure, or a refusal if nothing dates the close.

    Only the instant is required: a Case closed without a closure row is a Case
    closed with no determination stated, which ``ClosedCase.category`` maps.
    """
    case_id = case.case_id
    closure = session.get(CaseClosureInfo, case_id)
    concluded_at = _utc(closure.closed_at if closure is not None else None) or _utc(
        case.updated_at
    )
    if concluded_at is None:
        raise CaseDistilRefused(
            f"{case_id} is closed but records no instant it closed at"
        )
    return ClosedCase(case, closure, concluded_at)


def _withdraw(session: Session, case_id: str, status: Optional[str]) -> Dict[str, int]:
    """Take back what a Case concluded, because it is no longer closed.

    A reopened Case has retracted its determination, and memory that keeps
    asserting it is memory arguing with the analyst who reopened it. The rows
    are already cleared by the caller; what is left is the marker, and deleting
    it rather than rewriting it is what stops the Case being offered on every
    tick for the rest of its life -- a Case that is not closed has nothing to be
    marked as processed for.
    """
    logger.info("Case Distil: %s is %r again, withdrawing its Verdict", case_id, status)
    session.query(EpisodicDistilMarker).filter_by(
        investigation_kind=InvestigationKind.CASE.value, investigation_id=case_id
    ).delete()
    return {"verdicts": 0, "derived": 0, "withdrawn": 1}


def write_case_distil(session: Session, case_id: str) -> Dict[str, int]:
    """Map one closed Case to a Verdict and write it, marker included.

    Everything here is one transaction the caller owns. A failure raises with
    nothing written and no marker, so the Case comes back next tick rather than
    being recorded as done.
    """
    kind = InvestigationKind.CASE
    # First, as the hunt writer does and for the same reason, the withdrawal
    # included: a Case that is no longer failing must not keep a row saying it is.
    clear_failure(session, FailureKey(kind, case_id))

    case = session.get(Case, case_id)
    if case is None:
        raise CaseDistilRefused(f"{case_id} is not a case")

    # Before anything else, and unconditionally: a Case re-closed under a
    # different category -- or re-categorised to duplicate, which writes nothing
    # -- must not leave the earlier Verdict standing beside the new one.
    clear_investigation(session, kind, case_id)

    if (case.status or "").strip() != "closed":
        return _withdraw(session, case_id, case.status)

    closed = _accept(session, case)

    category = closed.category
    outcome = _OUTCOMES.get(category) if category is not None else None
    if outcome is None:
        if category is not ClosureCategory.DUPLICATE:
            logger.warning(
                "Case Distil: %s closed as %r, which this mapping has no outcome for",
                case_id,
                closed.closure.closure_category if closed.closure else None,
            )
        counts = {"verdicts": 0}
    else:
        first, last, window_source = _window(session, closed)
        verdict = EpisodicVerdict(
            investigation_kind=kind.value,
            investigation_id=case_id,
            # A Case is its own Hypothesis, so the two ids are the same string.
            hypothesis_id=case_id,
            statement=bounded_prose(closed.case.title or ""),
            outcome=outcome.value,
            rationale=bounded_prose(_rationale(closed.closure)),
            subject_entities=_subjects(session, case_id),
            # A Case's evidence is its Findings, which are our own telemetry,
            # and a Case with none stands on an analyst's assertion. Neither is
            # adversary-authored, which is the thing this flag exists to mark.
            attacker_influenceable_only=False,
            trust=closed.trust.value,
            first_seen=first,
            last_seen=last,
            window_source=window_source.value,
            concluded_at=closed.concluded_at,
        )
        session.add(verdict)
        # Flushed for the id its sources reference: a source row cannot name a
        # Verdict that has no id yet.
        session.flush()
        for source in _sources(session, case_id):
            session.add(EpisodicVerdictSource(verdict_id=verdict.id, **source))
        counts = {"verdicts": 1}

    write_marker(
        session,
        kind=kind,
        investigation_id=case_id,
        version=CASE_DISTIL_MAPPING_VERSION,
        counts=counts,
        concluded_at=closed.concluded_at,
    )
    return counts | {"derived": 1, "withdrawn": 0}


def pending(session: Session, limit: int = DEFAULT_BATCH) -> List[str]:
    """Cases whose episodic rows disagree with their state, newest close first.

    Both directions: closed Cases with no Verdict, a stale one, or one derived
    at another version, and reopened Cases still holding one to withdraw.
    """
    rows = session.execute(
        _CANDIDATES, {"version": CASE_DISTIL_MAPPING_VERSION, "limit": limit}
    ).all()
    return [str(row.case_id) for row in rows]


async def case_distil_once(limit: int = DEFAULT_BATCH) -> Dict[str, int]:
    """One pass: find closed Cases, write each one's Verdict.

    One transaction per Case, so a Case this job refuses costs that Case and not
    the batch. How a failure is retried, counted and reported is the hunt
    Distil's ``write_with_retry``: a Distil that goes quiet leaves a store that
    reads as healthy and a memory that reads as empty, and empty is also what an
    entity nobody has looked at reads as.
    """
    candidates = await asyncio.to_thread(_pending_in_own_session, limit)
    written = {
        "cases": 0,
        "verdicts": 0,
        "withdrawn": 0,
        "refused": 0,
        "failed": 0,
    }

    for case_id in candidates:
        counts = await write_with_retry(
            FailureKey(InvestigationKind.CASE, case_id),
            lambda: _write_in_own_session(case_id),
            CaseDistilRefused,
            written,
            version=CASE_DISTIL_MAPPING_VERSION,
        )
        if counts is None:
            continue
        written["cases"] += counts["derived"]
        written["verdicts"] += counts["verdicts"]
        written["withdrawn"] += counts["withdrawn"]

    return written


def _pending_in_own_session(limit: int) -> List[str]:
    with unit_of_work() as session:
        return pending(session, limit)


def _write_in_own_session(case_id: str) -> Dict[str, int]:
    with unit_of_work() as session:
        return write_case_distil(session, case_id)


__all__: Sequence[str] = (
    "CASE_DISTIL_MAPPING_VERSION",
    "CaseDistilRefused",
    "ClosedCase",
    "case_distil_once",
    "pending",
    "write_case_distil",
)
