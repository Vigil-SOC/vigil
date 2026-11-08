"""The rule-rollout limits an operator may change (#4058).

Three numbers decide how fast Vigil moves a rule copy from shadow to live and
when it pulls one back: the promotion cap per day, the days a copy stays in
shadow, and the most hits a day may carry. They live in one stored row,
``rollout.limits``, beside the autonomy ceiling, and every reader takes them
from here on each use, never at import. A missing row is today's defaults. A
row that cannot be read, or a field in it that is not a whole number inside its
bounds, falls back to the default for that field and logs; the sweep never
fails on it.

Quiet, Noisy and Silent thresholds are not stored here. They stay on each SIEM
integration (``siem_thresholds`` only reads them), because they are per-
integration search settings that the integration's own form validates.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Mapping, Optional

from sqlalchemy import select

from core.storage import config_service as config_store
from core.storage.models import SystemConfig
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

LIMITS_KEY = "rollout.limits"


@dataclass(frozen=True)
class RolloutLimits:
    live_per_day: int = 5  # rules promoted to live a day, across all rules
    shadow_days: int = 7  # days a copy stays in shadow before it may go live
    max_hits_per_day: int = 20  # no shadow or live day may carry more hits


DEFAULTS = RolloutLimits()
# name -> (lowest, highest) allowed value.
BOUNDS: Dict[str, tuple[int, int]] = {
    "live_per_day": (0, 1000),
    "shadow_days": (1, 90),
    "max_hits_per_day": (1, 100000),
}


class LimitsRefusal(Exception):
    """A limits write the caller is not allowed to store."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class LimitsWriteError(Exception):
    """The limits row was not stored."""


def valid(name: str, value: Any) -> bool:
    low, high = BOUNDS[name]
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def _parse(value: Any) -> RolloutLimits:
    """The stored value; each field that is not valid reads as its default."""
    if value is None:
        return DEFAULTS
    if not isinstance(value, dict):
        logger.error("%s is not an object; defaults apply", LIMITS_KEY)
        return DEFAULTS
    fields: Dict[str, int] = {}
    for name in BOUNDS:
        if name not in value:
            continue
        if valid(name, value[name]):
            fields[name] = value[name]
        else:
            logger.error(
                "%s.%s=%r is not a whole number from %d to %d; the default %d applies",
                LIMITS_KEY,
                name,
                value[name],
                *BOUNDS[name],
                getattr(DEFAULTS, name),
            )
    return replace(DEFAULTS, **fields)


def read_stored_limits() -> RolloutLimits:
    """The stored limits. Raises when the read fails; the API says 503."""
    return _parse(config_store.get_config_service().read_system_config(LIMITS_KEY))


def read_limits() -> RolloutLimits:
    """The limits now in force. Never raises: a failed read is the defaults."""
    try:
        return read_stored_limits()
    except Exception as exc:  # noqa: BLE001
        logger.error("%s not read; defaults apply: %s", LIMITS_KEY, exc)
        return DEFAULTS


def dump(limits: RolloutLimits) -> Dict[str, int]:
    return asdict(limits)


def tightens(current: RolloutLimits, after: RolloutLimits) -> bool:
    """True when ``after`` moves no copy to live that ``current`` would hold."""
    return (
        after.live_per_day <= current.live_per_day
        and after.shadow_days >= current.shadow_days
        and after.max_hits_per_day <= current.max_hits_per_day
    )


def update_limits(
    *,
    user_id: str,
    caller_is_service: bool,
    changes: Mapping[str, int],
    reason: Optional[str] = None,
) -> RolloutLimits:
    """Store a person's edit of the limits and its audit entry in one transaction.

    ``changes`` holds only the fields to change, already validated by the
    route; they are checked again here. A service account may only tighten:
    a lower cap, more days in shadow or a lower hit ceiling. The row is locked
    while it is read and written, so no other edit is lost.
    """
    bad = [n for n, v in changes.items() if n not in BOUNDS or not valid(n, v)]
    if bad or not changes:
        raise LimitsRefusal(422, "Not a valid rollout limit: " + ", ".join(sorted(bad)))
    try:
        return _write_locked(user_id, caller_is_service, dict(changes), reason)
    except LimitsRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LimitsWriteError("rollout.limits was not stored") from exc


def _write_locked(
    actor: str, caller_is_service: bool, changes: Dict[str, int], reason: Optional[str]
) -> RolloutLimits:
    with unit_of_work() as session:
        row = session.execute(
            select(SystemConfig).where(SystemConfig.key == LIMITS_KEY).with_for_update()
        ).scalar_one_or_none()
        current = _parse(row.value) if row is not None else DEFAULTS
        updated = replace(current, **changes)
        if caller_is_service and not tightens(current, updated):
            raise LimitsRefusal(403, "A service account cannot loosen rollout limits")
        if updated == current:
            return current
        old = row.value if row is not None else None
        value = dump(updated)
        if row is None:
            session.add(
                SystemConfig(
                    key=LIMITS_KEY,
                    value=value,
                    description="Rule rollout limits",
                    config_type="rollout",
                    updated_by=actor,
                )
            )
        else:
            row.value = value
            row.updated_by = actor
        what = ", ".join(
            f"{n} {getattr(current, n)} -> {getattr(updated, n)}"
            for n in changes
            if getattr(current, n) != getattr(updated, n)
        )
        config_store.ConfigService(user_id=actor)._create_audit_log(
            session,
            config_type="rollout",
            config_key=LIMITS_KEY,
            action="create" if old is None else "update",
            old_value=old if old is not None else dump(current),
            new_value=value,
            change_reason=f"Rollout limits changed: {what}"
            + (f". {reason}" if reason else ""),
        )
        return updated


def siem_thresholds() -> Dict[str, Optional[Dict[str, int]]]:
    """Each SIEM integration's Quiet, Noisy and Silent thresholds, as its own
    stored config sets them (the defaults where unset). Read only: they are
    edited on the integration. None when that integration's config can't be read.
    """
    from core.detections.inventory import quiet
    from core.integrations._base.config import resolve_fields
    from core.integrations.elastic.descriptor import ELASTIC
    from core.integrations.splunk.descriptor import SPLUNK

    names = {"quiet_max_alerts_per_day": quiet.DEFAULT_MAX_PER_DAY, **quiet.DEFAULTS}
    out: Dict[str, Optional[Dict[str, int]]] = {}
    for descriptor in (ELASTIC, SPLUNK):
        try:
            config = resolve_fields(descriptor.id, names)
            out[descriptor.id] = {
                k: quiet._int(config.get(k), d) for k, d in names.items()
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("%s thresholds not read: %s", descriptor.id, exc)
            out[descriptor.id] = None
    return out
