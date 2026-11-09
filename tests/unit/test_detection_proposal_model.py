"""A detection proposal's states and its append-only step record (#4157): the moves
TRANSITIONS allows, each step tried appending one event (failed ones too, with the
state unchanged), and a record no role can rewrite."""

import os
import uuid
from urllib.parse import urlparse

import pytest

from core.storage.models.detection_proposal import (
    TRANSITIONS,
    DetectionProposal,
    DetectionProposalEvent,
    InvalidProposalMove,
    ProposalState,
)

S = ProposalState
SIGMA = "title: t\ndetection:\n  sel:\n    Image|endswith: '\\\\rundll32.exe'\n  condition: sel\n"


def _draft(**kw):
    return DetectionProposal.draft(
        title="Excel spawns rundll32",
        sigma=SIGMA,
        actor="gray",
        actor_kind="person",
        evidence={"step": "draft", "source": "pasted by a person"},
        **kw,
    )


class _Events:
    """The write-only collection, as a list: what was appended, in order."""

    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


@pytest.fixture
def plain(monkeypatch):
    """A proposal not bound to a session, its events kept in a list."""
    events = _Events()
    monkeypatch.setattr(DetectionProposal, "events", property(lambda self: events))
    return _draft(), events


# --- the state machine ----------------------------------------------------------


def test_the_seven_steps_run_in_order_and_live_and_rejected_end_it():
    path = [S.DRAFT, S.LINTED, S.REPLAYED, S.CONVERTED, S.SHADOW, S.LIVE]
    for here, there in zip(path, path[1:]):
        assert there in TRANSITIONS[here]
    assert TRANSITIONS[S.LIVE] == frozenset()
    assert TRANSITIONS[S.REJECTED] == frozenset()
    # Every state short of live can go back to draft or be rejected.
    for state in path[1:-1]:
        assert {S.DRAFT, S.REJECTED} <= TRANSITIONS[state]


def test_a_step_cannot_be_skipped(plain):
    proposal, events = plain
    with pytest.raises(
        InvalidProposalMove, match="draft proposal cannot become shadow"
    ):
        proposal.move_to(
            S.SHADOW, actor="gray", actor_kind="person", evidence={"step": "shadow"}
        )
    assert proposal.state is S.DRAFT and len(events.rows) == 1


def test_each_move_appends_one_event_with_the_rule_as_it_stood(plain):
    proposal, events = plain
    proposal.move_to(
        S.LINTED, actor="gray", actor_kind="person", evidence={"step": "lint"}
    )
    first, lint = events.rows
    assert (first.from_state, first.to_state) == (None, S.DRAFT)
    assert (lint.from_state, lint.to_state) == (S.DRAFT, S.LINTED)
    assert lint.sigma == SIGMA and lint.actor == "gray" and lint.actor_kind == "person"


def test_a_failed_step_is_recorded_and_leaves_the_state(plain):
    proposal, events = plain
    proposal.record(
        actor="gray",
        actor_kind="person",
        evidence={"step": "lint", "passed": False, "findings": ["a literal host"]},
        reason="lint found a literal host",
    )
    failed = events.rows[-1]
    assert proposal.state is S.DRAFT
    assert (failed.from_state, failed.to_state) == (S.DRAFT, S.DRAFT)
    assert failed.evidence["passed"] is False


def test_new_text_only_on_the_way_back_to_draft(plain):
    proposal, events = plain
    proposal.move_to(
        S.LINTED, actor="gray", actor_kind="person", evidence={"step": "lint"}
    )
    with pytest.raises(ValueError, match="only on the way back to draft"):
        proposal.move_to(
            S.REPLAYED,
            actor="gray",
            actor_kind="person",
            evidence={"step": "replay"},
            sigma="title: other",
        )
    proposal.move_to(
        S.DRAFT,
        actor="gray",
        actor_kind="person",
        evidence={"step": "edit"},
        sigma="title: other",
    )
    assert proposal.sigma == "title: other" and events.rows[-1].sigma == "title: other"


@pytest.mark.parametrize("kind", ["", "token", "daemon"])
def test_an_actor_is_a_person_an_agent_or_vigil(plain, kind):
    proposal, _ = plain
    with pytest.raises(ValueError, match="actor_kind"):
        proposal.record(actor="x", actor_kind=kind, evidence={"step": "lint"})


def test_every_step_records_its_evidence(plain):
    proposal, _ = plain
    with pytest.raises(ValueError, match="evidence"):
        proposal.record(actor="gray", actor_kind="person", evidence={})


# --- real Postgres: a database upgraded to 0040 ---------------------------------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")


@pytest.fixture
def pg(monkeypatch):
    """The database the DSN names, upgraded to head (export POSTGRES_* to match).
    The proposals made here stay: the record can't be deleted, by design."""
    if not DSN:
        pytest.skip("VIGIL_TEST_PG_DSN not set")
    from core.storage import connection

    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)
    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    from sqlalchemy import text

    with manager.session_scope() as session:
        if (
            session.execute(
                text("select to_regclass('detection_proposal_events')")
            ).scalar()
            is None
        ):
            pytest.skip("the database is not upgraded to 0040_detection_proposals")
        assert session.execute(
            text("select current_database()")
        ).scalar() == url.path.lstrip("/")
    return manager


def _saved(manager, case_id=None):
    with manager.session_scope() as session:
        proposal = _draft(case_id=case_id)
        proposal.title = f"model test {uuid.uuid4()}"
        session.add(proposal)
        session.flush()
        proposal.move_to(
            S.LINTED, actor="gray", actor_kind="person", evidence={"step": "lint"}
        )
        proposal.record(
            actor="agent-run-1",
            actor_kind="agent",
            evidence={"step": "replay", "proven": False},
            reason="matched rows no case claims",
        )
        return proposal.proposal_id


def _events(manager, pid):
    from sqlalchemy import select

    with manager.session_scope() as session:
        rows = session.scalars(
            select(DetectionProposalEvent)
            .where(DetectionProposalEvent.proposal_id == pid)
            .order_by(DetectionProposalEvent.id)
        ).all()
        return [
            (r.from_state, r.to_state, r.actor_kind, r.evidence["step"], r.reason)
            for r in rows
        ]


def test_the_record_lands_in_order_with_who_and_why(pg):
    pid = _saved(pg)
    assert _events(pg, pid) == [
        (None, S.DRAFT, "person", "draft", ""),
        (S.DRAFT, S.LINTED, "person", "lint", ""),
        (S.LINTED, S.LINTED, "agent", "replay", "matched rows no case claims"),
    ]
    with pg.session_scope() as session:
        assert session.get(DetectionProposal, pid).state is S.LINTED


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE detection_proposal_events SET reason = 'rewritten' WHERE proposal_id = :p",
        "DELETE FROM detection_proposal_events WHERE proposal_id = :p",
    ],
)
def test_no_role_can_rewrite_or_delete_the_record(pg, statement):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    pid = _saved(pg)
    with pytest.raises(DBAPIError, match="append-only"):
        with pg.session_scope() as session:
            session.execute(text(statement), {"p": pid})
    assert len(_events(pg, pid)) == 3


@pytest.mark.parametrize(
    "from_state, to_state, evidence, actor, kind, constraint",
    [
        (None, "linted", '{"step": "x"}', "gray", "person", "created_as_draft"),
        ("draft", "draft", "{}", "gray", "person", "evidence_check"),
        ("draft", "draft", '{"step": "x"}', "", "person", "actor_check"),
        ("draft", "draft", '{"step": "x"}', "gray", "token", "actor_kind_check"),
    ],
)
def test_the_database_refuses_a_malformed_step(
    pg, from_state, to_state, evidence, actor, kind, constraint
):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    pid = _saved(pg)
    with pytest.raises(IntegrityError, match=constraint):
        with pg.session_scope() as session:
            session.execute(
                text(
                    "INSERT INTO detection_proposal_events (proposal_id, from_state,"
                    " to_state, evidence, actor, actor_kind, sigma) VALUES (:p, :f, :t,"
                    " CAST(:e AS jsonb), :a, :k, 's')"
                ),
                {
                    "p": pid,
                    "f": from_state,
                    "t": to_state,
                    "e": evidence,
                    "a": actor,
                    "k": kind,
                },
            )
