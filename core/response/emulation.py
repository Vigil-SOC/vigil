"""The one host list emulators may run on (#5061).

One stored row, ``emulation``, beside ``autonomy.ceiling`` and
``rollout.limits``: ``hosts`` holds the hostnames every emulator may act on.
Which tools are emulators is their binding (``core/integrations/bindings.py``,
#5315): a person binds a tool as an emulator on its classify row, and Atomic
Red Team's shipped binding is one. Read on each use, never at import. A missing
row, or one that cannot be read, lists no host, so every emulator call is refused.
A stored ``tools`` field from before #5315 is ignored.

``refusal`` is the check ``core.agents.tools_router`` makes before a call to an
emulator leaves; ``unlisted_hosts`` reads an answer back for steps on hosts
outside the list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from core.auth.principals import SYSTEM_PRINCIPAL_ID
from core.detections.gaps import TECHNIQUE_ID
from core.detections.reconstruction import answer_steps
from core.storage import config_service as config_store
from core.storage.models import SystemConfig
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

EMULATION_KEY = "emulation"
# The Atomic tool's {server}_{tool} spelling and its engine id: names a declared
# source may not take (core/sources.py). Its emulator binding is in bindings.yaml.
ATOMIC_TOOLS = (
    "atomic-red-team_atomic_red_team_execute",
    "atomic_red_team_execute",
)
CHANGED = "autonomy.emulation_changed"
UNLISTED = "emulation.unlisted_host"
LIMITS_POINTER = "Settings → Limits & autonomy → Emulators"
MAX_HOST = 253  # longest DNS name


@dataclass(frozen=True)
class Emulation:
    hosts: Tuple[str, ...] = ()

    def listed(self, hostname: str) -> Optional[str]:
        """The listed entry ``hostname`` names, ignoring case, or None."""
        want = hostname.lower()
        return next((h for h in self.hosts if h.lower() == want), None)


DEFAULTS = Emulation()


class EmulationRefusal(Exception):
    """An emulation write the caller is not allowed to store."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class EmulationWriteError(Exception):
    """The emulation row was not stored."""


def valid_host(entry: Any) -> bool:
    """One hostname: no whitespace, nothing a runner could read as a flag."""
    return (
        isinstance(entry, str)
        and 0 < len(entry) <= MAX_HOST
        and not entry.startswith("-")
        and not re.search(r"\s", entry)
    )


def _unique(items: Iterable[str], key: Callable[[str], str] = str) -> Tuple[str, ...]:
    seen: Dict[str, str] = {}
    for item in items:
        seen.setdefault(key(item), item)
    return tuple(seen.values())


def _parse(value: Any) -> Emulation:
    """The stored value; a field that is not a list reads as its default, and
    an entry that is not valid is dropped."""
    if value is None:
        return DEFAULTS
    if not isinstance(value, dict):
        logger.error("%s is not an object; defaults apply", EMULATION_KEY)
        return DEFAULTS
    hosts = value.get("hosts")
    if not isinstance(hosts, list):
        logger.error("%s.hosts is not a list; no host is listed", EMULATION_KEY)
        hosts = []
    bad = [h for h in hosts if not valid_host(h)]
    if bad:
        logger.error("%s.hosts ignores %r", EMULATION_KEY, bad)
    return Emulation(hosts=_unique((h for h in hosts if valid_host(h)), str.lower))


def read_stored() -> Emulation:
    """The stored record. Raises when the read fails; the API says 503."""
    return _parse(config_store.get_config_service().read_system_config(EMULATION_KEY))


def read_emulation() -> Emulation:
    """The record now in force. Never raises: a failed read is the defaults."""
    try:
        return read_stored()
    except Exception as exc:  # noqa: BLE001
        logger.error("%s not read; defaults apply: %s", EMULATION_KEY, exc)
        return DEFAULTS


def dump(emulation: Emulation) -> Dict[str, List[str]]:
    return {"hosts": list(emulation.hosts)}


@dataclass(frozen=True)
class Change:
    hosts_added: Tuple[str, ...]
    hosts_removed: Tuple[str, ...]

    @property
    def adds(self) -> bool:
        return bool(self.hosts_added)

    @property
    def removes(self) -> bool:
        return bool(self.hosts_removed)


def diff(current: Emulation, after: Emulation) -> Change:
    low = {h.lower() for h in current.hosts}
    low_after = {h.lower() for h in after.hosts}
    return Change(
        hosts_added=tuple(h for h in after.hosts if h.lower() not in low),
        hosts_removed=tuple(h for h in current.hosts if h.lower() not in low_after),
    )


def update_emulation(
    *,
    actor: str,
    hosts: Optional[List[str]],
    allow: Callable[[Change], None],
) -> Emulation:
    """Store the host list and one trail entry together.

    ``hosts`` replaces what is stored; None leaves it as it is. ``allow`` is
    called with what changes while the row is locked, and raises to refuse it:
    adding is a person's act, removing is not.
    """
    if hosts is not None:
        bad = [h for h in hosts if not valid_host(h)]
        if bad:
            raise EmulationRefusal(
                422,
                "A host is one hostname, with no whitespace and no leading '-': "
                + ", ".join(repr(h) for h in bad),
            )
    try:
        return _write_locked(actor, hosts, allow)
    except EmulationRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EmulationWriteError(f"{EMULATION_KEY} was not stored") from exc


def _write_locked(
    actor: str,
    hosts: Optional[List[str]],
    allow: Callable[[Change], None],
) -> Emulation:
    from core.auth import trail_events

    with unit_of_work() as session:
        row = session.execute(
            select(SystemConfig)
            .where(SystemConfig.key == EMULATION_KEY)
            .with_for_update()
        ).scalar_one_or_none()
        current = _parse(row.value) if row is not None else DEFAULTS
        updated = Emulation(
            hosts=current.hosts if hosts is None else _unique(hosts, str.lower)
        )
        change = diff(current, updated)
        if not (change.adds or change.removes):
            return current
        allow(change)
        value = dump(updated)
        if row is None:
            session.add(
                SystemConfig(
                    key=EMULATION_KEY,
                    value=value,
                    description="The hosts emulators may run on",
                    config_type="autonomy",
                    updated_by=actor,
                )
            )
        else:
            row.value = value
            row.updated_by = actor
        session.flush()
        trail_events.record(
            session,
            CHANGED,
            actor=actor,
            hosts_added=list(change.hosts_added) or None,
            hosts_removed=list(change.hosts_removed) or None,
        )
        return updated


def refusal(emulation: Emulation, tool: str, args: Dict[str, Any]) -> Optional[str]:
    """Why a call to ``tool``, an emulator, may not leave, or None. The caller
    asks only for a tool bound as an emulator."""
    if not emulation.hosts:
        return (
            f"{tool} is an emulator and no host is listed for emulation; "
            f"list the hosts it may run on under {LIMITS_POINTER}"
        )
    hostname = str(args.get("hostname") or "").strip()
    if not hostname and not str(args.get("handle") or "").strip():
        return f"{tool} is an emulator: a call names a hostname, or a handle to read"
    if hostname and emulation.listed(hostname) is None:
        return (
            f"hostname {hostname!r} is not listed for emulation; "
            f"emulators run only on: {', '.join(emulation.hosts)}"
        )
    technique = args.get("technique")
    if technique is not None and not TECHNIQUE_ID.match(str(technique).strip()):
        return (
            f"technique {technique!r} is not an ATT&CK technique id "
            "(e.g. T1003 or T1003.001)"
        )
    return None


def unlisted_hosts(emulation: Emulation, answer: Any) -> List[str]:
    """Hostnames the answer's steps name that are not listed, once each."""
    hosts = [str(s.get("hostname") or "").strip() for s in answer_steps(answer)]
    return list(
        _unique((h for h in hosts if h and emulation.listed(h) is None), str.lower)
    )


def note_unlisted(tool: str, run_id: Optional[str], hosts: List[str]) -> None:
    """One trail entry for an answer with steps on unlisted hosts. Never raises:
    the answer stands either way."""
    from core.auth import trail_events

    try:
        with unit_of_work() as session:
            trail_events.record(
                session,
                UNLISTED,
                actor=SYSTEM_PRINCIPAL_ID,
                tool=tool,
                run_id=run_id,
                hosts=hosts,
            )
    except Exception:  # noqa: BLE001
        logger.exception("%s steps on unlisted hosts %s were not recorded", tool, hosts)
