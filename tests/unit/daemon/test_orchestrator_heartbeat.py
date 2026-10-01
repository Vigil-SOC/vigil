"""The investigation heartbeat, ported from AgentRunner._update_db_record.

Regression coverage for the silent-heartbeat bug that let the supervisor
stale-kill healthy auto-investigations (issue #147 follow-up). The runner is
gone (#629) and the write moved to ``Orchestrator._record_progress``, but the
failure mode did not: swallow the error and ``last_activity_at`` stops moving
while the run is fine.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


class _FakeSession:
    """Minimal fake SQLAlchemy session that records setattr calls."""

    def __init__(self, row):
        self._row = row

    def query(self, _model):
        return self

    def filter_by(self, **_kwargs):
        return self

    def first(self):
        return self._row


def _make_orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.workdir = MagicMock()
    return orch


def _patch_session_scope(row):
    """Build a context manager that yields _FakeSession(row)."""

    @contextmanager
    def fake_scope():
        yield _FakeSession(row)

    db_manager = MagicMock()
    db_manager.session_scope = fake_scope
    return db_manager


def test_progress_writes_fields_via_session_scope():
    orch = _make_orchestrator()
    row = MagicMock()
    row.iteration_count = 0
    row.cost_usd = 0.0

    db_manager = _patch_session_scope(row)
    with patch("core.storage.connection.get_db_manager", return_value=db_manager):
        with patch("core.storage.models.Investigation"):
            orch._record_progress("inv-test-1", {"iterations": 5, "cost_usd": 0.12})

    assert row.iteration_count == 5
    assert row.cost_usd == 0.12


def test_progress_reads_iteration_when_the_fold_has_no_iterations():
    orch = _make_orchestrator()
    row = MagicMock()
    row.iteration_count = 0

    db_manager = _patch_session_scope(row)
    with patch("core.storage.connection.get_db_manager", return_value=db_manager):
        with patch("core.storage.models.Investigation"):
            orch._record_progress("inv-hunt", {"iteration": 4, "cost_usd": 0.1})

    assert row.iteration_count == 4


def test_progress_stamps_the_heartbeat():
    orch = _make_orchestrator()
    row = MagicMock()
    row.last_activity_at = None

    db_manager = _patch_session_scope(row)
    with patch("core.storage.connection.get_db_manager", return_value=db_manager):
        with patch("core.storage.models.Investigation"):
            orch._record_progress("inv-test-2", {"iterations": 1})

    # Stamped here rather than carried in the projection: it records when this
    # side last heard from the run, which is what the stale check asks about.
    assert isinstance(row.last_activity_at, datetime)


def test_progress_leaves_cost_alone_when_the_gateway_priced_nothing():
    orch = _make_orchestrator()
    row = MagicMock()
    row.cost_usd = 0.42

    db_manager = _patch_session_scope(row)
    with patch("core.storage.connection.get_db_manager", return_value=db_manager):
        with patch("core.storage.models.Investigation"):
            orch._record_progress("inv-test-cost", {"iterations": 2, "cost_usd": None})

    # None is "nothing was priced", not "nothing was spent". Writing 0.0 here
    # would reset a real spend and hand the run its budget back.
    assert row.cost_usd == 0.42


def test_progress_logs_error_with_traceback_on_failure(caplog):
    """A broken DB connection must log at ERROR with exc_info, not DEBUG.

    Swallowing it to ``logger.debug`` hides silent heartbeat failures and lets
    the supervisor mark healthy investigations ``Stale: no activity``.
    """
    orch = _make_orchestrator()
    db_manager = MagicMock()

    @contextmanager
    def broken_scope():
        raise RuntimeError("connection pool exhausted")
        yield  # unreachable, satisfies generator contract

    db_manager.session_scope = broken_scope

    with patch("core.storage.connection.get_db_manager", return_value=db_manager):
        with caplog.at_level(logging.ERROR, logger="services.daemon.orchestrator"):
            orch._record_progress("inv-test-3", {"iterations": 1})

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("DB update for inv-test-3 failed" in r.message for r in error_records)
    assert any(r.exc_info is not None for r in error_records)


def test_progress_warns_when_row_missing():
    orch = _make_orchestrator()
    db_manager = _patch_session_scope(row=None)

    with patch("core.storage.connection.get_db_manager", return_value=db_manager):
        with patch("core.storage.models.Investigation"):
            with patch("services.daemon.orchestrator.logger.warning") as warn:
                orch._record_progress("inv-missing", {"iterations": 0})
                warn.assert_called_once()
                assert "row not found" in warn.call_args[0][0]
