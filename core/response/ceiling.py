"""One autonomy ceiling (#2918).

The stored row is ``autonomy.ceiling``. Read is always on. Write of Vigil's
own records and propose are on unless ``stopped``. The act flags are off until a person
raises them. ``DAEMON_FORCE_APPROVAL`` and ``DAEMON_AUTO_RESPONSE`` can only
force the effective act flags off. Four-eyes is enforced where an approval
is recorded, in ``ApprovalService.approve_action`` (#2932).
An unbound caller is not denied here; the HTTP gates are the permission
checks on the routes.

Each outside tool also has one stored effect class (#3304), read here by every
caller that decides whether a tool call needs an approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import select

from core.config import Settings, get_settings
from core.storage import config_service as config_store
from core.storage.models import ToolEffectClass
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

CEILING_KEY = "autonomy.ceiling"


@dataclass(frozen=True)
class Ceiling:
    act_reversible: bool = False
    act_irreversible: bool = False
    stopped: bool = False
    four_eyes: bool = False


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


def read_stored_ceiling() -> tuple[Ceiling, bool]:
    """The stored row. ``(defaults, False)`` when the key is absent.

    Raises when the read fails. A missing row is the seeded off state, not Act.
    """
    value = config_store.get_config_service().read_system_config(CEILING_KEY)
    if value is None:
        return Ceiling(), False
    if not isinstance(value, dict):
        return Ceiling(), True
    return (
        Ceiling(
            act_reversible=_flag(value.get("act_reversible")),
            act_irreversible=_flag(value.get("act_irreversible")),
            stopped=_flag(value.get("stopped")),
            four_eyes=_flag(value.get("four_eyes")),
        ),
        True,
    )


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


def effective_act_reversible(
    stored: Ceiling, settings: Optional[Settings] = None
) -> bool:
    if stored.stopped or environment_locks_act(settings):
        return False
    return stored.act_reversible


def effective_act_irreversible(
    stored: Ceiling, settings: Optional[Settings] = None
) -> bool:
    if stored.stopped or environment_locks_act(settings):
        return False
    return stored.act_irreversible


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


def _dump(ceiling: Ceiling) -> dict[str, bool]:
    return {
        "act_reversible": ceiling.act_reversible,
        "act_irreversible": ceiling.act_irreversible,
        "stopped": ceiling.stopped,
        "four_eyes": ceiling.four_eyes,
    }


def _store(ceiling: Ceiling, user_id: str, reason: str) -> None:
    ok = config_store.get_config_service(user_id=user_id).set_system_config(
        key=CEILING_KEY,
        value=_dump(ceiling),
        description="Autonomy ceiling",
        config_type="autonomy",
        change_reason=reason,
    )
    if not ok:
        raise CeilingWriteError("autonomy.ceiling was not stored")


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
    updated = Ceiling(
        act_reversible=(
            current.act_reversible if act_reversible is None else act_reversible
        ),
        act_irreversible=(
            current.act_irreversible if act_irreversible is None else act_irreversible
        ),
        stopped=current.stopped,
        four_eyes=current.four_eyes if four_eyes is None else four_eyes,
    )
    if updated == current:
        return current
    _store(updated, user_id, "Autonomy ceiling updated")
    return updated


def stop_ceiling(user_id: str) -> Ceiling:
    """Set ``stopped``. Stored act flags stay, so a later resume can restore them.

    Effective act flags are off while stopped. This does not execute anything.
    """
    current = read_ceiling()
    if current.stopped:
        return current
    updated = Ceiling(
        act_reversible=current.act_reversible,
        act_irreversible=current.act_irreversible,
        stopped=True,
        four_eyes=current.four_eyes,
    )
    _store(updated, user_id, "Autonomy stopped")
    return updated


def resume_ceiling(*, user_id: str, caller_is_service: bool) -> Ceiling:
    """Clear ``stopped``. Act flags already lowered during the stop stay off.

    Executes nothing. A service account cannot resume.
    """
    if caller_is_service:
        raise CeilingRefusal(403, "A service account cannot resume")
    current = read_ceiling()
    if not current.stopped:
        return current
    updated = Ceiling(
        act_reversible=current.act_reversible,
        act_irreversible=current.act_irreversible,
        stopped=False,
        four_eyes=current.four_eyes,
    )
    _store(updated, user_id, "Autonomy resumed")
    return updated


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
