"""The scheduled cleanup actually runs its sweeps, at the configured windows.

The wiring is the risk here, not the sweeps. `_run_cleanup` shipped as a stub that
computed a cutoff and only logged it, so "cleanup runs" was true while nothing was
cleaned. A sweep that is registered but never reached, or reached with the wrong
window, fails exactly the same silent way (#675).

Both sweeps are patched in every test, not only the one under assertion. An
unpatched sweep reaches Postgres for real, and this is a no-service unit test --
it passes on a developer's machine with a dev database up and fails in CI.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from services.daemon.config import SchedulerConfig
from services.daemon.scheduler import TaskScheduler

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_cleanup_expires_approvals_using_the_configured_window():
    config = SchedulerConfig()
    config.approval_expiry_days = 3
    scheduler = TaskScheduler(config)

    with (
        patch("core.response.checkpoints.expire_stale", return_value=2) as sweep,
        patch("core.memory.recall.expire_read_log", return_value=0),
    ):
        result = await scheduler._run_cleanup()

    sweep.assert_called_once_with(3)
    assert result["approvals_expired"] == 2


@pytest.mark.asyncio
async def test_cleanup_expires_the_read_log_at_the_retention_cutoff():
    # The read log sweep (#732) is wired the same way and fails the same silent
    # way. It takes the shared retention cutoff rather than a knob of its own,
    # so what is asserted is that it was reached with that cutoff.
    config = SchedulerConfig()
    config.cleanup_retention_days = 90
    scheduler = TaskScheduler(config)

    with (
        patch("core.response.checkpoints.expire_stale", return_value=0),
        patch("core.memory.recall.expire_read_log", return_value=7) as reads,
    ):
        result = await scheduler._run_cleanup()

    reads.assert_called_once()
    assert reads.call_args.args[0].isoformat() == result["cutoff_date"]
    assert result["read_log_removed"] == 7


@pytest.mark.asyncio
async def test_cleanup_reports_zero_when_nothing_is_stale():
    scheduler = TaskScheduler(SchedulerConfig())

    with (
        patch("core.response.checkpoints.expire_stale", return_value=0),
        patch("core.memory.recall.expire_read_log", return_value=0),
    ):
        result = await scheduler._run_cleanup()

    assert result["approvals_expired"] == 0
    # The cutoff drives only the read log sweep, but it is still reported:
    # it must not quietly disappear now that the approval sweep runs with it.
    assert "cutoff_date" in result


def test_the_expiry_window_is_wired_from_settings(monkeypatch):
    # A SchedulerConfig field nothing populates from Settings is a knob that
    # silently does nothing — the failure mode this whole issue is about.
    #
    # Asserted against a NON-default value on purpose. Comparing the two
    # defaults passes whether or not the wiring line exists, which is a
    # vacuous test of exactly the thing being checked.
    from core.config import get_settings
    from services.daemon.config import DaemonConfig

    monkeypatch.setenv("DAEMON_APPROVAL_EXPIRY_DAYS", "11")
    get_settings.cache_clear()

    config = DaemonConfig.from_env()
    assert config.scheduler.approval_expiry_days == 11


@pytest.mark.asyncio
async def test_cleanup_never_deletes_findings_or_processed_events(caplog):
    # Findings and processed events are intentionally NOT pruned by the daily
    # cleanup (#1741): the setting's name once promised "keep data for 90
    # days" while the sweep deleted none of it, and turning on real deletion
    # at a 90-day default would wipe findings on existing installs. Retention
    # for them is a Settings phase 2 decision. Pin the behaviour so a future
    # delete added to this sweep fails loudly here instead.
    scheduler = TaskScheduler(SchedulerConfig())
    scheduler._data_service = MagicMock()

    with (
        patch("core.response.checkpoints.expire_stale", return_value=2),
        patch("core.memory.recall.expire_read_log", return_value=5),
        caplog.at_level(logging.INFO, logger="services.daemon.scheduler"),
    ):
        result = await scheduler._run_cleanup()

    # The data service is the only path to the findings and processed-event
    # stores; the cleanup must not reach it at all.
    assert scheduler._data_service.mock_calls == []
    # The log reports what was actually removed, never what "would" be.
    assert "would remove" not in caplog.text
    assert "expired 2 unanswered approvals" in caplog.text
    assert "removed 5 episodic read log rows" in caplog.text
    assert result["approvals_expired"] == 2
    assert result["read_log_removed"] == 5
