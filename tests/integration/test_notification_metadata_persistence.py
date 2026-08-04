"""End-to-end proof for issue #559: notification metadata survives the commit.

The unit tests in ``tests/unit/test_notification_metadata.py`` assert the
*mechanism* — the payload lands on the mapped attribute rather than a shadowing
instance attribute — and need no database. This asserts the *symptom* is gone:
create a notification through the service, commit, re-read in a **fresh session**,
and the metadata is still there.

Before the fix this stored ``NULL`` for every notification the service created,
with no error at any layer.

Requires Postgres, because the column is ``JSONB``.
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
    is reachable.
    """
    from database.connection import init_database

    try:
        init_database()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not available for integration test: {exc}")


@pytest.fixture
def user_id():
    """A unique id so parallel or repeated runs cannot collide."""
    return f"user-559-{uuid.uuid4().hex[:12]}"


def _fresh_session():
    """A session with its own identity map, so the re-read cannot be served from
    the writing session's cache — otherwise the test could pass on a cached
    object without the value ever reaching Postgres."""
    from database.connection import get_db_session

    return get_db_session()


@pytest.fixture
def cleanup(user_id):
    """Remove any notifications this test created, whatever the outcome."""
    yield
    from database.models import CaseNotification

    s = _fresh_session()
    try:
        rows = (
            s.query(CaseNotification).filter(CaseNotification.user_id == user_id).all()
        )
        for row in rows:
            s.delete(row)
        s.commit()
    finally:
        s.close()


def test_metadata_round_trips_through_a_fresh_session(user_id, cleanup):
    """The symptom from #559: committed cleanly, stored nothing."""
    from database.models import CaseNotification
    from services.case_notification_service import CaseNotificationService

    payload = {"threshold_percent": 90, "sla_type": "response"}

    created = CaseNotificationService().create_notification(
        user_id=user_id,
        notification_type="sla_warning",
        title="SLA Warning: 90%",
        message="a case has reached 90% of its response SLA",
        metadata=payload,
    )
    assert created is not None, "create_notification returned None"

    s = _fresh_session()
    try:
        reread = (
            s.query(CaseNotification)
            .filter(CaseNotification.user_id == user_id)
            .first()
        )
        assert reread is not None, "notification row was never written"
        assert reread.notification_metadata == payload, (
            "notification metadata was not persisted — the kwarg is landing on "
            "the declarative MetaData again (#559), got "
            f"{reread.notification_metadata!r}"
        )
    finally:
        s.close()


def test_metadata_is_exposed_as_the_metadata_key(user_id, cleanup):
    """``to_dict()`` maps the column back to the ``metadata`` JSON key.

    Pins the API shape: the fix changes the value from always-null to the real
    payload, and must not change the key.
    """
    from database.models import CaseNotification
    from services.case_notification_service import CaseNotificationService

    CaseNotificationService().create_notification(
        user_id=user_id,
        notification_type="case_assigned",
        title="Case Assigned",
        message="you have been assigned a case",
        metadata={"assigned_by": "lead-1"},
    )

    s = _fresh_session()
    try:
        reread = (
            s.query(CaseNotification)
            .filter(CaseNotification.user_id == user_id)
            .first()
        )
        as_dict = reread.to_dict()
        assert "notification_metadata" not in as_dict
        assert as_dict["metadata"] == {"assigned_by": "lead-1"}
    finally:
        s.close()
