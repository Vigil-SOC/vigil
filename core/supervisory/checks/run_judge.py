"""How each agent run was done, rolled up per run kind and model, by rules only.

Read-only. Every agent-run kind that investigates is judged: ``investigate``,
``hunt``, ``root_cause`` and ``compose``. ``adjudicate`` is a second look at
another run's verdict and is left out, and so are removed runs. So are runs on
sample alerts or probes, known by the run's Case (its investigation's, else the
one it was started on) or by the findings that started it. A run is judged only
once it answered, which each kind does its own way:

- investigate: ``completed``, or ``data_starved``, which still reports a verdict
  though it could not look and so is exactly what the tool flags are for;
- hunt: ``completed`` or ``data_starved``, the hunt ended and reported, every
  hypothesis settled or set inconclusive. A hunt out of turns or budget with a
  hypothesis open parks without ending, so it is not judged until it does;
- root_cause: ``completed``, the trace ended on a root cause, a false positive or
  inconclusive;
- compose: ``completed``, every phase ran.

Four flags, each a rule on what the run recorded:

- thin evidence: its final answer cites nothing (``citation_count`` 0) or its
  review found it ungrounded (``grounded`` false). Null is unsaid, never a flag.
  A hunt's evidence lives in its record, so for a hunt this says only that the
  account it ended on cites none of it. Only investigate and hunt report these;
  root_cause and compose never do, so the flag does not apply to them.
- gave up on tools: a tool call failed and no tool call after it succeeded.
- skipped steps: no tool call gathered evidence. Not judged for compose: a
  workflow's phases are written by an operator, and one may only act.
- ran out of turns: the turn cap ended it (``capped``), and it still answered.
  Only investigate and hunt report it; root_cause and compose do not apply.

Only the model's own calls are judged. For investigate and hunt that is the
run's own agent, whose calls carry no role (or ``agent``); a reviewer's or
critic's are not the run's. A root-cause trace journals its engine's fixed
searches as calls too (role ``code``); those are not the model's choice and count
on neither side. A tool call gathers evidence when it succeeded, is not one
of the run's own tools, and does not act: neither stored as ``reversible`` or
``irreversible`` in ``tool_effect_classes``, nor one of Vigil's tools that
changes something, by the same rule the playbook marks writes with. A run's own
tools count on neither side of the first two flags: they keep its notes or carry
its answer, and none looks at the estate. They are the kernel's hypothesis
record, playbook loader and answer tools, and a hunt's own tools (the hunt
plugin's HUNT_TOOLS and ``expand``). root_cause and compose have none beyond the
playbook loader.

A run's model is read by :mod:`core.supervisory.run_model`, the rule agent
agreement reads too. root_cause spends under the name of each move and compose
under each phase, so every role but the reader counts for them. A run that used
more than one model, or recorded none, is counted under no model, and the card
says how many such runs it left out.

Models are compared only within one run kind. Triage records no model per
finding and there is no response model setting, so neither is judged.

Every kind takes its model from the ``investigation`` component, so
:func:`check` can propose only ``model.investigation``. It proposes another model
only when the one the component resolves to now is clearly worse on some kind
and the other is clearly worse on none: clearly worse is, with ``FLOOR`` judged
runs each in that kind, a gap of ``GAP`` in rate and ``GAP_RUNS`` more flagged
runs than the other model's rate would give, on a flag that applies. Changing a
model needs platform.write, so the card cannot be applied from here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from functools import cache
from typing import Any

from sqlalchemy import String, and_, case, column, exists, func, or_, select, table

from core.storage.models import Finding
from core.storage.models.base import case_findings
from core.storage.models.workflow import Investigation, WorkflowRun
from core.storage.unit_of_work import unit_of_work
from core.supervisory.feedback import _NOT_REAL_DATA, _naive
from core.supervisory.run_model import AGENT_ONLY as _AGENT_ONLY
from core.supervisory.run_model import attributed
from core.supervisory.run_model import events as _events
from core.supervisory.run_model import ledger_of, run_model

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
KINDS = ("investigate", "hunt", "root_cause", "compose")
KIND_LABELS = {
    "investigate": "Investigations",
    "hunt": "Threat hunts",
    "root_cause": "Root-cause traces",
    "compose": "Workflows",
}
# What each kind reports: root_cause and compose never set citation_count,
# grounded or capped, so those two flags do not apply to them.
APPLIES = {
    "investigate": FLAGS,
    "hunt": FLAGS,
    "root_cause": ("gave_up", "skipped_steps"),
    # An operator's workflow may only act, so gathering nothing is no flag.
    "compose": ("gave_up",),
}
_ANSWERED = {
    "investigate": ("completed", "data_starved"),
    "hunt": ("completed", "data_starved"),
    "root_cause": ("completed",),
    "compose": ("completed",),
}
# A root-cause trace's own fixed searches, journaled as calls.
_ENGINE_ROLE = "code"
# A run's own tools. The kernel's: the hypothesis record, the playbook loader,
# and the tools a run answers through (conclude is journaled as a call like any
# other). A hunt's: services/agent/plugins/hunt/hooks.ts HUNT_TOOLS, and expand,
# which reads the run's own ledger.
_OWN_TOOLS = (
    "propose_hypothesis",
    "link_evidence",
    "open_question",
    "load_skill",
    "conclude",
    "submit_answer",
    "emit",
    "hunt_status",
    "record_evidence",
    "validate_hypothesis",
    "abandon_hypothesis",
    "handoff_ir",
    "close_question",
    "ask_operator",
    "conclude_hunt",
    "expand",
)
_ACTING = ("reversible", "irreversible")
# A run that ends in a window can have started any time before it: a hunt can
# park for up to 30 days, be extended and park again, with its start time set
# once. So runs are read by when they ended, and the start time bounds them only
# from above.

_tool_classes = table(
    "tool_effect_classes", column("tool_name", String), column("effect_class", String)
)


def rates(now: datetime | None = None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Per run kind and model, judged runs and how many raised each flag that
    applies; and per kind, the runs with no model."""
    end = _naive(now or datetime.now(UTC))
    runs = _judged_runs(end - timedelta(days=days), end, _vigil_writes())
    with unit_of_work() as session:
        rows = session.execute(
            select(
                runs.c.kind,
                runs.c.model,
                runs.c.models,
                func.count(),
                *(func.count().filter(runs.c[f]) for f in FLAGS),
            ).group_by(runs.c.kind, runs.c.model, runs.c.models)
        ).all()
    kinds: dict[str, dict[str, dict[str, int]]] = {k: {} for k in KINDS}
    unattributed = {k: 0 for k in KINDS}
    for kind, model, n_models, n, *flagged in rows:
        if attributed(model, n_models) is None:
            unattributed[kind] += n
            continue
        counts = dict(zip(FLAGS, flagged))
        kinds[kind][model] = {
            "runs": n,
            **{f: counts[f] for f in APPLIES[kind]},
        }
    return {"days": days, "kinds": kinds, "unattributed": unattributed}


@cache
def _vigil_writes() -> list[str]:
    """Vigil's own tools that change something, by the rule the playbook marks
    writes with: the tool's read-only hint, else a permission that is not a read.

    Read once per process: the set is fixed at import, and building Vigil's tool
    server the first time takes seconds."""
    from core.agents.tool_registry import TOOL_PERMISSIONS
    from core.integrations.mcp import in_process

    names = {tool["name"] for tool in in_process.list_tools()} | set(TOOL_PERMISSIONS)
    return sorted(
        name
        for name in names
        if (hint := in_process.changes(name)) is True
        or (
            hint is None
            and name in TOOL_PERMISSIONS
            and not TOOL_PERMISSIONS[name].endswith(".read")
        )
    )


def _judged_runs(start: datetime, end: datetime, writes: list[str]):
    """One row per judged run: its kind, model, how many models, and its flags."""
    kind = WorkflowRun.trigger_context["run_kind"].astext
    ledger = ledger_of(WorkflowRun.run_id)

    def models_call(e):
        """A call the model chose: the run's own agent's for investigate and hunt,
        and not one the engine made itself for the rest."""
        role = e.c.payload["role"].astext
        return case(
            (kind.in_(_AGENT_ONLY), or_(role.is_(None), role == "agent")),
            else_=role.is_distinct_from(_ENGINE_ROLE),
        )

    def events(alias: str, event_kind: str):
        e = _events.alias(alias)
        return e, and_(e.c.run_id == ledger, e.c.kind == event_kind)

    model, models = run_model(kind, ledger)

    failed, own_failed = events("judge_failed", "call")
    later, own_later = events("judge_later", "call")
    gathered, own_gathered = events("judge_gathered", "call")
    gave_up = exists().where(
        own_failed,
        models_call(failed),
        _ok(failed).is_(False),
        _tool(failed).notin_(_OWN_TOOLS),
        # Explicit: nested two deep, the run and the failed call are outer rows.
        ~exists()
        .where(
            own_later,
            models_call(later),
            later.c.seq > failed.c.seq,
            _ok(later).is_(True),
            _tool(later).notin_(_OWN_TOOLS),
        )
        .correlate_except(later),
    )
    acting = select(_tool_classes.c.tool_name).where(
        _tool_classes.c.effect_class.in_(_ACTING)
    )
    skipped = and_(
        kind != "compose",
        ~exists().where(
            own_gathered,
            models_call(gathered),
            _ok(gathered).is_(True),
            _tool(gathered).notin_(_OWN_TOOLS),
            _tool(gathered).notin_(acting),
            _tool(gathered).notin_(writes),
        ),
    )
    reports = kind.in_(_AGENT_ONLY)
    thin = and_(
        reports,
        or_(
            func.coalesce(WorkflowRun.citation_count == 0, False),
            func.coalesce(WorkflowRun.grounded.is_(False), False),
        ),
    )
    capped = and_(reports, func.coalesce(WorkflowRun.capped.is_(True), False))
    answered = or_(
        *(
            and_(kind == k, WorkflowRun.outcome.in_(outcomes))
            for k, outcomes in _ANSWERED.items()
        )
    )
    return (
        select(
            kind.label("kind"),
            model.label("model"),
            models.label("models"),
            thin.label("thin_evidence"),
            gave_up.label("gave_up"),
            skipped.label("skipped_steps"),
            capped.label("out_of_turns"),
        )
        .outerjoin(
            Investigation,
            Investigation.investigation_id
            == WorkflowRun.trigger_context["investigation_id"].astext,
        )
        .where(
            WorkflowRun.started_at < end,
            WorkflowRun.finished_at >= start,
            WorkflowRun.finished_at < end,
            WorkflowRun.deleted_at.is_(None),
            kind.in_(KINDS),
            answered,
            ~_not_real_data(),
        )
        .subquery("judged_runs")
    )


def _ok(e):
    return e.c.payload["ok"].as_boolean()


def _tool(e):
    return e.c.payload["tool"].astext


def _not_real_data():
    """The run's Case, or a finding that started it, is a sample or probe. The Case
    is its investigation's, else the one it was started on; the findings are its
    investigation's triggers and the one it was started on."""
    context = WorkflowRun.trigger_context
    case_id = func.coalesce(Investigation.case_id, context["case_id"].astext)
    in_case = (
        exists()
        .where(
            case_findings.c.case_id == case_id,
            case_findings.c.finding_id == Finding.finding_id,
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        .correlate_except(case_findings, Finding)
    )
    trigger_ids = select(
        func.jsonb_array_elements_text(Investigation.trigger_ids)
    ).correlate(Investigation)
    started_by = (
        exists()
        .where(
            or_(
                Finding.finding_id.in_(trigger_ids),
                Finding.finding_id == context["finding_id"].astext,
            ),
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        .correlate_except(Finding)
    )
    return or_(in_case, started_by)


def check() -> list[dict[str, Any]]:
    """At most one proposal: a better model for the investigation component, or none."""
    current = _current_model()
    if current is None:
        return []
    measured = rates()
    kinds = measured["kinds"]
    others = {m for by_model in kinds.values() for m in by_model} - {current}
    offers = []
    for other in sorted(others):
        better, worse = [], []
        for kind, by_model in kinds.items():
            a, b = by_model.get(current), by_model.get(other)
            if a is None or b is None or min(a["runs"], b["runs"]) < FLOOR:
                continue
            if _clearly_worse(a, b, kind):
                better.append(kind)
            if _clearly_worse(b, a, kind):
                worse.append(kind)
        if better and not worse:
            total = sum(_rate(kinds[k][other], f) for k in better for f in APPLIES[k])
            offers.append((-len(better), total, other, better))
    if not offers:
        return []
    _, _, other, better = min(offers)
    return [
        _proposal(
            current, other, better, kinds, measured["days"], measured["unattributed"]
        )
    ]


def _current_model() -> str | None:
    """The model a run is handed now, as the run itself records it."""
    from core.llm import target

    resolved = target.resolve_component(COMPONENT)
    return resolved[1] if resolved else None


def _rate(stats: dict[str, int], flag: str) -> Fraction:
    """Exact, so a gap of exactly GAP counts: 6/20 - 4/20 is 0.1, not 0.0999…."""
    return Fraction(stats[flag], stats["runs"])


def _clearly_worse(a: dict[str, int], b: dict[str, int], kind: str) -> bool:
    """On a flag that applies to ``kind``, ``a`` is GAP and GAP_RUNS worse than ``b``."""
    return any(_gap(a, b, f) for f in APPLIES[kind])


def _gap(a: dict[str, int], b: dict[str, int], flag: str) -> bool:
    expected = _rate(b, flag) * a["runs"]
    return _rate(a, flag) - _rate(b, flag) >= GAP and a[flag] - expected >= GAP_RUNS


def _pct(stats: dict[str, int], flag: str) -> str:
    return f"{float(_rate(stats, flag)):.0%}"


def _proposal(
    current: str,
    other: str,
    better: list[str],
    kinds: dict[str, dict[str, dict[str, int]]],
    days: int,
    unattributed: dict[str, int],
) -> dict[str, Any]:
    lead = better[0]
    now, then = kinds[lead][current], kinds[lead][other]
    flags = [f for f in APPLIES[lead] if _gap(now, then, f)]
    worst = max(flags, key=lambda f: _rate(now, f) - _rate(then, f))
    named = ", ".join(KIND_LABELS[k].lower() for k in better)
    why = (
        f"{KIND_LABELS[lead]}: {LABELS[worst].lower()} in {_pct(now, worst)} of "
        f"{current}'s runs, {_pct(then, worst)} of {other}'s"
    )
    shown = [
        k
        for k in KINDS
        if current in kinds[k]
        and other in kinds[k]
        and min(kinds[k][current]["runs"], kinds[k][other]["runs"]) >= FLOOR
    ]
    judged = sum(kinds[k][m]["runs"] for k in shown for m in (current, other))
    left_out = sum(unattributed[k] for k in shown)
    plain = (
        f"Over the last {days} days {other} raised fewer of these flags than "
        f"{current} on {named}, and clearly more on none of the kinds where both "
        f"have {FLOOR} runs. Investigations, hunts, root-cause traces and workflows "
        "all use this one setting. The flags are rules on what each run recorded, "
        "not a judge of its verdict: change the model in AI settings if you agree."
    )
    if left_out:
        plain += (
            f" {left_out} runs of these kinds recorded no single model and are "
            "left out."
        )
    return {
        "fn": "investigation",
        "key": KEY,
        "verb": "Change",
        "from_value": current,
        "to_value": other,
        "widens_autonomy": False,
        "card": {
            "why": why,
            "evidence": f"{judged} agent runs · {days} days",
            "replay": "; ".join(
                f"{KIND_LABELS[k].lower()}: "
                + ", ".join(
                    f"{LABELS[f].lower()} {_pct(kinds[k][current], f)} → "
                    f"{_pct(kinds[k][other], f)}"
                    for f in APPLIES[k]
                    if _gap(kinds[k][current], kinds[k][other], f)
                )
                for k in better
            ),
            "check": None,
            "plain_words": plain,
            "table": {
                "title": "Agent runs flagged, by kind and model",
                "columns": [
                    {"label": "Kind · flag", "kind": "text"},
                    {"label": current, "kind": "mono"},
                    {"label": other, "kind": "mono"},
                ],
                "rows": [
                    [
                        {"text": f"{KIND_LABELS[k]} · {LABELS[f]}"},
                        {"text": _of(kinds[k][current], f)},
                        {"text": _of(kinds[k][other], f)},
                    ]
                    for k in shown
                    for f in APPLIES[k]
                ],
                "foot": (
                    f"All from the last {days} days; kinds are compared only where "
                    f"both models have {FLOOR} runs"
                ),
            },
            "replay_title": f"{KIND_LABELS[lead]}, last {days} days: "
            f"{current} and {other}",
            "replay_rows": [
                {
                    "label": LABELS[f],
                    "now": _pct(now, f),
                    "next": _pct(then, f),
                    "effect": _effect(now, then, f),
                }
                for f in APPLIES[lead]
            ],
        },
    }


def _of(stats: dict[str, int], flag: str) -> str:
    return f"{stats[flag]} of {stats['runs']}"


def _effect(now: dict[str, int], then: dict[str, int], flag: str) -> str:
    if _gap(now, then, flag):
        return "better"
    if _gap(then, now, flag):
        return "worse"
    return "same"
