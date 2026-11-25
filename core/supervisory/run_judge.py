"""How each investigation run was done, rolled up per model, by rules only.

Read-only. A judged run is an investigation's own ``investigate`` run that
ended in the window with an answer: ``completed``, or ``data_starved``, which
still reports a verdict though it could not look and so is exactly what the
tool flags are for. Other kinds (hunts, compose, root cause) answer through
tools of their own and are not compared here. Shadow runs, removed runs, and
investigations of sample alerts or probes (through their Case or the findings
that started them) are left out. Four flags, each a rule on what the run
recorded:

- thin evidence: its report cites nothing (``citation_count`` 0) or its review
  found it ungrounded (``grounded`` false). Null is unsaid, never a flag.
- gave up on tools: a tool call failed and no tool call after it succeeded. The
  kernel's own tools count on neither side, so the conclude call is not a recovery.
- skipped steps: no tool call gathered evidence. A call gathers evidence when it
  succeeded, is not one of the kernel's own tools (the hypothesis record, the
  playbook loader, and conclude and the other tools a run answers through), and
  its tool is not stored as one that acts
  (``reversible`` or ``irreversible`` in ``tool_effect_classes``). Vigil's own
  tools carry no class and count as reads: an investigation run takes no action.
- ran out of turns: the turn cap ended it (``capped``), and it still answered.

A run's model is the one its own agent calls used, as each call's spend event
on the agent ledger records it (role ``agent``; the reviewer's calls are not the
run's). A run whose agent calls used more than one model, or none recorded, is
counted under no model.

Only investigation is judged. Triage records no model per finding and makes no
tool calls, and there is no response model setting: response proposals come out
of investigation runs.

:func:`check` proposes a ``model.investigation`` change only when the model the
investigation component resolves to now is clearly worse than another on these
runs: both with ``FLOOR`` judged runs, and on some flag a gap of ``GAP`` (in
rate) and ``GAP_RUNS`` (more flagged runs than the other model's rate would give
it), with no flag clearly the other way. Changing a model needs platform.write,
so the card cannot be applied from here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    cast,
    column,
    exists,
    func,
    or_,
    select,
    table,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.storage.models import Finding
from core.storage.models.base import case_findings
from core.storage.models.workflow import Investigation, WorkflowRun
from core.storage.unit_of_work import unit_of_work
from core.supervisory.feedback import _NOT_REAL_DATA, _naive

WINDOW_DAYS = 30
FLOOR = 20
GAP = Fraction(1, 10)
GAP_RUNS = 2
KEY = "model.investigation"
COMPONENT = "investigation"
FLAGS = ("thin_evidence", "gave_up", "skipped_steps", "out_of_turns")
LABELS = {
    "thin_evidence": "Answered on thin evidence",
    "gave_up": "Gave up on a failing tool",
    "skipped_steps": "Answered without gathering evidence",
    "out_of_turns": "Ran out of turns and still answered",
}
# The kernel's own tools: the hypothesis record, the playbook loader, and the
# tools a run answers through (conclude is journaled as a call like any other).
# None of them looks at the estate.
_BOOKKEEPING = (
    "propose_hypothesis",
    "link_evidence",
    "open_question",
    "load_skill",
    "conclude",
    "submit_answer",
    "emit",
)
_ACTING = ("reversible", "irreversible")
# Shadow runs are "adjudicate", so naming the kind leaves them out too.
_RUN_KIND = "investigate"
_ANSWERED = ("completed", "data_starved")
_UUID = "^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$"

_events = table(
    "agent_events",
    column("run_id", UUID(as_uuid=False)),
    column("seq", Integer),
    column("kind", String),
    column("payload", JSONB),
)
_tool_classes = table(
    "tool_effect_classes", column("tool_name", String), column("effect_class", String)
)


def rates(now: datetime | None = None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Per model, judged runs and how many raised each flag; and runs with no model."""
    end = _naive(now or datetime.now(UTC))
    runs = _judged_runs(end - timedelta(days=days), end)
    with unit_of_work() as session:
        rows = session.execute(
            select(
                runs.c.model,
                runs.c.models,
                func.count(),
                *(func.count().filter(runs.c[f]) for f in FLAGS),
            ).group_by(runs.c.model, runs.c.models)
        ).all()
    models: dict[str, dict[str, int]] = {}
    unattributed = 0
    for model, n_models, n, *flagged in rows:
        if n_models != 1 or model is None:
            unattributed += n
            continue
        models[model] = {"runs": n, **dict(zip(FLAGS, flagged))}
    return {"days": days, "models": models, "unattributed": unattributed}


def _judged_runs(start: datetime, end: datetime):
    """One row per judged run: its model, how many models it used, and its flags."""
    ledger = case(
        (
            WorkflowRun.run_id.op("~*")(_UUID),
            cast(WorkflowRun.run_id, UUID(as_uuid=False)),
        ),
        else_=None,
    )

    def events(alias: str, kind: str):
        e = _events.alias(alias)
        return e, and_(e.c.run_id == ledger, e.c.kind == kind)

    spend, own_spend = events("judge_spend", "spend")
    agent_spend = and_(own_spend, spend.c.payload["role"].astext == "agent")
    model_of = spend.c.payload["model_id"].astext
    models = (
        select(func.count(func.distinct(model_of))).where(agent_spend).scalar_subquery()
    )
    model = select(func.min(model_of)).where(agent_spend).scalar_subquery()

    failed, own_failed = events("judge_failed", "call")
    later, own_later = events("judge_later", "call")
    gathered, own_gathered = events("judge_gathered", "call")
    gave_up = exists().where(
        own_failed,
        _ok(failed).is_(False),
        _tool(failed).notin_(_BOOKKEEPING),
        # Explicit: nested two deep, the run and the failed call are outer rows.
        ~exists()
        .where(
            own_later,
            later.c.seq > failed.c.seq,
            _ok(later).is_(True),
            _tool(later).notin_(_BOOKKEEPING),
        )
        .correlate_except(later),
    )
    acting = select(_tool_classes.c.tool_name).where(
        _tool_classes.c.effect_class.in_(_ACTING)
    )
    skipped = ~exists().where(
        own_gathered,
        _ok(gathered).is_(True),
        _tool(gathered).notin_(_BOOKKEEPING),
        _tool(gathered).notin_(acting),
    )
    thin = or_(
        func.coalesce(WorkflowRun.citation_count == 0, False),
        func.coalesce(WorkflowRun.grounded.is_(False), False),
    )
    return (
        select(
            model.label("model"),
            models.label("models"),
            thin.label("thin_evidence"),
            gave_up.label("gave_up"),
            skipped.label("skipped_steps"),
            func.coalesce(WorkflowRun.capped.is_(True), False).label("out_of_turns"),
        )
        .join(
            Investigation,
            Investigation.investigation_id
            == WorkflowRun.trigger_context["investigation_id"].astext,
        )
        .where(
            WorkflowRun.finished_at >= start,
            WorkflowRun.finished_at < end,
            WorkflowRun.deleted_at.is_(None),
            WorkflowRun.outcome.in_(_ANSWERED),
            WorkflowRun.trigger_context["run_kind"].astext == _RUN_KIND,
            ~_not_real_data(),
        )
        .subquery("judged_runs")
    )


def _ok(e):
    return e.c.payload["ok"].as_boolean()


def _tool(e):
    return e.c.payload["tool"].astext


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


def check() -> list[dict[str, Any]]:
    """At most one proposal: a better model for investigation runs, or none."""
    current = _current_model()
    if current is None:
        return []
    measured = rates()
    models = measured["models"]
    now = models.get(current)
    if now is None or now["runs"] < FLOOR:
        return []
    better = [
        (sum(_rate(stats, f) for f in FLAGS), name, stats)
        for name, stats in models.items()
        if name != current
        and stats["runs"] >= FLOOR
        and _clearly_worse(now, stats)
        and not _clearly_worse(stats, now)
    ]
    if not better:
        return []
    _, name, stats = min(better)
    return [_proposal(current, now, name, stats, measured["days"])]


def _current_model() -> str | None:
    """The model an investigation run is handed now, as the run itself records it."""
    from core.llm import target

    resolved = target.resolve_component(COMPONENT)
    return resolved[1] if resolved else None


def _rate(stats: dict[str, int], flag: str) -> Fraction:
    """Exact, so a gap of exactly GAP counts: 6/20 - 4/20 is 0.1, not 0.0999…."""
    return Fraction(stats[flag], stats["runs"])


def _clearly_worse(a: dict[str, int], b: dict[str, int]) -> bool:
    """On some flag, ``a`` is GAP worse in rate and GAP_RUNS worse in runs than ``b``."""
    return any(_gap(a, b, f) for f in FLAGS)


def _gap(a: dict[str, int], b: dict[str, int], flag: str) -> bool:
    expected = _rate(b, flag) * a["runs"]
    return _rate(a, flag) - _rate(b, flag) >= GAP and a[flag] - expected >= GAP_RUNS


def _pct(stats: dict[str, int], flag: str) -> str:
    return f"{float(_rate(stats, flag)):.0%}"


def _proposal(
    current: str, now: dict[str, int], other: str, then: dict[str, int], days: int
) -> dict[str, Any]:
    worst = max(FLAGS, key=lambda f: _rate(now, f) - _rate(then, f))
    why = (
        f"{LABELS[worst]}: {_pct(now, worst)} of {current}'s investigation runs, "
        f"{_pct(then, worst)} of {other}'s"
    )
    plain = (
        f"On investigation runs over the last {days} days, {other} raised fewer of "
        f"these flags than {current}. The flags are rules on what each run recorded, "
        "not a judge of its verdict: change the model in AI settings if you agree."
    )
    gaps = [f for f in FLAGS if _gap(now, then, f)]
    return {
        "fn": "investigation",
        "key": KEY,
        "verb": "Change",
        "from_value": current,
        "to_value": other,
        "widens_autonomy": False,
        "card": {
            "why": why,
            "evidence": (
                f"{now['runs'] + then['runs']} investigation runs · {days} days"
            ),
            "replay": "; ".join(
                f"{LABELS[f].lower()} {_pct(now, f)} → {_pct(then, f)}" for f in gaps
            ),
            "check": None,
            "plain_words": plain,
            "table": {
                "title": "Investigation runs flagged, by model",
                "columns": [
                    {"label": "Flag", "kind": "text"},
                    {"label": current, "kind": "mono"},
                    {"label": other, "kind": "mono"},
                ],
                "rows": [
                    [
                        {"text": LABELS[f]},
                        {"text": f"{now[f]} of {now['runs']}"},
                        {"text": f"{then[f]} of {then['runs']}"},
                    ]
                    for f in FLAGS
                ],
                "foot": f"All from the last {days} days",
            },
            "replay_title": f"Last {days} days: {current} and {other}",
            "replay_rows": [
                {
                    "label": LABELS[f],
                    "now": _pct(now, f),
                    "next": _pct(then, f),
                    "effect": _effect(now, then, f),
                }
                for f in FLAGS
            ],
        },
    }


def _effect(now: dict[str, int], then: dict[str, int], flag: str) -> str:
    if _gap(now, then, flag):
        return "better"
    if _gap(then, now, flag):
        return "worse"
    return "same"
