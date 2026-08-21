"""One reading of the gateway's refusals, shared with the agent worker.

Before this, Python raised ``BudgetExceeded`` on a 429 and nothing caught it:
the docstring said the agent loop treated it as terminal, but that loop was
deleted in #629, so a transient rate limit failed the run and was reported as a
budget problem. TypeScript retried the same response.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from core.llm.cost.budget import BudgetExceeded
from core.llm.gateway_retry import (
    ATTEMPTS,
    EXHAUSTED,
    RETRYABLE,
    backoff_s,
    retry_after_s,
    through_gateway,
    translate,
)

LIMITER = REPO_ROOT / "services" / "agent" / "core" / "limiter.ts"

pytestmark = pytest.mark.unit


class Refused(Exception):
    def __init__(
        self, status_code: int, message: str = "", headers: dict | None = None
    ):
        super().__init__(message or f"status {status_code}")
        self.status_code = status_code
        self.message = message
        self.headers = headers or {}


def answering(*outcomes):
    """A call that yields each outcome in turn, raising the ones that are errors."""
    seen = []

    async def call():
        outcome = outcomes[len(seen)]
        seen.append(outcome)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    call.attempts = lambda: len(seen)  # type: ignore[attr-defined]
    return call


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("core.llm.gateway_retry.asyncio.sleep", instant)


class TestWhatRetries:
    @pytest.mark.asyncio
    async def test_a_rate_limit_is_retried_not_reported_as_a_budget_failure(self):
        call = answering(Refused(429), "answered")
        assert await through_gateway(call) == "answered"
        assert call.attempts() == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", sorted(RETRYABLE))
    async def test_every_transient_status_gets_a_second_chance(self, status):
        call = answering(Refused(status), "answered")
        assert await through_gateway(call) == "answered"

    @pytest.mark.asyncio
    async def test_it_gives_up_after_the_attempt_budget_and_raises_what_it_saw(self):
        call = answering(Refused(429), Refused(429), Refused(429))
        # Re-raised as itself, so a rate limit that never cleared still reads as
        # a rate limit rather than as a budget that was never exhausted.
        with pytest.raises(Refused) as raised:
            await through_gateway(call)
        assert raised.value.status_code == 429
        assert call.attempts() == 3

    @pytest.mark.asyncio
    async def test_an_unexpected_status_is_not_retried(self):
        call = answering(Refused(400, "malformed request"), "unreachable")
        with pytest.raises(Refused):
            await through_gateway(call)
        assert call.attempts() == 1

    @pytest.mark.asyncio
    async def test_an_error_with_no_status_is_not_retried(self):
        call = answering(ValueError("something local broke"), "unreachable")
        with pytest.raises(ValueError):
            await through_gateway(call)
        assert call.attempts() == 1


class TestWhatIsTerminal:
    @pytest.mark.asyncio
    async def test_a_spent_budget_raises_immediately(self):
        call = answering(
            Refused(EXHAUSTED, "virtual key budget exhausted"), "unreachable"
        )
        with pytest.raises(BudgetExceeded) as raised:
            await through_gateway(call)
        # Waiting does not refill a budget, so there is no second attempt.
        assert call.attempts() == 1
        assert raised.value.status_code == EXHAUSTED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,tier",
        [
            ("team budget exhausted", "team"),
            ("customer cap reached", "customer"),
            ("out of credit", "virtual_key"),
        ],
    )
    async def test_it_names_which_budget_ran_out(self, body, tier):
        with pytest.raises(BudgetExceeded) as raised:
            await through_gateway(answering(Refused(EXHAUSTED, body)))
        assert raised.value.tier == tier

    def test_a_dead_stream_is_translated_without_a_second_attempt(self):
        # The call already ran; there is nothing left to retry.
        assert isinstance(translate(Refused(EXHAUSTED, "spent")), BudgetExceeded)
        # And anything else comes back untouched, to be raised as it was.
        transient = Refused(429)
        assert translate(transient) is transient


class TestBackoff:
    def test_it_honours_the_gateway_stated_wait(self):
        wait = backoff_s(0, Refused(429, headers={"retry-after": "7"}))
        assert 7 <= wait <= 7.25

    def test_it_grows_when_the_gateway_says_nothing(self):
        first, second = backoff_s(0, Refused(429)), backoff_s(2, Refused(429))
        assert second > first

    def test_a_malformed_retry_after_falls_back_rather_than_raising(self):
        assert retry_after_s(Refused(429, headers={"retry-after": "soon"})) is None
        assert backoff_s(0, Refused(429, headers={"retry-after": "soon"})) > 0


class TestACeilingIsNotAStumble:
    """A 504 is Bifrost saying the call took longer than it allows, which it will say
    again just as fast. Retried three times it costs a caller ninety seconds and three
    upstream generations to learn nothing."""

    @pytest.mark.asyncio
    async def test_a_ceiling_is_tried_twice_and_not_three_times(self):
        calls = {"n": 0}

        async def always_over():
            calls["n"] += 1
            raise Refused(504)

        with pytest.raises(Refused):
            await through_gateway(always_over)
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_a_rate_limit_still_gets_every_attempt(self):
        calls = {"n": 0}

        async def busy():
            calls["n"] += 1
            raise Refused(429)

        with pytest.raises(Refused):
            await through_gateway(busy)
        assert calls["n"] == ATTEMPTS


class TestTheTwoHalvesAgree:
    """The agent worker applies the same policy in TypeScript. If one changes
    without the other, a 429 behaves differently depending on which process
    made the call -- which is the drift this whole module exists to remove."""

    def _limiter(self) -> str:
        return LIMITER.read_text()

    def test_both_treat_the_same_statuses_as_transient(self):
        listed = re.search(r"RETRYABLE = new Set\(\[([^\]]*)\]\)", self._limiter())
        assert listed, "RETRYABLE not found in limiter.ts"
        assert {int(n) for n in re.findall(r"\d+", listed.group(1))} == set(RETRYABLE)

    # Split out of RETRYABLE on both sides: a ceiling answers identically every attempt,
    # so three of them buy nothing and cost a caller its whole call in wall clock.
    def test_both_give_a_ceiling_one_more_attempt_rather_than_three(self):
        from core.llm.gateway_retry import CEILING

        listed = re.search(r"CEILING = new Set\(\[([^\]]*)\]\)", self._limiter())
        assert listed, "CEILING not found in limiter.ts"
        assert {int(n) for n in re.findall(r"\d+", listed.group(1))} == set(CEILING)
        assert not (CEILING & set(RETRYABLE)), "a status cannot be on both paths"

    def test_both_treat_a_spent_budget_as_terminal(self):
        assert f"status === {EXHAUSTED}" in self._limiter()

    def test_both_make_the_same_number_of_attempts(self):
        stated = re.search(r"attempts = (\d+)", self._limiter())
        assert stated, "the attempt budget is not stated in limiter.ts"
        from core.llm.gateway_retry import ATTEMPTS

        assert int(stated.group(1)) == ATTEMPTS
