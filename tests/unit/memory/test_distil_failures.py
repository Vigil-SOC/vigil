"""A distil that keeps failing is findable (#734), against a real Postgres.

What is asserted here is not that a failure is logged -- #731 already made it
loud -- but that it leaves state. A marker's absence means "not yet reached";
without a row saying otherwise, a subject that will never write is
indistinguishable from one nothing has looked at, and it is re-offered on every
tick forever.

The poll is a SQL predicate over a left join with a timestamptz on one side, so
it is exercised against the database rather than against a fake that would agree
with whatever this module happened to do. The transient failure is driven through
``write_with_retry`` because that is the seam both Distils call and the only
honest way to stand in for a store that is down -- no arrangement of real rows
produces one.

**The Case poll is the one asserted here.** The hunt poll reads
``agent_events``, which has no ORM model, and the throwaway database these tests
run against is built by ``create_all`` -- so the table it joins from does not
exist here. It is covered instead in
``tests/integration/test_distil_poll_failures.py``, which applies the ledger DDL
itself. What is hunt-specific about a *failure* -- that it is keyed by a run and
names the terminal it failed on -- needs no ledger and is asserted below.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.memory.case_distil import (
    CASE_DISTIL_MAPPING_VERSION,
    CaseDistilRefused,
    case_distil_once,
)
from core.memory.case_distil import pending as case_pending
from core.memory.distil import (
    DISTIL_MAPPING_VERSION,
    RETRY_INTERVALS,
    RETRY_NEVER,
    FailureKey,
    clear_failure,
    record_failure,
    write_with_retry,
)
from core.memory.recall_contract import DistilFailureReason, InvestigationKind
from core.storage.connection import get_db_session
from core.storage.models import (
    Case,
    CaseClosureInfo,
    EpisodicDistilFailure,
    EpisodicDistilMarker,
    EpisodicVerdict,
    EpisodicVerdictSource,
)
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.database, pytest.mark.external_service]

# Naive, as these tables store it; the Distil is what attaches a zone.
CLOSED = datetime(2026, 8, 3, 17, 0, 0)


@pytest.fixture
def session():
    """A session on the throwaway database, emptied of what these tests write."""
    db = get_db_session()
    try:
        _empty(db)
        yield db
    finally:
        db.rollback()
        _empty(db)
        db.close()


def _empty(db):
    for model in (
        EpisodicVerdictSource,
        EpisodicVerdict,
        EpisodicDistilMarker,
        EpisodicDistilFailure,
        CaseClosureInfo,
        Case,
    ):
        db.query(model).delete()
    db.commit()


def closed_case(db, case_id="case-734"):
    db.add(
        Case(
            case_id=case_id,
            title="beaconing from 10.1.2.3",
            description="",
            status="closed",
            priority="high",
            created_at=datetime(2026, 8, 1, 9, 0, 0),
            updated_at=CLOSED,
        )
    )
    db.add(
        CaseClosureInfo(
            case_id=case_id,
            closure_category="false_positive",
            closed_by_kind="analyst",
            closed_by="nestor",
            closed_at=CLOSED,
        )
    )
    db.flush()
    return case_id


def failure(
    db,
    key,
    *,
    reason=DistilFailureReason.FAILED,
    error="connection refused",
    version=None,
    now=None,
):
    """A failure already on the record, as an earlier tick would have left it.

    Reads the row back the way anyone asking "what is stuck" would, rather than
    being handed it: ``record_failure`` writes and returns nothing.

    Stamped with the version of the Distil the key belongs to, because each
    poll joins on its own; the two only happen to agree when both are 1.
    """
    if version is None:
        version = (
            DISTIL_MAPPING_VERSION
            if key.kind is InvestigationKind.HUNT
            else CASE_DISTIL_MAPPING_VERSION
        )
    record_failure(db, key=key, reason=reason, error=error, version=version, now=now)
    db.flush()
    return db.get(EpisodicDistilFailure, (key.kind.value, key.value))


class TestAFailureIsOnTheRecord:
    """The distinction the issue exists for: failed is not unprocessed."""

    @pytest.mark.asyncio
    async def test_a_failed_write_leaves_a_row_saying_so_and_no_marker(self, session):
        key = FailureKey(InvestigationKind.CASE, "case-734")

        def boom():
            raise RuntimeError("the store is down")

        counts = await write_with_retry(
            key,
            boom,
            CaseDistilRefused,
            {"refused": 0, "failed": 0},
            version=CASE_DISTIL_MAPPING_VERSION,
        )
        session.commit()

        assert counts is None
        row = session.get(EpisodicDistilFailure, ("case", "case-734"))
        assert row is not None
        assert row.reason == "failed"
        assert row.attempts == 1
        assert "the store is down" in row.last_error
        # Still no marker: the subject has not been processed, and the failure
        # row is what says the absence is a failure rather than a queue.
        assert session.get(EpisodicDistilMarker, ("case", "case-734")) is None

    @pytest.mark.asyncio
    async def test_a_refusal_is_recorded_as_a_refusal(self, session):
        key = FailureKey(InvestigationKind.CASE, "case-734")

        def refuse():
            raise CaseDistilRefused("case-734 is not a case")

        await write_with_retry(
            key,
            refuse,
            CaseDistilRefused,
            {"refused": 0, "failed": 0},
            version=CASE_DISTIL_MAPPING_VERSION,
        )
        session.commit()

        row = session.get(EpisodicDistilFailure, ("case", "case-734"))
        assert row.reason == "refused"
        assert "is not a case" in row.last_error

    def test_a_hunt_failure_names_the_terminal_it_failed_on(self, session):
        run_id = uuid.uuid4()
        row = failure(session, FailureKey(InvestigationKind.HUNT, str(run_id), 7))
        session.commit()

        assert row.failure_key == str(run_id)
        # A Case has none, and the schema's CHECK ties which shape a row has to
        # its kind rather than leaving a reader to guess.
        assert row.origin_seq == 7

    def test_a_case_failure_carries_no_terminal(self, session):
        row = failure(session, FailureKey(InvestigationKind.CASE, "case-734"))
        session.commit()
        assert row.origin_seq is None


class TestTheIntervalWidens:
    """Retried, but not on every tick, and never given up on."""

    def test_each_further_failure_counts_and_never_waits_less(self, session):
        """Asserted as behaviour rather than against ``RETRY_INTERVALS``.

        Comparing the observed waits to the constant they were read from only
        proves the code can index its own list: a schedule typed in the wrong
        order agrees with itself and passes. What a caller needs is that the
        count rises, the wait never shrinks, and neither runs away -- all true
        of any schedule someone might reasonably substitute, and false of the
        mistakes worth catching.
        """
        key = FailureKey(InvestigationKind.CASE, "case-734")
        at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

        waits = []
        for tick in range(1, len(RETRY_INTERVALS) + 3):
            row = failure(session, key, now=at)
            assert row.attempts == tick
            waits.append(row.next_attempt_at - at)
        session.commit()

        assert waits[0] > timedelta(0)
        assert all(
            later >= earlier for earlier, later in zip(waits, waits[1:])
        ), f"a later failure waited less than an earlier one: {waits}"
        # It stops growing rather than running away, and it stops rather than
        # giving up: a count cannot tell a store that will come back from one
        # that will not, so the last wait repeats instead of becoming RETRY_NEVER.
        assert waits[-1] == waits[-2]
        assert max(waits) < timedelta(days=1)

    def test_the_first_failure_is_kept_and_the_last_error_is_replaced(self, session):
        key = FailureKey(InvestigationKind.CASE, "case-734")
        first = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        later = first + timedelta(hours=3)

        failure(session, key, now=first, error="connection refused")
        row = failure(session, key, now=later, error="deadlock detected")
        session.commit()

        assert row.first_failed_at == first
        assert row.last_failed_at == later
        assert row.last_error == "deadlock detected"

    def test_a_refusal_waits_out_of_all_proportion_to_a_transient_failure(
        self, session
    ):
        """The difference is a kind, not a longer interval on the same scale."""
        transient = failure(session, FailureKey(InvestigationKind.CASE, "case-a"))
        refused = failure(
            session,
            FailureKey(InvestigationKind.CASE, "case-b"),
            reason=DistilFailureReason.REFUSED,
        )
        session.commit()

        assert refused.next_attempt_at > transient.next_attempt_at + timedelta(days=365)
        assert refused.next_attempt_at == RETRY_NEVER


class TestThePollHonoursIt:
    """The point of the interval: a subject stops arriving on every tick."""

    def test_a_case_waiting_out_its_interval_is_not_offered(self, session):
        case_id = closed_case(session)
        assert case_pending(session) == [case_id]

        failure(session, FailureKey(InvestigationKind.CASE, case_id))
        session.commit()

        assert case_pending(session) == []

    def test_a_case_whose_interval_is_up_is_offered_again(self, session):
        case_id = closed_case(session)
        failure(
            session,
            FailureKey(InvestigationKind.CASE, case_id),
            now=datetime.now(timezone.utc) - timedelta(days=1),
        )
        session.commit()

        assert case_pending(session) == [case_id]

    def test_a_refused_case_is_not_offered_again_while_it_stands_unchanged(
        self, session
    ):
        case_id = closed_case(session)
        failure(
            session,
            FailureKey(InvestigationKind.CASE, case_id),
            reason=DistilFailureReason.REFUSED,
            now=datetime.now(timezone.utc),
        )
        session.commit()

        assert case_pending(session) == []

    def test_a_case_edited_after_it_failed_is_offered_again(self, session):
        """A Case's input is live rows, so a refusal is not final the way a
        hunt's is: the edit that fixes it must bring the Case back."""
        case_id = closed_case(session)
        failure(
            session,
            FailureKey(InvestigationKind.CASE, case_id),
            reason=DistilFailureReason.REFUSED,
            now=datetime.now(timezone.utc),
        )
        session.commit()
        assert case_pending(session) == []

        # What a re-close through the console does, and what a refusal recorded
        # against the older state must not outlive.
        case = session.get(Case, case_id)
        case.updated_at = utcnow() + timedelta(minutes=1)
        session.commit()

        assert case_pending(session) == [case_id]


class TestAVersionBumpReOffers:
    """A mapping change is the fix for most of what fails, refusals included."""

    def test_a_case_that_failed_under_another_mapping_is_offered(self, session):
        case_id = closed_case(session)
        failure(
            session,
            FailureKey(InvestigationKind.CASE, case_id),
            reason=DistilFailureReason.REFUSED,
            version=CASE_DISTIL_MAPPING_VERSION + 1,
        )
        session.commit()

        # Stamped at a version this poll is not running, so the join misses it
        # and the Case is offered as though it had never failed.
        assert case_pending(session) == [case_id]


class TestSuccessForgetsIt:
    """A row that outlives what it describes is a subject reading as stuck."""

    @pytest.mark.asyncio
    async def test_a_case_that_writes_leaves_no_failure_behind(self, session):
        case_id = closed_case(session)
        failure(
            session,
            FailureKey(InvestigationKind.CASE, case_id),
            now=datetime.now(timezone.utc) - timedelta(days=1),
        )
        session.commit()

        written = await case_distil_once()

        assert written["cases"] == 1
        session.expire_all()
        assert session.get(EpisodicDistilFailure, ("case", case_id)) is None
        assert session.get(EpisodicDistilMarker, ("case", case_id)) is not None

    @pytest.mark.asyncio
    async def test_a_success_clears_a_failure_from_an_older_mapping(self, session):
        """Not scoped to the version: what failed under an older mapping has now
        been written under this one, and a row left behind reads as stuck."""
        case_id = closed_case(session)
        failure(
            session,
            FailureKey(InvestigationKind.CASE, case_id),
            version=CASE_DISTIL_MAPPING_VERSION + 1,
        )
        session.commit()

        await case_distil_once()

        session.expire_all()
        assert session.get(EpisodicDistilFailure, ("case", case_id)) is None

    def test_clearing_a_subject_that_never_failed_is_not_an_error(self, session):
        clear_failure(session, FailureKey(InvestigationKind.CASE, "case-734"))
        session.commit()


class TestBothDistilsRecordTheSameWay:
    @pytest.mark.asyncio
    async def test_a_hunt_and_a_case_failure_land_in_one_table(self, session):
        run_id = uuid.uuid4()
        failure(session, FailureKey(InvestigationKind.HUNT, str(run_id), 1))
        failure(session, FailureKey(InvestigationKind.CASE, "case-734"))
        session.commit()

        rows = session.query(EpisodicDistilFailure).all()
        assert {row.investigation_kind for row in rows} == {"hunt", "case"}
        assert all(row.attempts == 1 for row in rows)

    def test_a_hunt_key_must_name_its_terminal(self):
        """The one shape the table rejects, refused before it can reach it.

        `_note_failure` swallows what it cannot record, so a subject the schema
        would reject is pacing lost silently -- which is the failure mode this
        table exists to end.
        """
        with pytest.raises(ValueError, match="must name the terminal"):
            FailureKey(InvestigationKind.HUNT, str(uuid.uuid4()))

    def test_a_case_key_must_not_name_one(self):
        with pytest.raises(ValueError, match="cannot carry seq"):
            FailureKey(InvestigationKind.CASE, "case-734", 1)
