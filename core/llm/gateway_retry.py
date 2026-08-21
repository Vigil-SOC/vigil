# How a client answers the gateway, in one place. Bifrost owns the limits; what
# both processes need is the same reading of its refusals.
#
# 402 is terminal: the budget is gone and waiting will not bring it back.
# 429 is not: it means slow down, and a client that reports it as a budget
# failure turns a two-second wait into a failed run.
#
# The agent worker applies this in services/agent/core/limiter.ts. This is the
# Python half, and the two must not drift -- see tests/unit/llm/test_gateway_retry.py.

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

ATTEMPTS = 3
BASE_BACKOFF_S = 0.5
JITTER_S = 0.25

# Transient: the gateway or the provider behind it could not take the call now.
RETRYABLE = frozenset({429, 500, 502, 503})

# A ceiling was reached rather than a server stumbling. These answer the same way every
# attempt, so they get one more try and not three, each of which bills tokens the
# gateway discards. Mirrored in the agent layer's limiter.ts, held to it by a ratchet.
CEILING = frozenset({408, 504})

# The budget is spent. Retrying spends nothing and answers nothing.
EXHAUSTED = 402


def status_of(error: BaseException) -> Optional[int]:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def retry_after_s(error: BaseException) -> Optional[float]:
    headers = getattr(getattr(error, "response", None), "headers", None) or getattr(
        error, "headers", None
    )
    if not headers:
        return None
    try:
        raw = headers.get("retry-after")
    except AttributeError:
        return None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _body_of(error: BaseException) -> str:
    for attr in ("message", "body"):
        value = getattr(error, attr, None)
        if value:
            return str(value)
    response = getattr(error, "response", None)
    return getattr(response, "text", "") or "" if response is not None else ""


# Jittered, so callers that hit a 429 together do not come back in step.
def backoff_s(attempt: int, error: BaseException) -> float:
    stated = retry_after_s(error)
    base = stated if stated is not None else BASE_BACKOFF_S * (2**attempt)
    return base + random.random() * JITTER_S  # noqa: S311 — spacing retries, not crypto


async def through_gateway(
    call: Callable[[], Awaitable[T]], *, attempts: int = ATTEMPTS
) -> T:
    """Run an upstream call, retrying what the gateway says is transient.

    Raises ``BudgetExceeded`` on 402 and nothing else: a 429 that survives every
    attempt is re-raised as itself, so it reads as the rate limit it was.
    """
    from core.llm.cost.budget import BudgetExceeded

    last: BaseException
    ceilings = 0
    for attempt in range(attempts):
        try:
            return await call()
        except Exception as error:  # noqa: BLE001 — re-raised below unless retryable
            status = status_of(error)
            if status == EXHAUSTED:
                body = _body_of(error)
                raise BudgetExceeded(
                    tier=_tier_of(body),
                    message=body or "Bifrost reported the budget is spent",
                    status_code=status,
                ) from error
            if status in CEILING:
                ceilings += 1
                if ceilings > 1:
                    raise
            elif status is None or status not in RETRYABLE:
                raise
            last = error
            if attempt == attempts - 1:
                break
            wait = backoff_s(attempt, error)
            logger.info(
                "gateway returned %s; retrying in %.2fs (attempt %d/%d)",
                status,
                wait,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(wait)
    raise last


# Which budget ran out, when the gateway says so in the body. Only reached on a
# 402, so there is no rate-limit tier: that is not a budget.
def _tier_of(body: str) -> str:
    lowered = (body or "").lower()
    for tier in ("team", "customer"):
        if tier in lowered:
            return tier
    return "virtual_key"


# For a call that already happened -- a stream that died partway -- where there
# is nothing left to retry. Same reading of the status, no second attempt.
def translate(error: BaseException) -> BaseException:
    from core.llm.cost.budget import BudgetExceeded

    if status_of(error) != EXHAUSTED:
        return error
    body = _body_of(error)
    return BudgetExceeded(
        tier=_tier_of(body),
        message=body or "Bifrost reported the budget is spent",
        status_code=EXHAUSTED,
    )


def is_exhausted(status_code: Optional[Any]) -> bool:
    return status_code == EXHAUSTED
