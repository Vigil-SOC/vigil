"""Unit tests for AgentRunner._update_db_record.

Regression coverage for the silent-heartbeat bug that caused
auto-investigations to be stale-killed by the supervisor (issue #147
follow-up). The fix: route every DB write through ``session_scope()``
and surface failures at ``logger.error`` with a stack trace, instead of
swallowing them at ``logger.debug``.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from daemon.agent_runner import AgentRunner
from daemon.config import OrchestratorConfig

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


def _make_runner() -> AgentRunner:
    config = OrchestratorConfig()
    workdir = MagicMock()
    return AgentRunner(config, workdir)


def _patch_session_scope(row):
    """Build a context manager that yields _FakeSession(row)."""

    @contextmanager
    def fake_scope():
        yield _FakeSession(row)

    db_manager = MagicMock()
    db_manager.session_scope = fake_scope
    return db_manager


def test_update_db_record_writes_fields_via_session_scope():
    runner = _make_runner()
    row = MagicMock()
    row.iteration_count = 0
    row.cost_usd = 0.0

    db_manager = _patch_session_scope(row)
    with patch("database.connection.get_db_manager", return_value=db_manager):
        with patch("database.models.Investigation"):
            runner._update_db_record(
                "inv-test-1",
                {"iteration_count": 5, "cost_usd": 0.12},
            )

    assert row.iteration_count == 5
    assert row.cost_usd == 0.12


def test_update_db_record_parses_iso_string_for_at_fields():
    runner = _make_runner()
    row = MagicMock()
    row.last_activity_at = None

    db_manager = _patch_session_scope(row)
    iso = "2026-04-28T12:34:56"
    with patch("database.connection.get_db_manager", return_value=db_manager):
        with patch("database.models.Investigation"):
            runner._update_db_record(
                "inv-test-2",
                {"last_activity_at": iso},
            )

    assert isinstance(row.last_activity_at, datetime)
    assert row.last_activity_at == datetime.fromisoformat(iso)


def test_update_db_record_logs_error_with_traceback_on_failure(caplog):
    """A broken DB connection must log at ERROR with exc_info, not DEBUG.

    The previous behaviour swallowed exceptions to ``logger.debug``, which
    hid silent heartbeat failures and let the supervisor mark healthy
    investigations as ``Stale: no activity``.
    """
    runner = _make_runner()

    db_manager = MagicMock()

    @contextmanager
    def broken_scope():
        raise RuntimeError("connection pool exhausted")
        yield  # unreachable, satisfies generator contract

    db_manager.session_scope = broken_scope

    with patch("database.connection.get_db_manager", return_value=db_manager):
        with caplog.at_level(logging.ERROR, logger="daemon.agent_runner"):
            runner._update_db_record("inv-test-3", {"cost_usd": 1.0})

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("DB update for inv-test-3 failed" in r.message for r in error_records)
    # exc_info=True records exception info on the LogRecord
    assert any(r.exc_info is not None for r in error_records)


def test_update_db_record_warns_when_row_missing():
    runner = _make_runner()
    db_manager = _patch_session_scope(row=None)  # query returns no row

    with patch("database.connection.get_db_manager", return_value=db_manager):
        with patch("database.models.Investigation"):
            with patch.object(
                __import__("daemon.agent_runner", fromlist=["logger"]).logger,
                "warning",
            ) as warn:
                runner._update_db_record("inv-missing", {"cost_usd": 0.0})
                warn.assert_called_once()
                assert "row not found" in warn.call_args[0][0]
