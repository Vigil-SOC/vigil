"""End-to-end proof for issue #543: an in-place timeline append survives commit.

The unit tests in ``tests/unit/test_jsonb_mutation_tracking.py`` assert the
*mechanism* (columns coerce to ``MutableList``, appends fire change events) and
need no database. This asserts the *symptom* is gone: append via the ORM, commit,
re-read in a **fresh session**, and the event is still there.

Requires Postgres, because the columns are `JSONB`.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest.fixture(scope="module", autouse=True)
def _database():
    """Initialise the DatabaseManager.

    CI runs `init_database()` as a separate job step before pytest; doing it here
    too keeps the file runnable standalone. `create_all` is idempotent, so this is
    safe when the schema already exists. Skips rather than fails when no Postgres
    is reachable, so the file does not break a machine without the stack up.
    """
    from database.connection import init_database

    try:
        init_database()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not available for integration test: {exc}")


@pytest.fixture
def case_id():
    """A unique id so parallel or repeated runs cannot collide."""
    return f"case-543-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def session():
    from database.connection import get_db_session

    s = get_db_session()
    try:
        yield s
    finally:
        s.close()


def _fresh_session():
    """A session with its own identity map, so a read cannot be served from the
    first session's cache — otherwise the test could pass on a stale object
    without the row ever having been written."""
    from database.connection import get_db_session

    return get_db_session()


@pytest.fixture
def a_case(session, case_id):
    from database.models import Case

    case = Case(case_id=case_id, title="issue #543 timeline persistence", timeline=[])
    session.add(case)
    session.commit()
    yield case
    # Clean up regardless of assertion outcome.
    s = _fresh_session()
    try:
        row = s.query(Case).filter(Case.case_id == case_id).first()
        if row is not None:
            s.delete(row)
            s.commit()
    finally:
        s.close()


def test_in_place_timeline_append_persists(session, a_case, case_id):
    """The exact pattern `case_workflow_service.py:403` and `:470` use.

    Before the fix this committed silently and the event was gone — no error, no
    warning, just a missing audit entry.
    """
    from database.models import Case

    a_case.timeline.append({"timestamp": "2026-08-03T00:00:00", "event": "escalated"})
    session.commit()

    s = _fresh_session()
    try:
        reread = s.query(Case).filter(Case.case_id == case_id).first()
        assert reread is not None, "case row vanished"
        events = [e.get("event") for e in (reread.timeline or [])]
        assert "escalated" in events, (
            "in-place timeline append was not persisted — the mutation-tracking "
            "fix for #543 has regressed"
        )
    finally:
        s.close()


def test_repeated_appends_all_persist(session, a_case, case_id):
    """Several appends in one transaction must all survive.

    Guards a subtler variant: tracking that fires only on the first mutation
    would pass the test above and still lose later events.
    """
    from database.models import Case

    for i in range(3):
        a_case.timeline.append(
            {"timestamp": f"2026-08-03T0{i}:00:00", "event": f"e{i}"}
        )
    session.commit()

    s = _fresh_session()
    try:
        reread = s.query(Case).filter(Case.case_id == case_id).first()
        events = [e.get("event") for e in (reread.timeline or [])]
        assert events == ["e0", "e1", "e2"], (
            f"expected all three in order, got {events}"
        )
    finally:
        s.close()
