"""Put the 30-case check on an investigation limit suggestion, from two eval runs.

A person runs the engine eval (``services/agent/eval``) twice on the same model:
once at today's limit and once at the limit the suggestion proposes, with the
eval's budget flags (``--max-cost-usd``, ``--max-wall-seconds``, ``--max-calls``),
which also stamp each result line with the budgets it ran under. This reads both result files and records on the live
suggestion whether the proposed limit misses no more attacks than today's
(passed), with both counts and the proposed run's cost a case.

    python -m core.supervisory.eval_check investigate.max_cost_per_investigation \\
        eval/results/cost-now.jsonl eval/results/cost-next.jsonl

A missed attack is a case whose truth is attack and whose graded verdict is
benign, as the eval's own score counts it. Both runs must cover the same cases,
each with the same truth and none in error, on one model, and agree on every
budget but the one the setting sets, which must be the suggestion's from value in
today's run and its to value in the proposed one. Anything else is refused and
nothing is written.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.supervisory import store
from core.supervisory.checks.limits import LIMITS, Limit

# The eval budget each limit sets, and how a setting's value reads as that budget.
_BUDGET = {
    "investigate.max_cost_per_investigation": ("max_cost_usd", float),
    "investigate.max_runtime_per_investigation": ("max_wall_ms", lambda v: v * 1000),
    "investigate.max_iterations_per_agent": ("max_calls", float),
}


class CheckRefusal(Exception):
    """The two runs can't be compared for this suggestion; the message says why."""


@dataclass(frozen=True)
class Run:
    # case id -> its truth, attack or benign
    truth: dict[str, str]
    attacks: int
    missed: int
    cost_per_case: float
    budgets: dict[str, float]
    model: str


def read_run(path: Path) -> Run:
    """One eval result file, refused when a case errored or budgets or models differ."""
    try:
        rows = [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
    except (OSError, ValueError, RecursionError) as e:
        raise CheckRefusal(f"{path}: {e}") from None
    if not rows:
        raise CheckRefusal(f"{path} has no results")
    if not all(isinstance(r, dict) for r in rows):
        raise CheckRefusal(f"{path} has a line that is not a result")
    errored = [r.get("case_id") for r in rows if r.get("error")]
    if errored:
        raise CheckRefusal(f"{path}: {len(errored)} cases errored, e.g. {errored[0]}")
    if not all(
        isinstance(r, dict)
        and isinstance(r.get("case_id"), str)
        and r["case_id"]
        and isinstance(r.get("model"), str)
        and r.get("truth") in ("attack", "benign")
        # The eval's own score counts a case with no verdict as an error.
        and r.get("verdict") in ("attack", "benign", "undetermined")
        for r in rows
    ):
        raise CheckRefusal(
            f"{path} has a line that is not a graded result with a case, a model and a truth"
        )
    budgets = {json.dumps(r.get("budgets"), sort_keys=True) for r in rows}
    if len(budgets) != 1 or not _numbers(rows[0].get("budgets")):
        raise CheckRefusal(f"{path} does not record one set of budgets for every case")
    costs = [r.get("cost_usd") or 0 for r in rows]
    if not all(_finite(c) for c in costs):
        raise CheckRefusal(f"{path} has a cost that is not a number")
    models = {r.get("model") for r in rows}
    if len(models) != 1:
        raise CheckRefusal(f"{path} mixes models: {sorted(map(str, models))}")
    # A --dry run answers by schema and examines nothing, so it measures nothing.
    if rows[0]["model"].startswith("dry/"):
        raise CheckRefusal(f"{path} is a dry run, which measures nothing")
    cases = [r.get("case_id") for r in rows]
    if len(set(cases)) != len(cases):
        raise CheckRefusal(f"{path} has a case more than once")
    attacks = [r for r in rows if r.get("truth") == "attack"]
    cost_per_case = sum(float(c) for c in costs) / len(rows)
    if not math.isfinite(cost_per_case):
        raise CheckRefusal(f"{path} has costs too large to add up")
    return Run(
        truth={r.get("case_id"): r.get("truth") for r in rows},
        attacks=len(attacks),
        missed=sum(1 for r in attacks if r.get("verdict") == "benign"),
        cost_per_case=cost_per_case,
        budgets=rows[0]["budgets"],
        model=str(rows[0].get("model")),
    )


def compare(
    key: str, now: Run, proposed: Run, from_value: str, to_value: str
) -> dict[str, Any]:
    """The check for the card: passed when the proposed limit misses no more attacks."""
    if key not in _BUDGET:
        raise CheckRefusal(f"{key} is not an investigation limit the eval can run at")
    if now.truth != proposed.truth:
        raise CheckRefusal(
            "The two runs do not cover the same cases with the same truth"
        )
    if now.model != proposed.model:
        raise CheckRefusal(
            f"The runs used different models: {now.model}, {proposed.model}"
        )
    if not now.attacks:
        raise CheckRefusal("The runs have no attack cases to miss")
    try:
        values = float(from_value), float(to_value)
    except (TypeError, ValueError):
        raise CheckRefusal(
            f"The suggestion's values are not numbers: {from_value}, {to_value}"
        ) from None
    if not all(math.isfinite(v) for v in values):
        raise CheckRefusal(
            f"The suggestion's values are not numbers: {from_value}, {to_value}"
        )
    field, as_budget = _BUDGET[key]
    others = {k for k in now.budgets.keys() | proposed.budgets.keys() if k != field}
    if any(now.budgets.get(k) != proposed.budgets.get(k) for k in others):
        raise CheckRefusal(
            f"The runs differ in more than {field}: {now.budgets} and {proposed.budgets}"
        )
    for run, value, which in (
        (now, values[0], "today's"),
        (proposed, values[1], "the proposed"),
    ):
        want = as_budget(value)
        got = float(run.budgets.get(field, math.nan))
        if not math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-6):
            raise CheckRefusal(
                f"The run for {which} limit ran at {field} {got}, not {want}"
            )
    limit = _limit(key)
    passed = proposed.missed <= now.missed
    text = (
        f"At {limit.say(values[1])}: {proposed.missed} of {proposed.attacks} "
        f"attacks missed · at {limit.say(values[0])}: {now.missed} missed · "
        f"${proposed.cost_per_case:.2f} a case"
    )
    return {"passed": passed, "text": text}


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _numbers(budgets: Any) -> bool:
    """A budgets object whose every value is a finite number."""
    return (
        isinstance(budgets, dict)
        and bool(budgets)
        and all(_finite(v) for v in budgets.values())
    )


def _limit(key: str) -> Limit:
    return next(limit for limit in LIMITS if limit.key == key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.supervisory.eval_check",
        description="Record the 30-case check on the live suggestion for an investigation limit.",
    )
    parser.add_argument(
        "key", help="the setting, e.g. investigate.max_cost_per_investigation"
    )
    parser.add_argument("now", type=Path, help="eval results at today's limit")
    parser.add_argument(
        "proposed", type=Path, help="eval results at the proposed limit"
    )
    args = parser.parse_args(argv)

    from core.storage.connection import init_database

    init_database()
    live = store.live_change(args.key)
    if live is None:
        print(f"No live suggestion for {args.key}", file=sys.stderr)
        return 1
    from_value, to_value = live
    try:
        check = compare(
            args.key, read_run(args.now), read_run(args.proposed), from_value, to_value
        )
        suggestion_id = store.record_check(args.key, check, from_value, to_value)
    except (CheckRefusal, store.StoreRefusal) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(
        f"{'passed' if check['passed'] else 'failed'}: {check['text']} (suggestion {suggestion_id})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
