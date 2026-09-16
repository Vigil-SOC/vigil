"""A finding that overlaps open work attaches to it instead of being dropped (#921).

``check_overlap`` names the live investigations sharing an entity. The finding
goes into the first one's case; when none has a case, onto its ``trigger_ids``.
Either way nothing new is opened.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

FINDING = {
    "finding_id": "f-2",
    "severity": "high",
    "entity_context": {"hostnames": ["FYODOR-L"]},
}


def _orchestrator(overlapping, records, data_service) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = overlapping
    orch.stats = {"dedup_prevented": 0}
    orch.get_investigation = MagicMock(side_effect=records.get)
    orch._append_trigger_id = MagicMock()
    orch._log_ai_decision = MagicMock()
    orch._create_investigation = AsyncMock()
    orch._data_service = data_service
    return orch


def _nothing_new_opened(orch, data_service):
    orch._create_investigation.assert_not_awaited()
    if data_service is not None:
        data_service.create_case.assert_not_called()
    assert orch.stats["dedup_prevented"] == 1
    # The old dedup_prevention decision row is gone; only the counter remains.
    orch._log_ai_decision.assert_not_called()


@pytest.mark.asyncio
async def test_attaches_to_the_case_of_the_overlapping_investigation():
    data_service = MagicMock()
    data_service.add_finding_to_case.return_value = True
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}}, data_service)

    await orch._create_investigation_for_finding(FINDING, None)

    data_service.add_finding_to_case.assert_called_once_with("case-1", "f-2")
    orch._append_trigger_id.assert_not_called()
    _nothing_new_opened(orch, data_service)


@pytest.mark.asyncio
async def test_prefers_the_first_overlap_that_has_a_case():
    data_service = MagicMock()
    data_service.add_finding_to_case.return_value = True
    orch = _orchestrator(
        ["inv-1", "inv-2"],
        {"inv-1": {"case_id": None}, "inv-2": {"case_id": "case-2"}},
        data_service,
    )

    await orch._create_investigation_for_finding(FINDING, None)

    data_service.add_finding_to_case.assert_called_once_with("case-2", "f-2")
    orch._append_trigger_id.assert_not_called()
    _nothing_new_opened(orch, data_service)


@pytest.mark.asyncio
async def test_without_a_case_the_finding_goes_onto_trigger_ids():
    data_service = MagicMock()
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": None}}, data_service)
    orch._append_trigger_id.return_value = True

    await orch._create_investigation_for_finding(FINDING, None)

    orch._append_trigger_id.assert_called_once_with("inv-1", "f-2")
    data_service.add_finding_to_case.assert_not_called()
    _nothing_new_opened(orch, data_service)


@pytest.mark.asyncio
async def test_a_failed_attach_is_logged_and_does_not_raise(caplog):
    data_service = MagicMock()
    data_service.add_finding_to_case.return_value = False
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}}, data_service)

    with caplog.at_level("WARNING"):
        await orch._create_investigation_for_finding(FINDING, None)

    assert any("could not be attached" in r.message for r in caplog.records)
    # Not retried on the trigger_ids path either: the case exists, the write failed.
    orch._append_trigger_id.assert_not_called()
    _nothing_new_opened(orch, data_service)


@pytest.mark.asyncio
async def test_no_data_service_reads_as_a_failed_attach(caplog):
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}}, None)

    with caplog.at_level("WARNING"):
        await orch._create_investigation_for_finding(FINDING, None)

    assert any("could not be attached" in r.message for r in caplog.records)
    orch._append_trigger_id.assert_not_called()
    _nothing_new_opened(orch, None)


# The fallback writer itself, over a fake session: the column has to be
# reassigned (a JSONB list mutated in place is not flushed), the append has to
# be idempotent, and a missing row has to read as a failed attach.
class _Row:
    def __init__(self, trigger_ids):
        self.trigger_ids = trigger_ids


class _FakeSession:
    def __init__(self, row):
        self.row = row

    def query(self, _model):
        return self

    def filter_by(self, **_):
        return self

    def first(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _writer(monkeypatch, row):
    db = MagicMock()
    db.session_scope.return_value = _FakeSession(row)
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)
    return object.__new__(Orchestrator)


def test_append_reassigns_the_trigger_ids_column(monkeypatch):
    original = ["f-1"]
    row = _Row(original)

    assert _writer(monkeypatch, row)._append_trigger_id("inv-1", "f-2") is True

    assert row.trigger_ids == ["f-1", "f-2"]
    assert row.trigger_ids is not original


def test_append_is_idempotent(monkeypatch):
    row = _Row(["f-1", "f-2"])

    assert _writer(monkeypatch, row)._append_trigger_id("inv-1", "f-2") is True

    assert row.trigger_ids == ["f-1", "f-2"]


def test_append_to_a_missing_investigation_reads_as_failed(monkeypatch):
    assert _writer(monkeypatch, None)._append_trigger_id("inv-gone", "f-2") is False


def test_a_failing_append_is_logged_and_does_not_raise(monkeypatch, caplog):
    db = MagicMock()
    db.session_scope.side_effect = RuntimeError("db down")
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)

    with caplog.at_level("ERROR"):
        ok = object.__new__(Orchestrator)._append_trigger_id("inv-1", "f-2")

    assert ok is False
    assert any("db down" in r.message for r in caplog.records)
