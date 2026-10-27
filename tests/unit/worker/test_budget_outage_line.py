"""A spent LLM budget is one outage per tier, then one recovery line."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm import outage
from core.llm.cost.budget import BudgetExceeded
from services.worker.jobs import llm_call

pytestmark = pytest.mark.unit

_BUDGET = "LLM budget spent (%s tier); AI calls are refused until it resets"
_FAILED = "llm_call failed (returning error dict): %s"


@pytest.fixture(autouse=True)
def _clean_state():
    outage._failures.clear()
    yield
    outage._failures.clear()


def _ctx(dispatch: AsyncMock, claude: MagicMock | None = None) -> dict:
    router = MagicMock()
    router.dispatch = dispatch
    if claude is None:
        claude = MagicMock()
        claude.chat.return_value = "from-claude"
    return {
        "in_flight": asyncio.Semaphore(1),
        "claude_service": claude,
        "llm_router": router,
    }


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.ERROR]


def _infos(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.INFO]


def test_three_budget_refusals_log_once_then_a_success_recovers(caplog):
    dispatch = AsyncMock(
        side_effect=[
            BudgetExceeded(tier="virtual_key"),
            BudgetExceeded(tier="virtual_key"),
            BudgetExceeded(tier="virtual_key"),
            {"content": "ok", "type": "text"},
        ]
    )
    ctx = _ctx(dispatch)

    async def _run():
        refused = []
        with patch("core.llm.router.router.get_provider_spec", return_value=object()):
            for _ in range(3):
                refused.append(
                    await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
                )
            ok = await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
        return refused, ok

    with caplog.at_level(logging.INFO, logger="services.worker.jobs"):
        refused, ok = asyncio.run(_run())

    assert [r["type"] for r in refused] == ["error", "error", "error"]
    assert all(r["error"].startswith("BudgetExceeded:") for r in refused)
    assert ok == {"content": "ok", "type": "text"}

    errors = _errors(caplog)
    assert len(errors) == 1
    assert errors[0].msg == _BUDGET
    assert errors[0].args == ("virtual_key",)
    assert errors[0].exc_info is None
    assert _FAILED not in errors[0].getMessage()

    infos = _infos(caplog)
    assert len(infos) == 1
    assert infos[0].msg == _BUDGET + "; recovered after %d failures"
    assert infos[0].args == ("virtual_key", 3)
    assert "recovered after 3 failures" in infos[0].getMessage()


def test_each_budget_tier_is_its_own_outage(caplog):
    dispatch = AsyncMock(
        side_effect=[
            BudgetExceeded(tier="virtual_key"),
            BudgetExceeded(tier="team"),
            BudgetExceeded(tier="virtual_key"),
            BudgetExceeded(tier="customer"),
        ]
    )
    ctx = _ctx(dispatch)

    async def _run():
        with patch("core.llm.router.router.get_provider_spec", return_value=object()):
            for _ in range(4):
                await llm_call(ctx, "p", "m", 10, None, provider_id="prov")

    with caplog.at_level(logging.ERROR, logger="services.worker.jobs"):
        asyncio.run(_run())

    errors = _errors(caplog)
    assert [r.msg for r in errors] == [_BUDGET, _BUDGET, _BUDGET]
    assert [r.args for r in errors] == [("virtual_key",), ("team",), ("customer",)]


def test_claude_fallback_does_not_clear_a_budget_outage(caplog):
    claude = MagicMock()
    claude.chat.return_value = "from-claude"
    dispatch = AsyncMock(
        side_effect=[
            BudgetExceeded(tier="team"),
            BudgetExceeded(tier="team"),
            {"content": "ok", "type": "text"},
        ]
    )
    ctx = _ctx(dispatch, claude)

    async def _run():
        router = ctx["llm_router"]
        with patch(
            "core.llm.router.router.get_provider_spec",
            side_effect=[object(), None, object(), object()],
        ):
            refused = await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
            # provider_id is None, no router, and no spec are the three fallbacks.
            via_default = await llm_call(ctx, "p", "m", 10, None)
            ctx["llm_router"] = None
            via_no_router = await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
            ctx["llm_router"] = router
            via_no_spec = await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
            still = await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
            recovered = await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
        return refused, via_default, via_no_router, via_no_spec, still, recovered

    with caplog.at_level(logging.INFO, logger="services.worker.jobs"):
        refused, via_default, via_no_router, via_no_spec, still, recovered = asyncio.run(
            _run()
        )

    assert refused["error"].startswith("BudgetExceeded:")
    assert via_default["content"] == "from-claude"
    assert via_no_router["content"] == "from-claude"
    assert via_no_spec["content"] == "from-claude"
    assert still["error"].startswith("BudgetExceeded:")
    assert recovered == {"content": "ok", "type": "text"}
    assert claude.chat.call_count == 3

    errors = _errors(caplog)
    # The second refusal is the same tier, still inside the outage, so no second ERROR.
    assert len(errors) == 1
    assert errors[0].args == ("team",)
    infos = _infos(caplog)
    assert len(infos) == 1
    assert infos[0].args == ("team", 2)
    assert "recovered after 2 failures" in infos[0].getMessage()


def test_other_exceptions_log_every_call_with_exc_info(caplog):
    dispatch = AsyncMock(side_effect=RuntimeError("boom"))
    ctx = _ctx(dispatch)

    async def _run():
        with patch("core.llm.router.router.get_provider_spec", return_value=object()):
            return [
                await llm_call(ctx, "p", "m", 10, None, provider_id="prov")
                for _ in range(3)
            ]

    with caplog.at_level(logging.ERROR, logger="services.worker.jobs"):
        results = asyncio.run(_run())

    assert all(r["error"].startswith("RuntimeError:") for r in results)
    errors = _errors(caplog)
    assert len(errors) == 3
    assert [r.msg for r in errors] == [_FAILED, _FAILED, _FAILED]
    assert all(r.exc_info and r.exc_info[0] is RuntimeError for r in errors)
    assert all(_BUDGET not in r.getMessage() for r in errors)
