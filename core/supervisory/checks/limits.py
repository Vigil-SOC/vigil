"""Check the investigation limits Vigil can change against how stopped runs turned out.

Each limit the orchestrator puts on an investigation becomes the run's budget,
and a run that reaches it ends ``budget_exhausted`` with the limit in its reason:

- ``max_iterations_per_agent`` is the run's call budget: ``calls_exhausted``;
- ``max_cost_per_investigation`` its cost budget: ``cost_exhausted``;
- ``max_runtime_per_investigation`` its wall budget: ``wall_exhausted``.

Over the last 30 days, for each limit, the check counts the investigations whose
run stopped at it, and how many of their Cases a person later closed as a real
attack. A limit that cut off at least two real attacks (the noise margin on
missed attacks) is suggested half as high again, within the bounds Settings
allows. Nothing is suggested below the floor of 20 closed cases an
investigation ran on. Runs Vigil did not start for an investigation, a run's
adjudication shadow, runs removed from History, and sample or probe data are
left out.

What a higher limit would have done cannot be replayed from history: a run cut
off does not say how much more it needed. The card says how many real attacks
were cut off and the most it could cost, not a promised result. The 30 labelled
investigation cases are not run here: a person runs the engine eval at both
values and records the result with ``python -m core.supervisory.eval_check``.

Only a stop at a cap at least today's counts, read from the cap the
investigation was started with, so the runs that led to a raise do not suggest
it again. A Case counts once however many investigations stopped on it. A run
the AI gateway refused, whose reason names no limit, is not counted, and
neither is a run a newer rerun has replaced; both lean toward not suggesting.

A hunt Vigil starts for an investigation runs under the same cost and runtime
caps, but at a cap it parks for a person rather than ending, so a hunt still
parked at one counts as stopped there: a refusal names the cap, and "spent its
allowance" is the cost cap. A hunt out of turns does not count, nor does a hunt's
call stop: its turns, and the call meter they raise, are the hunt's own, not a
Vigil setting. A park that later ends (extended, concluded by a person, or
ended past its time to live) no longer says which cap it met, so it does not
count. A cost stop whose stated ceiling is above the investigation's cap met a
person's extension, not the setting, and does not count. A refusal that states no
ceiling (runtime, or cost with nothing in flight) cannot be told apart, so a hunt
extended and parked again that way still counts. A root-cause trace's stop names no cap and its budgets are fixed in code,
so it does not count either.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select

from core.cases.closure import PERSON_CHANNELS, closure_says_real
from core.daemon_config import DaemonConfig
from core.storage.models import Finding
from core.storage.models.base import case_findings
from core.storage.models.case_entities import CaseClosureInfo
from core.storage.models.workflow import Investigation, WorkflowRun
from core.storage.unit_of_work import unit_of_work
from core.supervisory.feedback import (
    _NOT_REAL_DATA,
    FLOOR,
    WINDOW_DAYS,
    _investigation,
    _naive,
)
from core.workflows.workflows_service import HUNT_RUN_KIND

# What a hunt parked at its own cost check says, and what a refusal says before
# the token. Any other park reason is the hunt's own words and is not read.
_HUNT_COST_PARK = "spent its allowance"
_REFUSED = "the budget refused another iteration"
# The ceiling a cost stop states: "... of $5.00".
_STATED_CEILING = re.compile(r" of \$(\d+(?:\.\d+)?)")
_PARKED = "paused"
MISSED_MARGIN = 2
TABLE_ROWS = 8
_SHADOW_KIND = "adjudicate"


class Limit:
    def __init__(
        self,
        key: str,
        attr: str,
        token: str,
        label: str,
        cap: str,
        raise_to: Callable[[float], float],
        show: Callable[[float], str],
        prefix: str = "",
        suffix: str = "",
    ):
        self.key, self.attr, self.token, self.label = key, attr, token, label
        # The Investigation column holding the cap this run was started with.
        self.cap = cap
        self.raise_to, self.show = raise_to, show
        # How the value reads in a sentence: "$8.0", "2700 s", "40 calls".
        self.prefix, self.suffix = prefix, suffix

    def say(self, value: float) -> str:
        return f"{self.prefix}{self.show(value)}{self.suffix}"


# Half as high again, on Settings' own step, never past its maximum. Shown as
# the settings list shows the value.
LIMITS = (
    Limit(
        "investigate.max_cost_per_investigation",
        "max_cost_per_investigation",
        "cost_exhausted",
        "cost cap",
        "max_cost_usd",
        lambda v: min(100.0, math.ceil(v * 1.5 * 2) / 2),
        lambda v: str(float(v)),
        "$",
        "",
    ),
    Limit(
        "investigate.max_runtime_per_investigation",
        "max_runtime_per_investigation",
        "wall_exhausted",
        "runtime cap",
        "max_runtime_seconds",
        lambda v: min(86_400, math.ceil(v * 1.5 / 60) * 60),
        lambda v: f"{int(v)}",
        "",
        " s",
    ),
    Limit(
        "investigate.max_iterations_per_agent",
        "max_iterations_per_agent",
        "calls_exhausted",
        "call limit",
        "max_iterations",
        lambda v: min(500, math.ceil(v * 1.5)),
        lambda v: f"{int(v)}",
        "",
        " calls",
    ),
)


def check() -> list[dict[str, Any]]:
    """One proposal per limit that cut off real attacks, or none."""
    now = _naive(datetime.now(UTC))
    start = now - timedelta(days=WINDOW_DAYS)
    with unit_of_work() as session:
        labelled = _investigation(session, start, now)
    if labelled < FLOOR:
        return []
    orchestrator = DaemonConfig.from_env().orchestrator
    stopped = _stopped(start, now)
    proposals = []
    for limit in LIMITS:
        current = float(getattr(orchestrator, limit.attr))
        stops = _at(limit, current, stopped)
        real = sum(1 for s in stops if s[2] is True)
        target = limit.raise_to(current)
        if real < MISSED_MARGIN or target <= current:
            continue
        proposals.append(_proposal(limit, current, target, stops, labelled))
    return proposals


def _at(
    limit: Limit, current: float, stopped: list[dict[str, Any]]
) -> list[tuple[str, str, Any]]:
    """(case id, run id, closed as real) for each Case whose investigation stopped
    at ``limit`` while it was at least today's value. A stop at a lower cap says
    nothing about today's, so a raise is not suggested again on the runs that
    led to it. One row per Case, so one attack counts once."""
    seen: dict[str, tuple[str, str, Any]] = {}
    for row in stopped:
        if limit.token not in _caps_met(row):
            continue
        cap = row[limit.cap]
        if cap is None or float(cap) < current - 1e-9:
            continue
        seen.setdefault(row["case_id"], (row["case_id"], row["run_id"], row["real"]))
    return sorted(seen.values(), key=lambda s: (s[2] is not True, s[0]))


def _caps_met(row: dict[str, Any]) -> set[str]:
    """The limit tokens a stopped run's reason says it reached."""
    reason = row["reason"] or ""
    if row["kind"] != HUNT_RUN_KIND:
        return {limit.token for limit in LIMITS if limit.token in reason}
    if reason.startswith(_HUNT_COST_PARK):
        met = {"cost_exhausted"}
    elif reason.startswith(_REFUSED):
        met = {limit.token for limit in LIMITS if limit.token in reason}
    else:
        return set()
    met.discard("calls_exhausted")
    # A person's extension raises the hunt's own ceiling past the investigation's
    # cap, so a stop at the raised ceiling says nothing about the setting.
    stated = _STATED_CEILING.search(reason)
    cap = row["max_cost_usd"]
    if stated and cap is not None and float(stated.group(1)) > float(cap) + 0.005:
        met.discard("cost_exhausted")
    return met


def _stopped(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Each investigation whose last run in the window ended at a budget, or is a
    hunt parked at one, with its caps and how a person closed its Case. One read
    for every limit."""
    investigation_id = WorkflowRun.trigger_context["investigation_id"].astext
    run_kind = WorkflowRun.trigger_context["run_kind"].astext
    counted = (
        WorkflowRun.deleted_at.is_(None),
        func.coalesce(WorkflowRun.trigger_context["run_kind"].astext, "")
        != _SHADOW_KIND,
        investigation_id.isnot(None),
    )
    ranked = (
        select(
            WorkflowRun.run_id,
            WorkflowRun.outcome,
            WorkflowRun.status,
            WorkflowRun.reason,
            run_kind.label("kind"),
            investigation_id.label("investigation_id"),
            func.row_number()
            .over(
                partition_by=investigation_id,
                order_by=(WorkflowRun.started_at.desc(), WorkflowRun.run_id.desc()),
            )
            .label("n"),
        )
        # The latest run, finished or not: a newer rerun still running means
        # the stop is not the last word.
        .where(
            *counted,
            # No lower bound on the start: a run's start is set once, and a run
            # can park and be extended again and again before it ends in the
            # window. The check runs once an hour, so the wider scan is affordable.
            WorkflowRun.started_at < end,
        ).subquery()
    )
    closed = (
        select(CaseClosureInfo.closure_category)
        .where(
            CaseClosureInfo.case_id == Investigation.case_id,
            CaseClosureInfo.close_channel.in_(PERSON_CHANNELS),
        )
        .correlate(Investigation)
        .scalar_subquery()
    )
    query = (
        select(
            Investigation.case_id,
            ranked.c.run_id,
            ranked.c.reason,
            ranked.c.kind,
            Investigation.max_cost_usd,
            Investigation.max_runtime_seconds,
            Investigation.max_iterations,
            closed.label("category"),
        )
        .join(
            Investigation, Investigation.investigation_id == ranked.c.investigation_id
        )
        .join(WorkflowRun, WorkflowRun.run_id == ranked.c.run_id)
        .where(
            ranked.c.n == 1,
            or_(
                and_(
                    ranked.c.outcome == "budget_exhausted",
                    WorkflowRun.finished_at >= start,
                    WorkflowRun.finished_at < end,
                ),
                # Parked, so not finished. The investigation's last activity is
                # the last iteration before the park, and a park does not move it,
                # so a park that began in the window counts; a row left paused
                # when no worker ran to expire it ages out with it.
                and_(
                    ranked.c.kind == HUNT_RUN_KIND,
                    ranked.c.status == _PARKED,
                    Investigation.last_activity_at >= start,
                ),
            ),
            Investigation.case_id.isnot(None),
            ~_not_real_data(),
        )
    )
    with unit_of_work() as session:
        return [
            {
                "case_id": case_id,
                "run_id": run_id,
                "reason": reason,
                "kind": kind,
                "max_cost_usd": cost,
                "max_runtime_seconds": runtime,
                "max_iterations": iterations,
                "real": closure_says_real(category) if category else None,
            }
            for case_id, run_id, reason, kind, cost, runtime, iterations, category in (
                session.execute(query)
            )
        ]


def _not_real_data():
    """The investigation's Case, or a finding that started it, is a sample or probe."""
    in_case = (
        exists()
        .where(
            case_findings.c.case_id == Investigation.case_id,
            case_findings.c.finding_id == Finding.finding_id,
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        .correlate(Investigation)
    )
    trigger_ids = select(
        func.jsonb_array_elements_text(Investigation.trigger_ids)
    ).correlate(Investigation)
    started_by = (
        exists()
        .where(
            Finding.finding_id.in_(trigger_ids),
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        .correlate(Investigation)
    )
    return or_(in_case, started_by)


def _proposal(
    limit: Limit,
    current: float,
    target: float,
    stops: list[tuple[str, str, Any]],
    labelled: int,
) -> dict[str, Any]:
    real = sum(1 for s in stops if s[2] is True)
    shown_from, shown_to = limit.show(current), limit.show(target)
    extra = _extra(limit, current, target, len(stops))
    return {
        "fn": "investigation",
        "key": limit.key,
        "verb": "Raise",
        "from_value": shown_from,
        "to_value": shown_to,
        "widens_autonomy": False,
        "card": {
            "why": (
                f"{real} of {len(stops)} Cases whose investigation stopped at the "
                f"{limit.label} were later closed as real attacks"
            ),
            "evidence": f"{labelled} closed cases · {WINDOW_DAYS} days",
            "replay": f"{real} real attacks were cut off at {limit.say(current)}",
            "check": None,
            "plain_words": (
                f"Investigations keep reaching the {limit.label} before they finish, "
                f"and {real} of them turned out to be real attacks. Raising it to "
                f"{limit.say(target)} gives runs like those room to finish; {extra}."
            ),
            "table": {
                "title": f"Investigations stopped at the {limit.label}",
                "columns": [
                    {"label": "Case", "kind": "case"},
                    {"label": "Run", "kind": "mono"},
                    {"label": "Closed as", "kind": "text"},
                ],
                "rows": [
                    [
                        {"text": case_id},
                        {"text": run_id},
                        {"text": _judged(verdict), "attack": verdict is True},
                    ]
                    for case_id, run_id, verdict in stops[:TABLE_ROWS]
                ],
                "foot": (
                    f"{len(stops) - TABLE_ROWS} more · all from the last {WINDOW_DAYS} days"
                    if len(stops) > TABLE_ROWS
                    else f"All from the last {WINDOW_DAYS} days"
                ),
            },
            "replay_title": f"Last {WINDOW_DAYS} days at {limit.say(current)}",
            "replay_rows": [
                {
                    "label": f"Stopped at the {limit.label}",
                    "now": str(len(stops)),
                    "next": "not replayed",
                    "effect": "same",
                },
                {
                    "label": "Of those, closed as real attacks",
                    "now": str(real),
                    "next": "not replayed",
                    "effect": "same",
                },
            ],
        },
    }


def _extra(limit: Limit, current: float, target: float, stopped: int) -> str:
    """The most the raise could cost over a window like the last, said plainly."""
    delta = target - current
    if limit.attr == "max_cost_per_investigation":
        return (
            f"at most ${delta * stopped:g} more over {WINDOW_DAYS} days if every "
            "one of them used the whole new cap"
        )
    if limit.attr == "max_runtime_per_investigation":
        return f"each such run may take up to {int(delta // 60)} minutes longer"
    return f"each such run may make up to {int(delta)} more calls"


def _judged(verdict: Any) -> str:
    if verdict is True:
        return "real attack"
    if verdict is False:
        return "not real"
    return "no real or not-real verdict yet"
