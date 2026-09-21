"""What an intel row makes spoken for, against the throwaway Postgres (#1009).

The payload filter and the recheck window are the thing under test, so the rows
are inserted through `insert_intake_trigger` the way the producer inserts them
and the query is called for real -- a faked session would pass whatever it was
handed.
"""

from __future__ import annotations

import pytest

from core.memory.hunt_coverage import build_proposal
from core.time import utcnow
from services.daemon.orchestrator import insert_intake_trigger
from services.daemon.threat_feed_poller import (
    INTEL_RECHECK_AFTER,
    _intel_intake_state,
)

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

KEY = "ip:198.51.100.4"
OTHER_KEY = "domain:bad.example"


# The throwaway database outlives the test, and every assertion here is about
# what the whole table says, so the rows a neighbour left would answer for it.
@pytest.fixture(autouse=True)
def _empty_intake():
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    with get_db_manager().session_scope() as session:
        session.query(IntakeTrigger).delete()
    yield


def _intel_row(*keys, state="queued", age=None):
    body = build_proposal(list(keys), [])
    trigger_id = insert_intake_trigger(
        kind="schedule",
        priority="low",
        payload={
            "workflow_id": "threat-hunt",
            "trigger_type": "intel",
            "finding_ids": [],
            "hypothesis": body["hypothesis"],
            "hypothesis_subjects": body["hypothesis_subjects"],
        },
    )
    if state != "queued" or age is not None:
        _age(trigger_id, state=state, age=age)
    return trigger_id


def _age(trigger_id, *, state, age):
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    with get_db_manager().session_scope() as session:
        row = session.get(IntakeTrigger, trigger_id)
        row.state = state
        if age is not None:
            row.created_at = utcnow() - age


def test_a_queued_intel_row_speaks_for_its_keys():
    _intel_row(KEY, OTHER_KEY)

    assert _intel_intake_state().spoken_for >= {KEY, OTHER_KEY}


def test_a_queued_row_speaks_for_its_keys_however_old_it_is():
    _intel_row(KEY, age=INTEL_RECHECK_AFTER * 10)

    assert KEY in _intel_intake_state().spoken_for


def test_a_launched_row_still_speaks_inside_the_recheck_window():
    _intel_row(KEY, state="launched", age=INTEL_RECHECK_AFTER / 2)

    assert KEY in _intel_intake_state().spoken_for


def test_a_launched_row_past_the_window_is_due_a_fresh_look():
    _intel_row(KEY, state="launched", age=INTEL_RECHECK_AFTER * 2)

    assert KEY not in _intel_intake_state().spoken_for


def test_the_nightly_scheduled_hunt_speaks_for_nothing():
    insert_intake_trigger(
        kind="schedule",
        priority="low",
        payload={
            "workflow_id": "threat-hunt",
            "trigger_type": "scheduled",
            "finding_ids": [],
            "hypothesis": "Activity consistent with T1071 is present in the estate",
            "hypothesis_subjects": {"anything": [KEY]},
        },
    )

    assert _intel_intake_state().spoken_for == set()


def test_a_human_ask_carrying_the_same_key_speaks_for_nothing():
    insert_intake_trigger(
        kind="human_ask",
        priority="medium",
        payload={
            "workflow_id": "threat-hunt",
            "trigger_type": "intel",
            "hypothesis_subjects": {"anything": [KEY]},
        },
    )

    assert _intel_intake_state().spoken_for == set()


def test_a_queued_intel_row_holds_the_intake():
    _intel_row(KEY)

    assert _intel_intake_state().queued is True


def test_a_launched_intel_row_does_not_hold_the_intake():
    _intel_row(KEY, state="launched", age=INTEL_RECHECK_AFTER / 2)

    state = _intel_intake_state()
    assert state.queued is False
    assert KEY in state.spoken_for


def test_a_queued_nightly_hunt_does_not_hold_the_intel_intake():
    insert_intake_trigger(
        kind="schedule",
        priority="low",
        payload={
            "workflow_id": "threat-hunt",
            "trigger_type": "scheduled",
            "finding_ids": [],
            "hypothesis": "Activity consistent with T1071 is present in the estate",
        },
    )

    assert _intel_intake_state().queued is False
