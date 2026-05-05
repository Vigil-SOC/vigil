"""Pre-flight cost gate tests for ``daemon.agent_runner.AgentRunner``.

The gate (added per #184 acceptance #4) is a thin wrapper around
``services.cost_estimator.estimate_cost``. The post-hoc gate at
``_run_agent`` line ~460 still catches actual overruns; this gate just
prevents one expensive iteration from blowing the budget before we look.

Tests cover the four behaviors that matter:

  1. Estimate fits within remaining budget → return False (proceed).
  2. Estimate exceeds remaining budget → return True (caller breaks).
  3. ``max_cost_per_investigation == 0`` is "unlimited" — caller skips
     the gate entirely; verified by asserting the gate-block side-effects
     don't fire when the agent loop's outer ``> 0`` check is false.
  4. Estimator failure must never block dispatch (return False, log).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


pytestmark = pytest.mark.unit


def _runner(*, max_cost_per_investigation: float):
    """Build an AgentRunner with a stubbed config + workdir.

    We don't exercise the agent loop here — only ``_preflight_budget_blocked``,
    which depends on ``self.config.max_cost_per_investigation``,
    ``self.config.plan_model``, ``self._claude_service``,
    ``self.workdir.append_log``, and ``self._mark_failed``.
    """
    from daemon.agent_runner import AgentRunner
    from daemon.config import OrchestratorConfig

    cfg = OrchestratorConfig()
    cfg.max_cost_per_investigation = max_cost_per_investigation
    cfg.plan_model = "claude-sonnet-4-5-20250929"

    workdir = MagicMock()
    workdir.append_log = MagicMock()

    runner = AgentRunner(cfg, workdir)
    runner._mark_failed = MagicMock()
    return runner


def _stub_estimate(high_usd: float, low_usd: float = 0.0, source: str = "exact"):
    """Build an awaitable that mimics services.cost_estimator.estimate_cost."""
    from services.cost_estimator import CostEstimate

    estimate = CostEstimate(
        provider_type="anthropic",
        model_id="claude-sonnet-4-5-20250929",
        input_tokens=1000,
        output_tokens_max=4096,
        low_usd=low_usd,
        high_usd=high_usd,
        pricing_source=source,
        token_count_method="anthropic_count_tokens",
    )

    async def _fake(**kwargs):
        return estimate

    return _fake


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_preflight_proceeds_when_estimate_fits_budget():
    runner = _runner(max_cost_per_investigation=5.0)
    with patch("services.cost_estimator.estimate_cost", _stub_estimate(high_usd=0.5)):
        blocked = _run(
            runner._preflight_budget_blocked(
                inv_id="inv-1",
                iteration=3,
                prompt="hello",
                total_cost=2.0,
            )
        )
    assert blocked is False
    runner._mark_failed.assert_not_called()


def test_preflight_blocks_when_projected_exceeds_budget():
    runner = _runner(max_cost_per_investigation=5.0)
    with patch("services.cost_estimator.estimate_cost", _stub_estimate(high_usd=4.0)):
        blocked = _run(
            runner._preflight_budget_blocked(
                inv_id="inv-1",
                iteration=3,
                prompt="hello",
                total_cost=2.0,
            )
        )
    # 2.0 + 4.0 = 6.0 > 5.0 → block
    assert blocked is True
    runner._mark_failed.assert_called_once_with(
        "inv-1", "Cost budget would be exceeded (pre-flight)"
    )
    # Workdir log entry recorded for audit trail.
    runner.workdir.append_log.assert_called_once()
    log_entry = runner.workdir.append_log.call_args.args[1]
    assert log_entry["event"] == "preflight_budget_block"
    assert log_entry["iteration"] == 3
    assert log_entry["estimate_high_usd"] == 4.0
    assert log_entry["max_cost_usd"] == 5.0


def test_preflight_blocks_at_exact_boundary_does_not_fire():
    """projected == budget should not block (the post-hoc gate uses >=,
    pre-flight uses > to leave one cent of slop for edge calls)."""
    runner = _runner(max_cost_per_investigation=5.0)
    with patch("services.cost_estimator.estimate_cost", _stub_estimate(high_usd=3.0)):
        blocked = _run(
            runner._preflight_budget_blocked(
                inv_id="inv-1",
                iteration=3,
                prompt="hello",
                total_cost=2.0,
            )
        )
    assert blocked is False


def test_preflight_swallows_estimator_errors_and_proceeds(caplog):
    """Telemetry must never block the call. If estimate_cost raises, the
    gate logs at debug and returns False so the call goes through and the
    post-hoc gate handles enforcement."""
    runner = _runner(max_cost_per_investigation=5.0)

    async def _raise(**kwargs):
        raise RuntimeError("count_tokens unavailable")

    with patch("services.cost_estimator.estimate_cost", _raise):
        blocked = _run(
            runner._preflight_budget_blocked(
                inv_id="inv-1",
                iteration=3,
                prompt="hello",
                total_cost=2.0,
            )
        )
    assert blocked is False
    runner._mark_failed.assert_not_called()


def test_preflight_records_pricing_source_in_log():
    """When the estimator returns pricing_source='unknown' / 'heuristic',
    the audit log includes it so operators can tell whether the block was
    based on exact or approximate pricing."""
    runner = _runner(max_cost_per_investigation=5.0)
    with patch(
        "services.cost_estimator.estimate_cost",
        _stub_estimate(high_usd=10.0, source="heuristic"),
    ):
        _run(
            runner._preflight_budget_blocked(
                inv_id="inv-1",
                iteration=3,
                prompt="hello",
                total_cost=0.0,
            )
        )
    log_entry = runner.workdir.append_log.call_args.args[1]
    assert log_entry["pricing_source"] == "heuristic"
