"""End-to-end proof for issue #559: notification metadata survives the commit.

The unit tests in ``tests/unit/test_notification_metadata.py`` assert the
*mechanism* — the model refuses the shadowing kwarg, and the payload lands on the
mapped attribute — and need no database. This asserts the *symptom* is gone:
create a notification through the service, commit, re-read in a **fresh session**,
and the metadata is still there.

Before the fix this stored ``NULL`` for every notification the service created,
with no error at any layer.

Requires Postgres, because the column is ``JSONB``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest.fixture(autouse=True)
def _database():
    """Skip when Postgres is unreachable; fail on anything else.

    Function-scoped so it runs *after* the autouse settings reset in
    ``tests/conftest.py`` — the connection this probes is then the same one the
    code under test will open, rather than one resolved from a developer's
    ``.env`` at collection time.

    The skip is deliberately narrow. A broad ``except`` around schema setup would
    turn a real breakage into a green skip, and this file is the only end-to-end
    evidence for #559. CI provisions the schema as its own job step, so anything
    failing past the probe is a genuine failure.
    """
    from core.storage.connection import get_db_manager, init_database

    manager = get_db_manager()
    try:
        manager.initialize()
        session = manager.get_session()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            "requires a local PostgreSQL (docker compose up -d postgres): " f"{exc}"
        )

    init_database()


@pytest.fixture
def user_id():
    """A unique id so parallel or repeated runs cannot collide."""
    return f"user-559-{uuid.uuid4().hex[:12]}"


def _fresh_session():
    """A session with its own identity map, so the re-read cannot be served from
    the writing session's cache — otherwise the test could pass on a cached
    object without the value ever reaching Postgres."""
    from core.storage.connection import get_db_session

    return get_db_session()


@pytest.fixture
def cleanup(user_id):
    """Remove any notifications this test created, whatever the outcome."""
    yield
    from core.storage.models import CaseNotification

    session = _fresh_session()
    try:
        rows = (
            session.query(CaseNotification)
            .filter(CaseNotification.user_id == user_id)
            .all()
        )
        for row in rows:
            session.delete(row)
        session.commit()
    finally:
        session.close()


def test_metadata_round_trips_through_a_fresh_session(user_id, cleanup):
    """The symptom from #559: committed cleanly, stored nothing."""
    from core.cases.case_notification_service import CaseNotificationService
    from core.storage.models import CaseNotification

    payload = {"threshold_percent": 90, "sla_type": "response"}

    created = CaseNotificationService().create_notification(
        user_id=user_id,
        notification_type="sla_warning",
        title="SLA Warning: 90%",
        message="a case has reached 90% of its response SLA",
        metadata=payload,
    )
    assert created is not None, "create_notification returned None"

    session = _fresh_session()
    try:
        reread = (
            session.query(CaseNotification)
            .filter(CaseNotification.user_id == user_id)
            .first()
        )
        assert reread is not None, "notification row was never written"
        assert reread.notification_metadata == payload, (
            "notification metadata was not persisted — the payload is landing "
            "somewhere other than the column again (#559), got "
            f"{reread.notification_metadata!r}"
        )
    finally:
        session.close()


def test_metadata_is_serialized_under_the_metadata_key(user_id, cleanup):
    """``CaseNotificationSchema`` maps the column back to the ``metadata`` key.

    Pins the API shape from the real row: the fix changes the value from
    always-null to the real payload, and must not change the key.
    """
    from core.cases.case_notification_service import CaseNotificationService
    from core.storage.models import CaseNotification
    from core.storage.schemas import CaseNotificationSchema

    CaseNotificationService().create_notification(
        user_id=user_id,
        notification_type="comment_mention",
        title="Mentioned in Comment",
        message="analyst-2 mentioned you",
        metadata={"comment_author": "analyst-2"},
    )

    session = _fresh_session()
    try:
        reread = (
            session.query(CaseNotification)
            .filter(CaseNotification.user_id == user_id)
            .first()
        )
        payload = CaseNotificationSchema.dump(reread)
        assert "notification_metadata" not in payload
        assert payload["metadata"] == {"comment_author": "analyst-2"}
    finally:
        session.close()
