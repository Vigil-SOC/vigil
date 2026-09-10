"""The hunt Distil's failure join, against a database that has a ledger (#734).

This poll cannot be tested in ``tests/unit``. It reads ``agent_events``, which
Python does not model (ADR 0001), and the throwaway database DB-backed unit
tests run against is built by ``create_all`` -- so the table this joins from does
not exist there, and neither does the hunt Distil's only unit coverage.

The predicate it exists for is not incidental. A refusal parks at
``RETRY_NEVER``; if the failure join matched a run without regard to which
terminal it failed on, a run refused once would be invisible for the rest of its
life, and the later terminal that answers the refusal could never be read. That
is a silent, permanent hole in memory, and the only place it is visible is here.

Rows are inserted rather than run through the harness: what is asserted is one
SQL predicate over the ledger, and a real hunt would prove the fold instead.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from core.memory.distil import DISTIL_MAPPING_VERSION, pending

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database,
    pytest.mark.external_service,
]

REPO_ROOT = Path(__file__).resolve().parents[2]

# The ledger this polls, and the episodic tables it joins. Python models the
# episodic half and not the ledger, so both are applied rather than assumed.
DDL = (
    "19_agent_ledger.sql",
    "31_agent_ledger_hash_chain.sql",
    "26_episodic_memory.sql",
    "29_episodic_distil_failures.sql",
)


def _database_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql://vigil:vigil@localhost:5432/vigil_test"
    )


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(_database_url(), future=True)
    with engine.connect() as conn:
        for name in DDL:
            conn.execute(
                text((REPO_ROOT / "infra" / "database" / "init" / name).read_text())
            )
        conn.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def run(engine):
    """One run id, and everything written under it removed afterwards."""
    run_id = uuid.uuid4()
    yield run_id
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM episodic_distil_failures WHERE failure_key = :k"),
            {"k": str(run_id)},
        )
        conn.execute(
            text("DELETE FROM agent_events WHERE run_id = :r"), {"r": str(run_id)}
        )
        conn.commit()


def terminal(conn, run_id, seq):
    conn.execute(
        text(
            "INSERT INTO agent_events (run_id, seq, run_kind, kind, payload, schema_version)"
            " VALUES (:r, :s, 'hunt', 'terminal', '{}'::jsonb, 1)"
        ),
        {"r": str(run_id), "s": seq},
    )


def refused_at(conn, run_id, seq, version=DISTIL_MAPPING_VERSION):
    """A refusal, which is the failure that waits forever and so hides the most."""
    conn.execute(
        text("""INSERT INTO episodic_distil_failures
               (investigation_kind, failure_key, origin_seq, reason, attempts,
                last_error, first_failed_at, last_failed_at, next_attempt_at,
                distil_version)
               VALUES ('hunt', :k, :s, 'refused', 1, 'no investigation id',
                       now(), now(), '9999-12-31'::timestamptz, :v)"""),
        {"k": str(run_id), "s": seq, "v": version},
    )


def offered(engine):
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        return {(str(t.run_id), t.seq) for t in pending(session, limit=200)}


class TestARefusalHidesOnlyWhatItSaw:
    def test_a_run_refused_at_its_only_terminal_is_not_offered(self, engine, run):
        with engine.connect() as conn:
            terminal(conn, run, 1)
            refused_at(conn, run, 1)
            conn.commit()

        assert (str(run), 1) not in offered(engine)

    def test_a_later_terminal_is_offered_despite_the_refusal(self, engine, run):
        """The hole this clause closes: a run that concluded again is readable.

        A refusal waits forever, so without the seq comparison this run is gone
        for good -- and it is gone silently, which is the failure mode the whole
        table exists to end.
        """
        with engine.connect() as conn:
            terminal(conn, run, 1)
            terminal(conn, run, 2)
            refused_at(conn, run, 1)
            conn.commit()

        assert (str(run), 2) in offered(engine)

    def test_a_refusal_at_the_latest_terminal_still_hides_the_run(self, engine, run):
        with engine.connect() as conn:
            terminal(conn, run, 1)
            terminal(conn, run, 2)
            refused_at(conn, run, 2)
            conn.commit()

        assert not {t for t in offered(engine) if t[0] == str(run)}

    def test_a_bump_of_the_mapping_re_offers_a_refused_run(self, engine, run):
        with engine.connect() as conn:
            terminal(conn, run, 1)
            refused_at(conn, run, 1, version=DISTIL_MAPPING_VERSION + 1)
            conn.commit()

        assert (str(run), 1) in offered(engine)
