"""One autonomy ceiling (#2918).

The stored row is ``autonomy.ceiling``. Read is always on. Write of Vigil's
own records and propose are on unless ``stopped``. The act flags are off until a person
raises them. ``DAEMON_FORCE_APPROVAL`` and ``DAEMON_AUTO_RESPONSE`` can only
force the effective act flags off. Four-eyes is enforced where an approval
is recorded, in ``ApprovalService.approve_action`` (#2932).
An unbound caller is not denied here; the HTTP gates are the permission
checks on the routes, and ``/internal/tools/invoke`` refuses a tool call that
names nobody.

Each outside tool also has one stored effect class (#3304), read here by every
caller that decides whether a tool call needs an approval.

Each investigate-kind workflow may carry its own tier in the same row (#3624):
``act`` or ``assist`` replaces the fleet's ``act_reversible`` for its runs. A
person raises it with an agreement floor; Vigil drops it to ``assist`` when a
person's closure takes its agreement below that floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import event, select

from core.config import Settings, get_settings
from core.storage import config_service as config_store
from core.storage.models import SystemConfig, ToolEffectClass, WorkflowRun
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

CEILING_KEY = "autonomy.ceiling"
ACT = "act"
ASSIST = "assist"
TIERS = (ACT, ASSIST)
AGREEMENT_WINDOW = timedelta(days=30)


def _no_workflows() -> Mapping[str, dict]:
    return MappingProxyType({})


@dataclass(frozen=True)
class Ceiling:
    act_reversible: bool = False
    act_irreversible: bool = False
    stopped: bool = False
    four_eyes: bool = False
    # workflow id -> {tier, floor, evidence, by, at}. Absent follows the fleet.
    workflows: Mapping[str, dict] = field(default_factory=_no_workflows)


@dataclass(frozen=True)
class CeilingView:
    """What the console API returns.

    The act flags and ``four_eyes`` are the stored ceiling. Effective act is
    off while ``stopped`` or ``environment_locked``. Read stays on while
    stopped; write and propose follow ``stopped`` only.
    """

    act_reversible: bool
    act_irreversible: bool
    stopped: bool
    read: bool
    write: bool
    propose: bool
    environment_locked: bool
    four_eyes: bool


class CeilingRefusal(Exception):
    """A ceiling write the caller is not allowed to store."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CeilingWriteError(Exception):
    """The ceiling row was not stored."""


def _flag(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def environment_locks_act(settings: Optional[Settings] = None) -> bool:
    """Force-approval, or auto-response turned off, beats a stored act flag.

    Neither variable can turn an act flag on. ``DAEMON_AUTO_RESPONSE`` defaults
    to true, which is not a grant.
    """
    settings = get_settings() if settings is None else settings
    return bool(settings.daemon_force_approval) or not bool(
        settings.daemon_auto_response
    )


def _workflow_entries(value: Any) -> Mapping[str, dict]:
    """Entries with a known tier; anything else follows the fleet."""
    if not isinstance(value, dict):
        return _no_workflows()
    return MappingProxyType(
        {
            str(wid): dict(entry)
            for wid, entry in value.items()
            if isinstance(entry, dict) and entry.get("tier") in TIERS
        }
    )


def _parse(value: Any) -> Ceiling:
    if not isinstance(value, dict):
        return Ceiling()
    return Ceiling(
        act_reversible=_flag(value.get("act_reversible")),
        act_irreversible=_flag(value.get("act_irreversible")),
        stopped=_flag(value.get("stopped")),
        four_eyes=_flag(value.get("four_eyes")),
        workflows=_workflow_entries(value.get("workflows")),
    )


def read_stored_ceiling() -> tuple[Ceiling, bool]:
    """The stored row. ``(defaults, False)`` when the key is absent.

    Raises when the read fails. A missing row is the seeded off state, not Act.
    """
    value = config_store.get_config_service().read_system_config(CEILING_KEY)
    if value is None:
        return Ceiling(), False
    return _parse(value), True


def read_ceiling() -> Ceiling:
    stored, _found = read_stored_ceiling()
    return stored


def try_read_stored_ceiling() -> Optional[tuple[Ceiling, bool]]:
    """The stored row, or None when the read fails (act flags stay off)."""
    try:
        return read_stored_ceiling()
    except Exception as exc:  # noqa: BLE001
        logger.error("autonomy ceiling not read; act flags stay off: %s", exc)
        return None


def workflow_tier(stored: Ceiling, workflow_id: Optional[str]) -> Optional[str]:
    entry = stored.workflows.get(workflow_id) if workflow_id else None
    return entry.get("tier") if entry else None


def stored_act(
    stored: Ceiling, effect: Optional[str], workflow_id: Optional[str]
) -> bool:
    """Whether the stored row lets a run of ``workflow_id`` take ``effect``.

    Stop and the environment lock are not read here. A workflow's tier replaces
    the fleet's ``act_reversible``; ``irreversible`` always needs the fleet's
    ``act_irreversible`` and never runs under ``assist``.
    """
    tier = workflow_tier(stored, workflow_id)
    if effect == REVERSIBLE:
        return stored.act_reversible if tier is None else tier == ACT
    if effect == IRREVERSIBLE:
        return stored.act_irreversible and tier != ASSIST
    return False


def effective_act(
    stored: Ceiling,
    effect: Optional[str],
    workflow_id: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> bool:
    """The one answer to "may this run act": stop and the lock beat every tier."""
    if stored.stopped or environment_locks_act(settings):
        return False
    return stored_act(stored, effect, workflow_id)


def workflow_of(run_id: Optional[str]) -> Optional[str]:
    """The workflow behind a run. None follows the fleet, a failed read too."""
    if not run_id:
        return None
    try:
        with unit_of_work() as session:
            return session.execute(
                select(WorkflowRun.workflow_id).where(WorkflowRun.run_id == run_id)
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        logger.error("workflow of run %s not read; fleet flags apply: %s", run_id, exc)
        return None


def ceiling_view(stored: Ceiling, settings: Optional[Settings] = None) -> CeilingView:
    return CeilingView(
        act_reversible=stored.act_reversible,
        act_irreversible=stored.act_irreversible,
        stopped=stored.stopped,
        read=True,
        write=not stored.stopped,
        propose=not stored.stopped,
        environment_locked=environment_locks_act(settings),
        four_eyes=stored.four_eyes,
    )


def execution_held() -> bool:
    """No outside action runs while stopped, or while the ceiling cannot be read."""
    try:
        stored, _found = read_stored_ceiling()
    except Exception as exc:  # noqa: BLE001
        logger.error("autonomy ceiling not read; holding execution: %s", exc)
        return True
    return stored.stopped


def _dump(ceiling: Ceiling) -> dict[str, Any]:
    return {
        "act_reversible": ceiling.act_reversible,
        "act_irreversible": ceiling.act_irreversible,
        "stopped": ceiling.stopped,
        "four_eyes": ceiling.four_eyes,
        "workflows": {wid: dict(e) for wid, e in ceiling.workflows.items()},
    }


def _store(user_id: str, reason: str, **fields: Any) -> Ceiling:
    """Write only ``fields`` over the row as it is now; the rest stays as stored."""
    return _locked_update(lambda current: (replace(current, **fields), reason), user_id)


def _raises(current: bool, requested: Optional[bool]) -> bool:
    return requested is True and not current


def update_ceiling(
    *,
    user_id: str,
    caller_is_service: bool,
    act_reversible: Optional[bool] = None,
    act_irreversible: Optional[bool] = None,
    four_eyes: Optional[bool] = None,
    locked: Optional[bool] = None,
) -> Ceiling:
    """Store a person's edit of the ceiling. Does not execute anything.

    A service account may lower an act flag. It may not raise one, resume,
    or switch four-eyes. An act flag is not stored on while the environment
    has locked them off.
    """
    if locked is None:
        locked = environment_locks_act()
    current = read_ceiling()
    wants_act_on = act_reversible is True or act_irreversible is True
    if locked and wants_act_on:
        raise CeilingRefusal(409, "The environment wins; Act was not saved.")
    if caller_is_service:
        if four_eyes is not None and four_eyes != current.four_eyes:
            raise CeilingRefusal(403, "A service account cannot switch four-eyes")
        if _raises(current.act_reversible, act_reversible) or _raises(
            current.act_irreversible, act_irreversible
        ):
            raise CeilingRefusal(403, "A service account cannot raise the ceiling")
    edit = {
        name: value
        for name, value in (
            ("act_reversible", act_reversible),
            ("act_irreversible", act_irreversible),
            ("four_eyes", four_eyes),
        )
        if value is not None
    }
    if replace(current, **edit) == current:
        return current
    return _store(user_id, "Autonomy ceiling updated", **edit)


def stop_ceiling(user_id: str) -> Ceiling:
    """Set ``stopped``. Stored act flags stay, so a later resume can restore them.

    Effective act flags are off while stopped. This does not execute anything.
    """
    current = read_ceiling()
    if current.stopped:
        return current
    return _store(user_id, "Autonomy stopped", stopped=True)


def resume_ceiling(*, user_id: str, caller_is_service: bool) -> Ceiling:
    """Clear ``stopped``. Act flags already lowered during the stop stay off.

    Executes nothing. A service account cannot resume.
    """
    if caller_is_service:
        raise CeilingRefusal(403, "A service account cannot resume")
    current = read_ceiling()
    if not current.stopped:
        return current
    return _store(user_id, "Autonomy resumed", stopped=False)


def raises_workflow(current: Ceiling, workflow_id: str, after: Ceiling) -> bool:
    """A raise is anything that lets the workflow act where it could not.

    Stop and the environment lock are ignored: they hold every tier alike.
    """
    return any(
        stored_act(after, effect, workflow_id)
        and not stored_act(current, effect, workflow_id)
        for effect in (REVERSIBLE, IRREVERSIBLE)
    )


def _evidence(tally: Optional[dict], window_end: str) -> dict[str, Any]:
    tally = tally or {}
    return {
        "agreed": tally.get("agreed", 0),
        "disagreed": tally.get("disagreed", 0),
        "agreement_rate": tally.get("agreement_rate"),
        "window_end": window_end,
    }


def agreement_by_workflow(session) -> tuple[dict[str, dict], str]:
    """Each workflow's 30-day agreement tally, and the window's end."""
    from core.reporting.analytics_service import agent_agreement

    end = utcnow()
    scored = agent_agreement(session, end - AGREEMENT_WINDOW, end)
    tallies = {t["workflow_id"]: t for t in scored["by_workflow"]}
    return tallies, end.isoformat()


def workflow_evidence(workflow_id: str) -> dict[str, Any]:
    """The workflow's 30-day agreement, computed here and never taken from a client."""
    with unit_of_work() as session:
        tallies, window_end = agreement_by_workflow(session)
    return _evidence(tallies.get(workflow_id), window_end)


def workflow_entry(
    tier: str, floor: Optional[int], evidence: Optional[dict], by: str
) -> dict[str, Any]:
    return {
        "tier": tier,
        "floor": floor,
        "evidence": evidence,
        "by": by,
        "at": utcnow().isoformat(),
    }


def _locked_update(
    change: Callable[[Ceiling], tuple[Ceiling, str]], actor: str
) -> Ceiling:
    """Read, change and write the row in one transaction with it locked.

    ``change`` sees the row as it is now, so no other write is lost, and the
    write leaves the same ``config.change`` entry as ``set_system_config``.
    """
    try:
        return _write_locked(change, actor)
    except CeilingRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CeilingWriteError("autonomy.ceiling was not stored") from exc


def _write_locked(change: Callable[[Ceiling], tuple[Ceiling, str]], actor: str):
    with unit_of_work() as session:
        row = session.execute(
            select(SystemConfig)
            .where(SystemConfig.key == CEILING_KEY)
            .with_for_update()
        ).scalar_one_or_none()
        current = _parse(row.value) if row is not None else Ceiling()
        updated, reason = change(current)
        if updated == current:
            return current
        old = row.value if row is not None else None
        value = _dump(updated)
        if row is None:
            session.add(
                SystemConfig(
                    key=CEILING_KEY,
                    value=value,
                    description="Autonomy ceiling",
                    config_type="autonomy",
                    updated_by=actor,
                )
            )
        else:
            row.value = value
            row.updated_by = actor
        config_store.ConfigService(user_id=actor)._create_audit_log(
            session,
            config_type="autonomy",
            config_key=CEILING_KEY,
            action="create" if old is None else "update",
            old_value=old,
            new_value=value,
            change_reason=reason,
        )
        return updated


def set_workflow_tier(
    workflow_id: str, entry: Optional[dict], user_id: str, *, raises: bool
) -> Ceiling:
    """Store one workflow's entry; None returns it to the fleet flags.

    A raise is refused if a stop landed since the caller looked.
    """

    def change(current: Ceiling) -> tuple[Ceiling, str]:
        if raises and current.stopped:
            raise CeilingRefusal(409, "Stopped: resume first")
        workflows = dict(current.workflows)
        if entry is None:
            workflows.pop(workflow_id, None)
        else:
            workflows[workflow_id] = entry
        updated = replace(current, workflows=MappingProxyType(workflows))
        return updated, f"Autonomy of workflow {workflow_id} set"

    return _locked_update(change, user_id)


def drop_below_floor() -> None:
    """Every workflow on Act whose agreement fell below its floor goes to Assist.

    Written as the system, under the row lock, so a concurrent edit is not
    lost. Never raises.
    """
    try:
        with unit_of_work() as session:
            tallies, window_end = agreement_by_workflow(session)
        dropped: list[str] = []

        def change(current: Ceiling) -> tuple[Ceiling, str]:
            workflows = dict(current.workflows)
            for wid, entry in current.workflows.items():
                floor = entry.get("floor")
                if entry.get("tier") != ACT or not isinstance(floor, int):
                    continue
                evidence = _evidence(tallies.get(wid), window_end)
                rate = evidence["agreement_rate"]
                if rate is not None and rate < floor / 100:
                    workflows[wid] = workflow_entry(ASSIST, floor, evidence, _SYSTEM)
                    dropped.append(wid)
            reason = "Agreement fell below the floor: " + ", ".join(sorted(dropped))
            return replace(current, workflows=MappingProxyType(workflows)), reason

        _locked_update(change, _SYSTEM)
        if dropped:
            logger.warning("workflows dropped to Assist: %s", ", ".join(dropped))
    except Exception:  # noqa: BLE001 - a close never fails on this
        logger.exception("agreement floors not checked after a close")


_DROP_PENDING = "autonomy.drop_pending"


def _after_commit(session) -> None:
    # A SAVEPOINT release fires this too; only the outermost commit counts.
    if session.in_nested_transaction():
        return
    if session.info.pop(_DROP_PENDING, False):
        drop_below_floor()


def _after_rollback(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_DROP_PENDING, None)


def drop_after_commit(session) -> None:
    """Check the floors once ``session`` commits; a rolled-back close drops nothing."""
    session.info[_DROP_PENDING] = True
    if not event.contains(session, "after_commit", _after_commit):
        event.listen(session, "after_commit", _after_commit)
        event.listen(session, "after_rollback", _after_rollback)


# Effect classes, loosest first (#3304). A name with no row is unclassified.
READ = "read"
READ_UNVERIFIED = "read_unverified"
REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"
EFFECT_CLASSES = (READ, READ_UNVERIFIED, REVERSIBLE, IRREVERSIBLE)
# What chat may reach, and what a direct-action call runs without an approval for.
_READS = frozenset({READ, READ_UNVERIFIED})
APPROVAL_TOOL = "create_approval_action"
TOOL_CLASSIFIED = "autonomy.tool_classified"
_SYSTEM = "system"

_own: Optional[frozenset[str]] = None


def own_tool_names() -> frozenset[str]:
    """Names Vigil answers itself: its built-ins and its own MCP server's.

    Neither is an outside tool, so neither has a class. The set is fixed at
    import, so it is remembered once both halves are read.
    """
    global _own
    if _own is not None:
        return _own
    from core.llm.tool_schemas import ALL_TOOLS

    names = {t["name"] for t in ALL_TOOLS if t.get("name")}
    try:
        from core.integrations.mcp import in_process

        names |= {t["name"] for t in in_process.list_tools()}
    except Exception:  # noqa: BLE001 - not remembered; the next call retries
        logger.exception("Vigil's own MCP tool names were not read")
        return frozenset(names)
    _own = frozenset(names)
    return _own


def stored_classes(session, names: Iterable[str]) -> dict[str, str]:
    """The rows for ``names``. A name with no row is absent."""
    rows = session.execute(
        select(ToolEffectClass.tool_name, ToolEffectClass.effect_class).where(
            ToolEffectClass.tool_name.in_(list(names))
        )
    )
    return {name: effect_class for name, effect_class in rows}


def _read_classes(names: list[str]) -> dict[str, str]:
    with unit_of_work() as session:
        return stored_classes(session, names)


def tool_classes(names: Iterable[str]) -> dict[str, Optional[str]]:
    """Each outside name's class, or None when it is unclassified. One query.

    Names Vigil answers itself are left out. A store that cannot be read
    leaves every name unclassified, which asks first.
    """
    own = own_tool_names()
    outside = sorted({n for n in names if n and n not in own})
    if not outside:
        return {}
    try:
        stored = _read_classes(outside)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "tool classes not read; %d treated as unclassified: %s", len(outside), exc
        )
        stored = {}
    return {name: stored.get(name) for name in outside}


def is_read(effect_class: Optional[str]) -> bool:
    return effect_class in _READS


def gated_tools(names: Iterable[str]) -> frozenset[str]:
    """Outside tools that wait for an approval: reversible, irreversible, unclassified."""
    return frozenset(
        name for name, effect in tool_classes(names).items() if not is_read(effect)
    )


def asks_first(names: Iterable[str]) -> frozenset[str]:
    """The approval tool, and every gated outside tool, among ``names``."""
    names = list(names)
    return gated_tools(names) | ({APPROVAL_TOOL} & set(names))


STOPPED = "Stopped"


# Own tools that change what Vigil detects: held to the ceiling as reversible
# (#3949). Fixed here, not a stored class an operator could loosen to read.
def _own_reversible(tool: str, args: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Why the call is refused unless the run may act, or None for an ordinary write."""
    held = "and the autonomy ceiling does not let a run no person started make it"
    if tool == "detection_rule_exception":
        return f"{tool} changes what a rule detects, {held}"
    exceptions = (args or {}).get("exceptions")
    if tool == "save_detection_rule" and isinstance(exceptions, Mapping):
        if any(exceptions.values()):
            return (
                f"{tool} with exceptions changes what a rule detects, {held}; "
                "save it without exceptions"
            )
    return None


def unattended_refusal(
    tool: str,
    *,
    writes: bool,
    run_id: Optional[str] = None,
    args: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Why a run no person started may not make this call, or None (#3306).

    Reads run, stopped or not. ``writes`` says whether one of Vigil's own tools
    changes a record; those run unless stopped, except a rule exception, which
    needs the effective act a reversible call does (#3949). An outside call runs
    only under the effective act its class needs, for the run's own workflow
    (#3624). A ceiling that cannot be read grants nothing.
    """
    classes = tool_classes([tool])
    own = tool not in classes
    if (own and not writes) or (not own and is_read(classes[tool])):
        return None
    held = try_read_stored_ceiling()
    if held is None:
        return "the autonomy ceiling could not be read"
    stored, _found = held
    if stored.stopped:
        return STOPPED
    if own:
        refusal = _own_reversible(tool, args)
        if refusal is None or effective_act(stored, REVERSIBLE, workflow_of(run_id)):
            return None
        return refusal
    effect = classes[tool]
    if effect in (REVERSIBLE, IRREVERSIBLE) and effective_act(
        stored, effect, workflow_of(run_id)
    ):
        return None
    return (
        f"{tool} is {effect or 'unclassified'} and the autonomy ceiling "
        "does not let a run no person started take it"
    )


def loosens(before: Optional[str], after: str) -> bool:
    """Classifying an unclassified name counts as loosening."""
    return before is None or EFFECT_CLASSES.index(after) < EFFECT_CLASSES.index(before)


def store_tool_class(
    session, name: str, before: Optional[str], after: str, actor: str
) -> None:
    """Write one class and its trail entry in the caller's transaction."""
    from core.auth import trail_events

    row = session.get(ToolEffectClass, name)
    if row is None:
        session.add(
            ToolEffectClass(tool_name=name, effect_class=after, classified_by=actor)
        )
    else:
        row.effect_class = after
        row.classified_by = actor
        row.classified_at = utcnow()
    session.flush()
    trail_events.record(
        session,
        TOOL_CLASSIFIED,
        actor=actor,
        tool=name,
        class_before=before or "unclassified",
        class_after=after,
    )


def tighten_from_hints(server: str, tools: Iterable[dict]) -> bool:
    """``destructiveHint: true`` raises an existing row to irreversible.

    It never inserts a row and never lowers one; ``readOnlyHint`` is not read.
    Best effort: False when the store could not be written, so the caller can
    try again; a server's connect does not fail on it.
    """
    hinted = sorted(
        f"{server}_{tool['name']}"
        for tool in tools
        if tool.get("name")
        and isinstance(tool.get("annotations"), dict)
        and tool["annotations"].get("destructiveHint") is True
    )
    if not hinted:
        return True
    try:
        with unit_of_work() as session:
            rows = (
                session.execute(
                    select(ToolEffectClass)
                    .where(
                        ToolEffectClass.tool_name.in_(hinted),
                        ToolEffectClass.effect_class != IRREVERSIBLE,
                    )
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            for row in rows:
                store_tool_class(
                    session, row.tool_name, row.effect_class, IRREVERSIBLE, _SYSTEM
                )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("destructiveHint from %s not applied: %s", server, exc)
        return False
