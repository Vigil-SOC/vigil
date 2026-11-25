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
investigation cases are not run against the new value here.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, select

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
        raise_to: Callable[[float], float],
        show: Callable[[float], str],
    ):
        self.key, self.attr, self.token, self.label = key, attr, token, label
        self.raise_to, self.show = raise_to, show


# Half as high again, on Settings' own step, never past its maximum.
LIMITS = (
    Limit(
        "investigate.max_cost_per_investigation",
        "max_cost_per_investigation",
        "cost_exhausted",
        "cost cap",
        lambda v: min(100.0, math.ceil(v * 1.5 * 2) / 2),
        lambda v: f"{v:g}",
    ),
    Limit(
        "investigate.max_runtime_per_investigation",
        "max_runtime_per_investigation",
        "wall_exhausted",
        "runtime cap",
        lambda v: min(86_400, math.ceil(v * 1.5 / 60) * 60),
        lambda v: f"{int(v)}",
    ),
    Limit(
        "investigate.max_iterations_per_agent",
        "max_iterations_per_agent",
        "calls_exhausted",
        "call limit",
        lambda v: min(500, math.ceil(v * 1.5)),
        lambda v: f"{int(v)}",
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
    proposals = []
    for limit in LIMITS:
        current = float(getattr(orchestrator, limit.attr))
        stops = _stops(limit.token, start, now)
        real = sum(1 for s in stops if s[2] is True)
        target = limit.raise_to(current)
        if real < MISSED_MARGIN or target <= current:
            continue
        proposals.append(_proposal(limit, current, target, stops, labelled))
    return proposals


def _stops(token: str, start: datetime, end: datetime) -> list[tuple[str, str, Any]]:
    """(case id, run id, closed as real: True, False, or None when not closed by a
    person) for each investigation whose last run in the window stopped at the limit."""
    investigation_id = WorkflowRun.trigger_context["investigation_id"].astext
    latest = (
        select(
            investigation_id.label("investigation_id"),
            func.max(WorkflowRun.finished_at).label("finished_at"),
        )
        .where(
            WorkflowRun.finished_at >= start,
            WorkflowRun.finished_at < end,
            WorkflowRun.deleted_at.is_(None),
            func.coalesce(WorkflowRun.trigger_context["run_kind"].astext, "")
            != _SHADOW_KIND,
            investigation_id.isnot(None),
        )
        .group_by(investigation_id)
        .subquery()
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
    rows = (
        select(Investigation.case_id, WorkflowRun.run_id, closed.label("category"))
        .select_from(
            WorkflowRun.__table__.join(
                latest,
                and_(
                    latest.c.investigation_id == investigation_id,
                    latest.c.finished_at == WorkflowRun.finished_at,
                ),
            ).join(Investigation, Investigation.investigation_id == investigation_id)
        )
        .where(
            WorkflowRun.outcome == "budget_exhausted",
            WorkflowRun.reason.contains(token, autoescape=True),
            Investigation.case_id.isnot(None),
            ~_sample_case(),
        )
    )
    with unit_of_work() as session:
        return [
            (case_id, run_id, closure_says_real(category) if category else None)
            for case_id, run_id, category in session.execute(rows)
        ]


def _sample_case():
    return (
        exists()
        .where(
            case_findings.c.case_id == Investigation.case_id,
            case_findings.c.finding_id == Finding.finding_id,
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        .correlate(Investigation)
    )


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
    ordered = sorted(stops, key=lambda s: (s[2] is not True, s[0]))
    return {
        "fn": "investigation",
        "key": limit.key,
        "verb": "Raise",
        "from_value": shown_from,
        "to_value": shown_to,
        "widens_autonomy": False,
        "card": {
            "why": (
                f"{real} of {len(stops)} investigations stopped at the {limit.label} "
                "were later closed as real attacks"
            ),
            "evidence": f"{labelled} closed cases · {WINDOW_DAYS} days",
            "replay": f"{real} real attacks were cut off at {shown_from}",
            "check": None,
            "plain_words": (
                f"Investigations keep reaching the {limit.label} before they finish, "
                f"and {real} of them turned out to be real attacks. Raising it to "
                f"{shown_to} gives runs like those room to finish; {extra}."
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
                    for case_id, run_id, verdict in ordered[:TABLE_ROWS]
                ],
                "foot": (
                    f"{len(stops) - TABLE_ROWS} more · all from the last {WINDOW_DAYS} days"
                    if len(stops) > TABLE_ROWS
                    else f"All from the last {WINDOW_DAYS} days"
                ),
            },
            "replay_title": f"Last {WINDOW_DAYS} days at {shown_from}",
            "replay_rows": [
                {
                    "label": f"Stopped at the {limit.label}",
                    "now": str(len(stops)),
                    "next": "fewer",
                    "effect": "better",
                },
                {
                    "label": "Of those, real attacks",
                    "now": str(real),
                    "next": "given room to finish",
                    "effect": "better",
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
    return "not closed by a person"
