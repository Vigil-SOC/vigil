"""One autonomy ceiling (#2918).

The stored row is ``autonomy.ceiling``. Read, write of Vigil's own records,
and propose are on unless ``stopped``. The act flags are off until a person
raises them. ``DAEMON_FORCE_APPROVAL`` and ``DAEMON_AUTO_RESPONSE`` can only
force the effective act flags off. Four-eyes is stored and not enforced.
An unbound caller is not denied here; the HTTP gates are the permission
checks on the routes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from core.config import Settings, get_settings
from core.storage import config_service as config_store

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
    off while ``stopped`` or ``environment_locked``. Read, write, and propose
    follow ``stopped`` only.
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
        read=not stored.stopped,
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
    service_account: bool,
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
    if service_account:
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


def resume_ceiling(*, user_id: str, service_account: bool) -> Ceiling:
    """Clear ``stopped``. Act flags already lowered during the stop stay off.

    Executes nothing. A service account cannot resume.
    """
    if service_account:
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
