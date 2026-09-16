"""Run-outcome counters and the approval-depth gauge (#895).

``get_meter`` is swapped for a recording fake so these run without OTEL and
without a database; the DB-backed label check lives in
``tests/unit/workflows/test_workflow_run_service.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.agents.queue as queue
import core.response.approval_service as approvals
import core.workflows.workflow_run_service as run_service


class _FakeCounter:
    def __init__(self):
        self.adds = []

    def add(self, amount, attributes=None):
        self.adds.append((amount, dict(attributes or {})))


class _FakeMeter:
    def __init__(self):
        self.counters = {}
        self.gauges = {}

    def create_counter(self, name, **_):
        return self.counters.setdefault(name, _FakeCounter())

    def create_observable_gauge(self, name, callbacks=(), **_):
        self.gauges[name] = list(callbacks)
        return object()


@pytest.fixture
def meter(monkeypatch):
    fake = _FakeMeter()
    # Reset the lazy caches so each test sees its own instruments.
    monkeypatch.setattr(queue, "_runs_started", None)
    monkeypatch.setattr(run_service, "_runs_finished", None)
    monkeypatch.setattr(approvals, "_pending_gauge", None)
    monkeypatch.setattr(queue, "get_meter", lambda name: fake)
    monkeypatch.setattr(run_service, "get_meter", lambda name: fake)
    monkeypatch.setattr(approvals, "get_meter", lambda name: fake)
    return fake


def _fake_queue():
    q = MagicMock()
    q.add = AsyncMock(return_value=SimpleNamespace(id="job-1"))
    return q


async def test_start_counts_by_run_kind_and_resume_does_not(meter):
    with patch.object(queue, "_run_queue", return_value=_fake_queue()):
        await queue.enqueue_run(queue.build_start_job("r1", "hunt", {}, "test"))
        await queue.enqueue_run(queue.build_start_job("r2", "compose", {}, "test"))
        await queue.enqueue_run(queue.build_resume_job("r1", "hunt", "test"))

    assert meter.counters["vigil.runs.started"].adds == [
        (1, {"run_kind": "hunt"}),
        (1, {"run_kind": "compose"}),
    ]


async def test_failed_enqueue_is_not_counted(meter):
    q = _fake_queue()
    q.add = AsyncMock(side_effect=RuntimeError("redis down"))
    with patch.object(queue, "_run_queue", return_value=q), patch.object(
        queue, "close_run_queue", new=AsyncMock()
    ):
        with pytest.raises(RuntimeError):
            await queue.enqueue_run(queue.build_start_job("r1", "hunt", {}, "test"))
    assert "vigil.runs.started" not in meter.counters


def _db_with_row(row):
    session = MagicMock()
    session.get.return_value = row
    session.__enter__ = lambda s: session
    session.__exit__ = lambda s, *a: False
    db = MagicMock()
    db.session_scope.return_value = session
    return db


@pytest.mark.parametrize(
    ("trigger_context", "expected_kind"),
    [({"run_kind": "root_cause"}, "root_cause"), ({}, "unknown"), (None, "unknown")],
)
def test_finalize_labels_run_kind_and_status(meter, trigger_context, expected_kind):
    row = SimpleNamespace(trigger_context=trigger_context, started_at=None)
    with patch.object(run_service, "get_db_manager", return_value=_db_with_row(row)):
        assert run_service.WorkflowRunService().finalize_run("r1", status="failed")

    assert meter.counters["vigil.runs.finished"].adds == [
        (1, {"run_kind": expected_kind, "status": "failed"})
    ]


def test_finalize_of_unknown_run_or_bad_status_is_not_counted(meter):
    svc = run_service.WorkflowRunService()
    with patch.object(run_service, "get_db_manager", return_value=_db_with_row(None)):
        assert svc.finalize_run("missing", status="completed") is False
    assert svc.finalize_run("r1", status="running") is False
    assert "vigil.runs.finished" not in meter.counters


def test_pending_gauge_registers_once_and_reports_queue_depth(meter):
    service = MagicMock()
    service.list_pending_approvals.return_value = [object()] * 3

    approvals.register_pending_gauge(service)
    approvals.register_pending_gauge(MagicMock())

    callbacks = meter.gauges["vigil.approvals.pending"]
    assert len(callbacks) == 1
    assert [o.value for o in callbacks[0](None)] == [
        len(service.list_pending_approvals())
    ]


def test_pending_gauge_drops_sample_when_service_fails(meter):
    service = MagicMock()
    service.list_pending_approvals.side_effect = RuntimeError("db down")
    approvals.register_pending_gauge(service)
    assert list(meter.gauges["vigil.approvals.pending"][0](None)) == []
