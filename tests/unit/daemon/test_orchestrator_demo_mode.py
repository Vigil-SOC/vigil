"""Demo mode swaps what the console reads, never what the orchestrator launches.

A real finding queued from Overview is a Postgres row. With demo mode on, the
orchestrator still has to find it, or the intake row waits until it expires.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.storage.database_data_service import DatabaseDataService
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

REAL_ID = "f-20261012-6e68d5f660a5e61e"


def _connected(service: DatabaseDataService) -> MagicMock:
    db = MagicMock()
    db.get_finding.return_value = object()
    service._db_service = db
    service._db_connected = True
    return db


@pytest.fixture
def demo_on():
    with patch("core.storage.database_data_service.is_demo_mode", return_value=True):
        yield


def test_a_demo_mode_service_answers_from_the_demo_set(demo_on):
    demo = MagicMock()
    demo.get_finding.return_value = None
    service = DatabaseDataService(demo_data=demo)
    db = _connected(service)

    assert service.get_finding(REAL_ID) is None
    db.get_finding.assert_not_called()


def test_demo_false_reads_postgres_while_demo_mode_is_on(demo_on):
    service = DatabaseDataService(demo=False)
    db = _connected(service)

    with patch(
        "core.storage.database_data_service.FindingSchema.dump",
        return_value={"finding_id": REAL_ID},
    ):
        assert service.get_finding(REAL_ID) == {"finding_id": REAL_ID}
    db.get_finding.assert_called_once_with(REAL_ID)
    assert not service.is_demo_mode()


def test_the_orchestrator_hydrates_an_intake_finding_from_postgres(demo_on):
    orch = object.__new__(Orchestrator)
    orch._data_service = None
    orch._init_services()

    assert isinstance(orch._data_service, DatabaseDataService)
    assert not orch._data_service.is_demo_mode()

    db = _connected(orch._data_service)
    with patch(
        "core.storage.database_data_service.FindingSchema.dump",
        return_value={"finding_id": REAL_ID, "severity": None},
    ):
        finding = orch._hydrate_detection_finding({"finding_id": REAL_ID})
    assert finding is not None and finding["finding_id"] == REAL_ID
    db.get_finding.assert_called_once_with(REAL_ID)
