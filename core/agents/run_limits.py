"""Ceilings on what a caller may ask of an agent run's budget.

The worker applies ``overrides`` over the run's config (``withOverrides`` in
services/agent/core/spec.ts) and checks only that each value is a positive number.
The orchestrator sets its own ceilings from operator settings and is trusted. A
caller on the API or the console is not: it may raise a run's limits, but only up
to these, which also bound the hard ceilings the hunt derives from the ask.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

# The console's run form already caps a hunt at $100.
MAX_COST_USD = 100.0
# ...and at 40 turns.
MAX_ITERATIONS = 40

OVERRIDE_CEILINGS: Mapping[str, Mapping[str, float]] = {
    "budgets": {
        "max_calls": 1_000,
        "max_cost_usd": MAX_COST_USD,
        "max_wall_ms": 24 * 3600 * 1000,
        "max_park_ms": 30 * 24 * 3600 * 1000,
    },
    "runtime": {
        "max_turns": 200,
        "result_cap": 100_000,
        "recall_limit": 50,
    },
}

# The wall ceiling above, in whole minutes: what a caller may ask on execute.
MAX_WALL_MINUTES = int(OVERRIDE_CEILINGS["budgets"]["max_wall_ms"] // 60_000)


class OverrideRefused(ValueError):
    """The caller asked for a limit the deployment does not grant."""


def check_wall_minutes(minutes: Any) -> int:
    """Whole minutes from 1 to the wall ceiling.

    A float that is a whole number counts. Anything else, including 0, is refused.
    """
    if isinstance(minutes, float) and math.isfinite(minutes) and minutes.is_integer():
        minutes = int(minutes)
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int)
        or not 1 <= minutes <= MAX_WALL_MINUTES
    ):
        raise OverrideRefused(
            "max_wall_minutes must be a whole number of minutes from 1 to "
            f"{MAX_WALL_MINUTES}"
        )
    return minutes


def check_overrides(overrides: Optional[Mapping[str, Any]]) -> None:
    """Raise OverrideRefused unless every value is a positive number within its ceiling."""
    for block, values in (overrides or {}).items():
        ceilings = OVERRIDE_CEILINGS.get(block)
        if ceilings is None:
            raise OverrideRefused(f"overrides may name budgets or runtime, not {block}")
        if not isinstance(values, Mapping):
            raise OverrideRefused(f"overrides.{block} must be an object")
        for key, value in values.items():
            if key not in ceilings:
                raise OverrideRefused(
                    f"unknown overrides.{block} key: {key}; "
                    f"expected any of {', '.join(sorted(ceilings))}"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OverrideRefused(f"overrides.{block}.{key} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise OverrideRefused(
                    f"overrides.{block}.{key} must be a positive number"
                )
            if value > ceilings[key]:
                raise OverrideRefused(
                    f"overrides.{block}.{key} may not exceed {ceilings[key]:g}"
                )
