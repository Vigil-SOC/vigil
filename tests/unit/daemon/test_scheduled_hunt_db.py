"""Which intake rows set the scheduled hunt's clock, against the throwaway Postgres (#2325)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.time import utcnow
from services.daemon.orchestrator import insert_intake_trigger
from services.daemon.scheduler import _last_scheduled_hunt_at

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture(autouse=True)
def _empty_intake():
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    with get_db_manager().session_scope() as session:
        session.query(IntakeTrigger).delete()
    yield


def _row(trigger_type, age=timedelta(0)):
    trigger_id = insert_intake_trigger(
        kind="schedule",
        priority="low",
        payload={"workflow_id": "threat-hunt", "trigger_type": trigger_type},
    )
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    with get_db_manager().session_scope() as session:
        session.get(IntakeTrigger, trigger_id).created_at = utcnow() - age


def test_no_history_means_no_last_hunt():
    assert _last_scheduled_hunt_at() is None


# The intel poller writes kind="schedule" too; its rows must not hold back the hunt.
def test_an_intel_row_is_not_a_scheduled_hunt():
    _row("intel")

    assert _last_scheduled_hunt_at() is None


def test_the_newest_scheduled_row_sets_the_clock():
    _row("scheduled", age=timedelta(days=3))
    _row("scheduled", age=timedelta(hours=2))
    _row("intel")

    last = _last_scheduled_hunt_at()
    assert timedelta(hours=1) < utcnow() - last < timedelta(hours=3)
