"""The Distil (#731): a finished hunt becomes Sightings, Verdicts and Gaps.

Poll rather than push. Nothing tells this job that a run ended; it finds
terminals no marker covers itself, using the ledger's partial index. That buys
three things a trigger does not: no change to the agent layer, a lost signal
becomes a late write rather than a missing one, and backfill is the same code
path exercised on every tick. Nothing is waiting on it either — memory is read
at the *start* of the next run, so a write that lands an hour later is a write
that lands in time. Memory that waits to be told stops being written the moment
the teller is down.

It maps the harness's already-typed conclusions and never re-derives them. The
fold is TypeScript and a second implementation here would drift while only one
is gated, so a field this job needs and cannot find is a contract change to ask
for. What it reads is ``GET /runs/<id>/distil``.

Idempotency is delete-then-insert, scoped to the investigation, in one
transaction. Not ``ON CONFLICT DO UPDATE``: upserting fixes rows that still
exist and does nothing about rows that should not, so bumping the version on
logic that now yields six Sightings where it yielded eight would leave two stale
rows indistinguishable from real ones. The marker goes in the same transaction —
split them and a crash between either double-writes the investigation or marks
it done when it is not.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.agents.projections import read_distil
from core.memory.entity_keys import entity_key, entity_keys
from core.memory.recall_contract import (
    VERDICT_SUBJECT_CAP,
    DistilFailureReason,
    GapDisposition,
    InvestigationKind,
    Stance,
    Trust,
    VerdictOutcome,
    WindowSource,
)
from core.memory.source_tier import InvestigationKind as TierKind
from core.memory.source_tier import SourceTier, resolve_source_tier
from core.storage.models import (
    EpisodicDistilFailure,
    EpisodicDistilMarker,
    EpisodicGap,
    EpisodicSighting,
    EpisodicVerdict,
    EpisodicVerdictSource,
)
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# This side's version: bumped when the mapping below changes what it writes.
# Re-deriving is delete-then-insert, so a bump that now yields fewer rows leaves
# none behind. Stamped on the marker, and the poll re-offers everything stamped
# with any other value.
DISTIL_MAPPING_VERSION = 2

# The other side's version: the wire schema of the payload this understands,
# which `DISTIL_SCHEMA_VERSION` in services/agent/workflows/hunt/distil.ts
# stamps. A newer one is refused rather than mis-mapped -- a field that changed
# meaning is worse read optimistically than not read at all, and the marker's
# absence brings the investigation back next tick.
SUPPORTED_SCHEMA_VERSION = 2

# Only hunts are distilled today. A Case closure writes its own Verdict from the
# Case, not from a ledger, and no other run kind concludes anything.
DISTILLED_RUN_KINDS: Tuple[str, ...] = ("hunt",)

DEFAULT_BATCH = 25

# One retry, and then it is reported. A write that fails twice is a store that is
# down or a payload that is wrong, and neither gets better by being hammered
# inside a poll tick that will come round again anyway.
ATTEMPTS = 2

# How long a failed subject waits before the poll offers it again, indexed by how
# many ticks it has now failed on. Widening rather than fixed: a store down for a
# minute should not cost an hour, and one down for a day should not be asked a
# thousand times about it. The last entry is the ceiling, and is where a subject
# that will never write settles.
#
# There is no attempt count that gives up. A count cannot tell a store that will
# come back from one that will not, so a ceiling would have to choose between
# abandoning the first and hammering the second. The row is what makes either
# findable (#734); the interval is only what stops it being hammered.
RETRY_INTERVALS: Tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)

# A refusal's next attempt, which is to say never. What this job would not map
# will not become mappable by being read again -- the fix is on the other side of
# the contract -- so a refusal is the far end of the same column rather than a
# second rule, and the poll compares one thing. A version bump still re-offers
# it, because the failure join matches on version the way the marker join does.
RETRY_NEVER = datetime(9999, 12, 31, tzinfo=timezone.utc)

# A crash is not an outcome. Nothing an aborted run believed at the moment it
# died is a conclusion, so none of it becomes memory. Running out is different:
# a hunt that spent its budget or found no data still concluded what it
# concluded, and those write normally.
ABORTED = "aborted"
BUDGET_TERMINATED = "budget_terminated"

# Terminals no marker at this version covers. The join is on the covered-runs
# array and not on origin_run_id: one investigation can span more than one run,
# and agent_events carries no investigation id for the poll to key on, so a
# marker has to say which runs it accounts for. Matching the version in the join
# rather than the filter is what makes a bump re-offer every covered run instead
# of only the origin one.
#
# The failure join is the second half of the same idea (#734). A failure writes
# no marker, so without it a run that cannot be written comes back on every tick
# forever; joined here, the run is offered again when its interval is up and not
# before. Matched on version so that a bump re-offers everything that failed
# under the old mapping -- a mapping change is the fix for most of what fails.
#
# And matched on seq, for the reason the marker clause below exists: a hunt that
# resumed past its own terminal appends a second one to the same run, and a
# failure keyed on the run alone would speak for terminals it never saw. A
# refusal parks at RETRY_NEVER, so without this a run refused at seq N would be
# invisible for the rest of its life and the later terminal that answers the
# refusal could never be read. Comparing what the failure recorded is what makes
# the later conclusions re-derive -- the same comparison, against the same
# column, that the marker makes.
_CANDIDATES = text("""
    SELECT DISTINCT ON (e.run_id) e.run_id AS run_id, e.seq AS seq
    FROM agent_events e
    LEFT JOIN episodic_distil_markers m
           ON m.origin_run_ids @> ARRAY[e.run_id]
          AND m.distil_version = :version
    LEFT JOIN episodic_distil_failures f
           ON f.investigation_kind = 'hunt'
          AND f.failure_key = e.run_id::text
          AND f.distil_version = :version
          AND f.origin_seq >= e.seq
    WHERE e.kind = 'terminal'
      AND e.run_kind = ANY(:kinds)
      AND (f.failure_key IS NULL OR f.next_attempt_at <= now())
      AND (
        m.investigation_id IS NULL
        OR (m.origin_run_id = e.run_id AND m.origin_seq < e.seq)
      )
    ORDER BY e.run_id, e.seq DESC
    LIMIT :limit
    """)


@dataclass(frozen=True)
class Terminal:
    """One run's terminal event: what the poll finds and the writer works from."""

    run_id: uuid.UUID
    seq: int


@dataclass(frozen=True)
class Origin:
    """The terminal a marker's rows were derived from, and the runs it covers.

    Absent on a Case, which is closed rather than run — the marker's origin
    columns are null there, and the schema's CHECK ties that to the kind.
    """

    run_id: uuid.UUID
    seq: int
    covered: Sequence[uuid.UUID]


@dataclass(frozen=True)
class FailureKey:
    """What a failure is recorded against: what the poll held before it failed.

    Not the investigation, which is what a marker is keyed by and what a failure
    frequently never learns — an unreadable fold has only a run id, and a refused
    payload is often refused *because* it carries no investigation id. So a hunt
    is its terminal and a Case is its case id, and one table holds both.
    """

    kind: InvestigationKind
    value: str
    seq: Optional[int] = None

    def __post_init__(self) -> None:
        """A hunt names the terminal it failed on and nothing else does.

        Checked here rather than left to the schema's CHECK, because the only
        caller is inside a failure path that swallows what it cannot record: a
        hunt key built without its seq would be rejected by the database,
        logged, and dropped, and the run would come back unpaced — which is the
        behaviour this table exists to end, arriving silently.

        The seq is not a breadcrumb. The poll compares it to decide whether a
        later terminal supersedes the failure, so a hunt missing one is a run
        that could never be rescued by concluding again.
        """
        if self.kind is InvestigationKind.HUNT and self.seq is None:
            raise ValueError(
                "a hunt failure key must name the terminal it failed on, "
                "because the poll compares it against later terminals"
            )
        if self.kind is not InvestigationKind.HUNT and self.seq is not None:
            raise ValueError(
                f"a {self.kind.value} failure key cannot carry seq={self.seq!r}: "
                "only a hunt has a terminal to name"
            )

    @classmethod
    def of(cls, terminal: Terminal) -> "FailureKey":
        """The key one terminal is recorded under."""
        return cls(InvestigationKind.HUNT, str(terminal.run_id), terminal.seq)


@dataclass(frozen=True)
class Concluded:
    """A payload the writer has accepted, with what every row built from it needs.

    The investigation id and the conclusion time are read once, refused once if
    they are absent, and then travel together because no row can be built
    without all three.
    """

    payload: Mapping[str, Any]
    investigation_id: str
    concluded_at: datetime


class DistilRefused(RuntimeError):
    """A payload this job will not map. Raised rather than written around."""


def _utc(value: object) -> Optional[datetime]:
    """The UTC instant a harness timestamp names, or None if it names none.

    The columns are ``timestamptz``, so a value that arrived without an offset
    is read as UTC rather than as the server's local time — the harness stamps
    in UTC and a naive value here would silently shift by the deployment's zone.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _gap_disposition(status: str, run_outcome: str) -> GapDisposition:
    """Why nothing was gathered for this question.

    ``parked`` is the hunt saying it chose not to pursue this one. Everything
    else is the hunt not getting to it, which is budget when the run ran out of
    budget and plain absence otherwise.
    """
    if status == "parked":
        return GapDisposition.DEPRIORITISED
    if run_outcome == BUDGET_TERMINATED:
        return GapDisposition.BUDGET_EXHAUSTED
    return GapDisposition.NO_EVIDENCE_GATHERED


# Why a Gap exists, when the harness gave no reason of its own. Stated rather
# than left empty: an empty reason reads as a missing field, and these are the
# three known absences.
_GAP_REASONS: Mapping[GapDisposition, str] = {
    GapDisposition.DEPRIORITISED: "parked at the hunt's terminal and never resumed",
    GapDisposition.BUDGET_EXHAUSTED: "the hunt ran out of budget before gathering evidence for it",
    GapDisposition.NO_EVIDENCE_GATHERED: "the hunt ended with no evidence gathered for it",
}


# The hunt's subjects are declared by whoever put the claim up, so nothing upstream
# bounds them. ADR 0016's rule is memory's, and this is where it is applied on this
# side, as it is for a Case.
def _capped(keys: List[str], investigation_id: str, hypothesis_id: str) -> List[str]:
    if len(keys) > VERDICT_SUBJECT_CAP:
        logger.warning(
            "Distil: %s/%s names %s entities, keeping the first %s (ADR 0016)",
            investigation_id,
            hypothesis_id,
            len(keys),
            VERDICT_SUBJECT_CAP,
        )
    return keys[:VERDICT_SUBJECT_CAP]


def _outcome_of(status: str) -> Optional[VerdictOutcome]:
    """The Verdict a Hypothesis status becomes, or None when it becomes a Gap.

    ``active`` and ``inconclusive`` are the same case: a claim still standing at
    a terminal did not conclude, and whether that is a Verdict or a Gap is
    decided by whether anything was gathered, not by which of the two it is.
    ``false_positive`` is unreachable here — it arrives only from a Case closure.
    """
    if status in ("proven", "disproven", "handed_off"):
        return VerdictOutcome(status)
    if status in ("inconclusive", "active"):
        return VerdictOutcome.INCONCLUSIVE
    return None


def _sources_of(
    conclusion: Mapping[str, Any], investigation_id: str
) -> List[Dict[str, str]]:
    """Per-source Stance and stamped Source Tier.

    The tier is stamped here and never joined at read time: an integration
    removed or recategorised later must not retroactively change how a past
    Verdict was corroborated.

    The harness's own records are already gone — the fold drops them before a
    stance is taken, which is what makes ``not_evidence`` impossible on a Verdict
    rather than merely visible on one. What survives to be graded here is a real
    source the tier map regrades, and one that comes back ``not_evidence`` is
    written and logged rather than dropped: a silent drop is a Verdict that looks
    thinner than it is for no recorded reason.
    """
    rows: List[Dict[str, str]] = []
    seen = set()
    for source in conclusion.get("sources") or []:
        system = str(source.get("source_system", "")).strip()
        try:
            stance = Stance(str(source.get("stance", "")).strip())
        except ValueError:
            logger.warning(
                "Distil: %s cites %r with a stance this contract has no value for",
                investigation_id,
                system,
            )
            continue
        if not system or system in seen:
            continue
        seen.add(system)
        tier = resolve_source_tier(system, TierKind.HUNT)
        if tier is SourceTier.NOT_EVIDENCE:
            logger.warning(
                "Distil: %s cites %r on hypothesis %s, which is not evidence",
                investigation_id,
                system,
                conclusion.get("hypothesis_id"),
            )
        rows.append(
            {"source_system": system, "stance": stance.value, "source_tier": tier.value}
        )
    return rows


def _techniques_of(conclusion: Mapping[str, Any]) -> List[str]:
    """Distinct T-IDs the conclusion cited, in order; ``[]`` when it names none.

    A schema-1 payload carries no field at all, and that is read as none rather
    than refused: the column's empty default already says known-to-be-none.
    """
    seen: List[str] = []
    for technique in conclusion.get("techniques") or []:
        if isinstance(technique, str) and technique.strip() and technique not in seen:
            seen.append(technique)
    return seen


def _sighting_rows(concluded: Concluded) -> List[Dict[str, Any]]:
    payload, investigation_id = concluded.payload, concluded.investigation_id
    rows: List[Dict[str, Any]] = []
    for sighting in payload.get("sightings") or []:
        entity = sighting.get("entity") or {}
        key = entity_key(str(entity.get("type", "")), str(entity.get("value", "")))
        first = _utc(sighting.get("first_seen"))
        last = _utc(sighting.get("last_seen"))
        hits = int(sighting.get("hit_count") or 0)
        # A row missing any of these cannot be joined, ordered or counted, and
        # writing a guessed one puts an entity in history it was never in.
        if not key or first is None or last is None or hits <= 0:
            logger.warning(
                "Distil: %s dropped an unusable sighting %r", investigation_id, sighting
            )
            continue
        rows.append(
            {
                "entity_key": key,
                "investigation_kind": InvestigationKind.HUNT.value,
                "investigation_id": investigation_id,
                "source_system": str(sighting.get("source_system", "")),
                "hit_count": hits,
                "attacker_influenceable": bool(sighting.get("attacker_influenceable")),
                # Both ends inclusive, and ordered: a harness that stamped them
                # out of order would otherwise fail the window constraint and
                # take the whole investigation down with it.
                "first_seen": min(first, last),
                "last_seen": max(first, last),
                "concluded_at": concluded.concluded_at,
            }
        )
    return rows


def _conclusion_rows(
    concluded: Concluded,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split the hunt's conclusions into Verdict rows and Gap rows."""
    payload, investigation_id = concluded.payload, concluded.investigation_id
    run_outcome = str(payload.get("outcome") or "")
    verdicts: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []

    for conclusion in payload.get("conclusions") or []:
        hypothesis_id = str(conclusion.get("hypothesis_id", "")).strip()
        if not hypothesis_id:
            logger.warning(
                "Distil: %s has a conclusion with no hypothesis id", investigation_id
            )
            continue

        status = str(conclusion.get("status", ""))
        subjects = _capped(
            entity_keys(conclusion.get("subject_entities")),
            investigation_id,
            hypothesis_id,
        )
        statement = str(conclusion.get("statement", ""))
        rationale = str(conclusion.get("rationale", ""))
        outcome = _outcome_of(status)
        gathered = int(conclusion.get("evidence_count") or 0) > 0

        # A claim that concluded nothing and gathered nothing is a question the
        # hunt never answered, not a conclusion with an empty outcome. handed_off
        # is never one: it carries evidence, a window and sources, so neither
        # "never looked" nor "failed to conclude" is true of it.
        if outcome is None or (outcome is VerdictOutcome.INCONCLUSIVE and not gathered):
            disposition = _gap_disposition(status, run_outcome)
            gaps.append(
                {
                    "investigation_kind": InvestigationKind.HUNT.value,
                    "investigation_id": investigation_id,
                    "hypothesis_id": hypothesis_id,
                    "statement": statement,
                    "disposition": disposition.value,
                    "reason": rationale or _GAP_REASONS[disposition],
                    "subject_entities": subjects,
                    "concluded_at": concluded.concluded_at,
                }
            )
            continue

        first = _utc(conclusion.get("first_seen")) or concluded.concluded_at
        last = _utc(conclusion.get("last_seen")) or concluded.concluded_at
        verdicts.append(
            {
                "row": {
                    "investigation_kind": InvestigationKind.HUNT.value,
                    "investigation_id": investigation_id,
                    "hypothesis_id": hypothesis_id,
                    "statement": statement,
                    "outcome": outcome.value,
                    "rationale": rationale,
                    "subject_entities": subjects,
                    "techniques": _techniques_of(conclusion),
                    "attacker_influenceable_only": bool(
                        conclusion.get("attacker_influenceable_only")
                    ),
                    # A hunt is agent-concluded. `analyst` arrives only when a
                    # person closed the Case that authored the Verdict.
                    "trust": Trust.AGENT.value,
                    "first_seen": min(first, last),
                    "last_seen": max(first, last),
                    "window_source": (
                        WindowSource.OBSERVED.value
                        if conclusion.get("window_observed")
                        else WindowSource.ASSERTED.value
                    ),
                    "concluded_at": concluded.concluded_at,
                },
                "sources": _sources_of(conclusion, investigation_id),
            }
        )

    return verdicts, gaps


def clear_investigation(
    session: Session, kind: InvestigationKind, investigation_id: str
) -> None:
    """Everything this investigation wrote, scoped to it and to nothing else.

    Verdict sources go with their Verdicts through the foreign key, which is why
    they are not deleted here: deleting them separately would leave a window in
    which a Verdict has none.

    Shared with the Case Distil rather than written twice: re-deriving is
    delete-then-insert, and two implementations of the delete half is two
    definitions of what an investigation owns.
    """
    for model in (EpisodicSighting, EpisodicVerdict, EpisodicGap):
        session.execute(
            delete(model).where(
                model.investigation_kind == kind.value,
                model.investigation_id == investigation_id,
            )
        )


def _accept(terminal: Terminal, payload: Mapping[str, Any]) -> Concluded:
    """The payload, or a refusal saying which field it could not be read on."""
    version = payload.get("distil_schema_version")
    # An absent version is refused rather than read as 0: a payload that does not
    # say what it is could be any shape, and mapping it optimistically writes
    # rows nobody can trust more quietly than writing none.
    if not isinstance(version, int) or version > SUPPORTED_SCHEMA_VERSION:
        raise DistilRefused(
            f"{terminal.run_id} answered distil schema {version!r}; "
            f"this Distil understands {SUPPORTED_SCHEMA_VERSION}"
        )

    investigation_id = str(payload.get("investigation_id", "")).strip()
    if not investigation_id:
        raise DistilRefused(
            f"{terminal.run_id} answered a distil payload with no investigation id"
        )

    concluded_at = _utc(payload.get("concluded_at"))
    if concluded_at is None:
        raise DistilRefused(
            f"{terminal.run_id} has not concluded, so there is nothing to distil"
        )

    return Concluded(payload, investigation_id, concluded_at)


def _superseded_by(
    marker: Optional[EpisodicDistilMarker], concluded: Concluded, terminal: Terminal
) -> bool:
    """Whether this investigation already holds rows from a later terminal.

    One investigation can reach a terminal in more than one run, and the poll
    offers each run separately because it has nothing but run ids to go on. Two
    runs both re-deriving the same investigation would leave whichever ran last
    on the record, and they would take turns forever. The later terminal wins,
    and the earlier one is recorded as covered without touching the rows.
    """
    return (
        marker is not None
        and marker.distil_version == DISTIL_MAPPING_VERSION
        and marker.origin_run_id != terminal.run_id
        and marker.concluded_at >= concluded.concluded_at
    )


def record_failure(
    session: Session,
    *,
    key: FailureKey,
    reason: DistilFailureReason,
    error: str,
    version: int,
    now: Optional[datetime] = None,
) -> None:
    """Record that this subject could not be written, and when to try again.

    Its own transaction, always: the write this is recording has just rolled
    back, so anything added to that session goes down with it.

    The interval is read off ``RETRY_INTERVALS`` by the new attempt count and a
    refusal takes ``RETRY_NEVER``, which is the same column and not a special case. The
    count is the one that survives the tick — ``ATTEMPTS`` bounds the retry
    inside a tick and is not what this counts.

    The count is read and then written rather than incremented in the statement,
    which is safe only because the daemon is a singleton: its StatefulSet is
    pinned to one replica and deliberately does not expose the count, because
    its loops are not safe to share. A second Distil ticking concurrently would
    pin this at 1 and the interval would never widen.
    """
    at = now or datetime.now(timezone.utc)
    existing = session.get(EpisodicDistilFailure, (key.kind.value, key.value))
    attempts = (existing.attempts if existing is not None else 0) + 1

    if reason is DistilFailureReason.REFUSED:
        next_attempt = RETRY_NEVER
    else:
        next_attempt = at + RETRY_INTERVALS[min(attempts, len(RETRY_INTERVALS)) - 1]

    row: Dict[str, Any] = {
        "investigation_kind": key.kind.value,
        "failure_key": key.value,
        "origin_seq": key.seq,
        "reason": reason.value,
        "attempts": attempts,
        "last_error": error,
        "first_failed_at": existing.first_failed_at if existing is not None else at,
        "last_failed_at": at,
        "next_attempt_at": next_attempt,
        "distil_version": version,
    }
    session.execute(
        insert(EpisodicDistilFailure)
        .values(**row)
        .on_conflict_do_update(
            index_elements=["investigation_kind", "failure_key"],
            set_={
                key: row[key]
                for key in row
                if key not in ("investigation_kind", "failure_key", "first_failed_at")
            },
        )
    )
    # Expired rather than returned: the row read at the top is still in the
    # identity map and the upsert went round it, so a caller that reads it back
    # through this session would be handed the count from before the increment.
    if existing is not None:
        session.expire(existing)


def clear_failure(session: Session, key: FailureKey) -> None:
    """Forget that this subject ever failed, in the transaction that wrote it.

    In that transaction and not after it, so a write that lands and a row that
    says it did not cannot both be true. Not scoped to the version: what failed
    under an older mapping has now been written under this one, and a row left
    behind at another version is a subject reading as stuck when it is done.
    """
    session.execute(
        delete(EpisodicDistilFailure).where(
            EpisodicDistilFailure.investigation_kind == key.kind.value,
            EpisodicDistilFailure.failure_key == key.value,
        )
    )


def write_distil(
    session: Session, terminal: Terminal, payload: Mapping[str, Any]
) -> Dict[str, int]:
    """Map one payload to rows and write them, marker included.

    Everything here is one transaction the caller owns. A failure raises with
    nothing written and no marker, so the investigation comes back next tick
    rather than being recorded as done.
    """
    # First, so that every path that returns rather than raises clears it, and
    # harmless if this transaction then rolls back -- the delete goes with it,
    # which is the point of doing it here rather than after the write.
    clear_failure(session, FailureKey.of(terminal))

    concluded = _accept(terminal, payload)
    kind = InvestigationKind.HUNT

    marker = session.get(EpisodicDistilMarker, (kind.value, concluded.investigation_id))
    # origin_run_id included, so the covered set is what the poll needs on its own.
    covered = sorted(
        {*(marker.origin_run_ids if marker is not None else []), terminal.run_id}
    )

    if _superseded_by(marker, concluded, terminal):
        marker.origin_run_ids = covered  # type: ignore[union-attr]
        return {"sightings": 0, "verdicts": 0, "gaps": 0, "derived": 0}

    # A crash is not an outcome, so an aborted run contributes no memory — and
    # withdraws none either. What an earlier terminal of this investigation
    # concluded was concluded; a later run dying does not unsay it, so the clear
    # belongs to the re-derive and not to the abort. The abort still takes a
    # marker: a marker records that the investigation was processed, not that it
    # was remembered, and without one this run comes back on every tick forever.
    if str(payload.get("outcome") or "") == ABORTED:
        counts = {"sightings": 0, "verdicts": 0, "gaps": 0}
    else:
        clear_investigation(session, kind, concluded.investigation_id)

        sightings = _sighting_rows(concluded)
        verdicts, gaps = _conclusion_rows(concluded)

        session.bulk_insert_mappings(EpisodicSighting, sightings)
        session.bulk_insert_mappings(EpisodicGap, gaps)
        for verdict in verdicts:
            row = EpisodicVerdict(**verdict["row"])
            session.add(row)
            # Flushed for the id its sources reference. One statement per Verdict
            # rather than one for the batch, because a source row cannot name a
            # Verdict that has no id yet.
            session.flush()
            for source in verdict["sources"]:
                session.add(EpisodicVerdictSource(verdict_id=row.id, **source))

        counts = {
            "sightings": len(sightings),
            "verdicts": len(verdicts),
            "gaps": len(gaps),
        }

    write_marker(
        session,
        kind=kind,
        investigation_id=concluded.investigation_id,
        version=DISTIL_MAPPING_VERSION,
        counts=counts,
        concluded_at=concluded.concluded_at,
        origin=Origin(terminal.run_id, terminal.seq, covered),
    )
    return counts | {"derived": 1}


def write_marker(
    session: Session,
    *,
    kind: InvestigationKind,
    investigation_id: str,
    version: int,
    counts: Mapping[str, int],
    concluded_at: datetime,
    origin: Optional[Origin] = None,
) -> None:
    """Record that this investigation was processed, at this version.

    The one upsert in either Distil, and the only row it is right for: a marker
    is one row keyed by the investigation, so there is no set of stale rows to
    leave behind. Counts are set from what was just written, never incremented.

    Each kind versions its own mapping, so the version is passed rather than
    read from a module constant — a change to how a Case maps must not re-offer
    every hunt.
    """
    row: Dict[str, Any] = {
        "investigation_kind": kind.value,
        "investigation_id": investigation_id,
        "origin_run_id": origin.run_id if origin is not None else None,
        "origin_seq": origin.seq if origin is not None else None,
        "origin_run_ids": list(origin.covered) if origin is not None else [],
        "distil_version": version,
        "sightings_written": counts.get("sightings", 0),
        "verdicts_written": counts.get("verdicts", 0),
        "gaps_written": counts.get("gaps", 0),
        "concluded_at": concluded_at,
    }
    session.execute(
        insert(EpisodicDistilMarker)
        .values(**row)
        .on_conflict_do_update(
            index_elements=["investigation_kind", "investigation_id"],
            set_={
                key: row[key]
                for key in row
                if key not in ("investigation_kind", "investigation_id")
            }
            | {"distilled_at": text("now()")},
        )
    )


def pending(session: Session, limit: int = DEFAULT_BATCH) -> List[Terminal]:
    """Terminals no marker at this version covers, newest terminal per run."""
    rows = session.execute(
        _CANDIDATES,
        {
            "kinds": list(DISTILLED_RUN_KINDS),
            "version": DISTIL_MAPPING_VERSION,
            "limit": limit,
        },
    ).all()
    return [Terminal(uuid.UUID(str(row.run_id)), int(row.seq)) for row in rows]


async def distil_once(limit: int = DEFAULT_BATCH) -> Dict[str, int]:
    """One pass: find terminals, read each fold, write each investigation.

    One transaction per investigation, so a payload this job refuses costs that
    investigation and not the batch. Nothing here is swallowed: a refusal, an
    unreadable fold and a failed write are each counted and each logged at a
    level that says which, because a Distil that goes quiet leaves a store that
    reads as healthy and a memory that reads as empty — and empty is also what an
    entity nobody has looked at reads as. None of the three writes a marker,
    which is what brings the investigation back once the reason is fixed.
    """
    candidates = await asyncio.to_thread(_pending_in_own_session, limit)
    written = {
        "investigations": 0,
        "sightings": 0,
        "verdicts": 0,
        "gaps": 0,
        "refused": 0,
        "unreadable": 0,
        "failed": 0,
    }

    for terminal in candidates:
        key = FailureKey.of(terminal)
        payload = await read_distil(str(terminal.run_id))
        if payload is None:
            # Not terminal and not a skip-forever: the agent layer may simply be
            # unreachable, and this run is a candidate again once its interval is
            # up. Said out loud all the same, because an agent layer that is
            # never reachable is a memory that silently never fills.
            logger.warning("Distil: %s had no readable fold", terminal.run_id)
            await _note_failure(
                key,
                DistilFailureReason.UNREADABLE,
                "no readable fold",
                DISTIL_MAPPING_VERSION,
            )
            written["unreadable"] += 1
            continue

        counts = await write_with_retry(
            key,
            lambda: _write_in_own_session(terminal, payload),
            DistilRefused,
            written,
            version=DISTIL_MAPPING_VERSION,
        )
        if counts is None:
            continue

        written["investigations"] += counts["derived"]
        for name in ("sightings", "verdicts", "gaps"):
            written[name] += counts[name]

    return written


async def write_with_retry(
    key: FailureKey,
    write: Callable[[], Dict[str, int]],
    refusal: Type[Exception],
    written: Dict[str, int],
    *,
    version: int,
) -> Optional[Dict[str, int]]:
    """One investigation's write, off the loop and retried once. None if it did
    not land.

    A refusal is not retried: what this job would not map will not become
    mappable by being read again, and the fix is on the other side of the
    contract. Everything else gets one more go and is then reported with a
    traceback -- a write that fails twice is a store that is down or an input
    that is wrong, and neither improves for being hammered inside a tick that
    comes round again anyway.

    Neither outcome writes a marker. Both now write a failure row (#734): the
    marker's absence is what brings the subject back, and the failure row is
    what says the absence is a failure and not a subject nothing has reached
    yet. It is also what paces the return, so a subject that will never write
    stops arriving on every tick.

    Shared by both Distils rather than written twice, because "how a failed
    write is counted, reported and paced" is one decision and the daemon reads
    both sets of counters the same way.
    """
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(write)
        except refusal as exc:
            logger.warning("%s Distil refused %s: %s", key.kind.value, key.value, exc)
            await _note_failure(key, DistilFailureReason.REFUSED, repr(exc), version)
            written["refused"] += 1
            return None
        except Exception as exc:
            if attempt < ATTEMPTS:
                logger.warning(
                    "%s Distil failed on %s, attempt %s of %s; retrying",
                    key.kind.value,
                    key.value,
                    attempt,
                    ATTEMPTS,
                )
                continue
            logger.error(
                "%s Distil failed on %s after %s attempts, leaving neither rows "
                "nor marker",
                key.kind.value,
                key.value,
                ATTEMPTS,
                exc_info=True,
            )
            await _note_failure(key, DistilFailureReason.FAILED, repr(exc), version)
            written["failed"] += 1
            return None
    return None


async def _note_failure(
    key: FailureKey,
    reason: DistilFailureReason,
    error: str,
    version: int,
) -> None:
    """Record the failure, and do not fail the tick if that fails too.

    The store that just refused a write is the store this row goes to, so it can
    be down for both -- and the caller is already in a failure path, so raising
    here would cost the rest of the batch to report something about one subject.

    Said at ERROR rather than counted, because what is lost is the pacing and
    not the report: the subject comes back next tick as it did before #734, and
    the write's own ERROR is already saying it failed.
    """
    try:
        await asyncio.to_thread(_record_in_own_session, key, reason, error, version)
    except Exception:
        logger.error(
            "%s Distil could not record the failure on %s; it will be re-offered "
            "on the next tick unpaced",
            key.kind.value,
            key.value,
            exc_info=True,
        )


def _record_in_own_session(
    key: FailureKey, reason: DistilFailureReason, error: str, version: int
) -> None:
    with unit_of_work() as session:
        record_failure(
            session,
            key=key,
            reason=reason,
            error=error,
            version=version,
        )


def _pending_in_own_session(limit: int) -> List[Terminal]:
    with unit_of_work() as session:
        return pending(session, limit)


def _write_in_own_session(
    terminal: Terminal, payload: Mapping[str, Any]
) -> Dict[str, int]:
    with unit_of_work() as session:
        return write_distil(session, terminal, payload)


__all__: Sequence[str] = (
    "DISTIL_MAPPING_VERSION",
    "DistilRefused",
    "Origin",
    "FailureKey",
    "Terminal",
    "ATTEMPTS",
    "RETRY_INTERVALS",
    "DEFAULT_BATCH",
    "RETRY_NEVER",
    "clear_failure",
    "clear_investigation",
    "distil_once",
    "pending",
    "record_failure",
    "write_distil",
    "write_marker",
    "write_with_retry",
)
