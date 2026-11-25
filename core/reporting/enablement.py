"""Which report modules are turned off, and how often the report runs again.

Every module in ``MODULES`` runs unless its id is listed. No row, or a read
that failed, means every module runs. Unknown ids in the list are harmless.
The recurring interval is a second row in whole days: no row is 7, 0 is off.
"""

from typing import Set

from core.reporting.checks import MODULES
from core.storage.config_service import get_config_service

DISABLED_MODULES_KEY = "day1_report.modules.disabled"
RECURRING_KEY = "day1_report.recurring"
RECURRING_DEFAULT_DAYS = 7
RECURRING_MAX_DAYS = 365


def disabled_module_ids() -> Set[str]:
    """The ids turned off. No row, or a read that failed, is every module on."""
    value = get_config_service().get_system_config(DISABLED_MODULES_KEY)
    if value is None:
        return set()
    ids = value.get("ids") if isinstance(value, dict) else value
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def set_module_enabled(module_id: str, enabled: bool, user_id: str) -> bool:
    """Turn a report module on or off, audited as ``user_id``. False if the write failed.

    Raises ``ValueError`` for an id that is not a built module.
    """
    if module_id not in MODULES:
        raise ValueError(f"Unknown report module: {module_id}")
    disabled = disabled_module_ids()
    if (module_id not in disabled) == enabled:
        return True
    disabled = disabled - {module_id} if enabled else disabled | {module_id}
    return get_config_service(user_id=user_id).set_system_config(
        key=DISABLED_MODULES_KEY,
        value={"ids": sorted(disabled)},
        description="Report module ids turned off",
        config_type="report",
        change_reason=f"Report module {module_id} turned {'on' if enabled else 'off'}",
    )


def recurring_days() -> int:
    """Days between recurring runs. No row, or one that does not parse, is the default."""
    return parse_recurring(get_config_service().get_system_config(RECURRING_KEY))


def parse_recurring(value) -> int:
    """The stored row's days. Missing or out of range is the default."""
    days = value.get("days") if isinstance(value, dict) else None
    if isinstance(days, int) and not isinstance(days, bool):
        if 0 <= days <= RECURRING_MAX_DAYS:
            return days
    return RECURRING_DEFAULT_DAYS


def set_recurring_days(days: int, user_id: str) -> bool:
    """Set the interval, audited as ``user_id``. False if the write failed.

    Raises ``ValueError`` outside 0..RECURRING_MAX_DAYS.
    """
    if not 0 <= days <= RECURRING_MAX_DAYS:
        raise ValueError(f"Interval must be 0 to {RECURRING_MAX_DAYS} days")
    return get_config_service(user_id=user_id).set_system_config(
        key=RECURRING_KEY,
        value={"days": days},
        description="Days between recurring Day 1 Report runs; 0 is off",
        config_type="report",
        change_reason=(
            "Recurring Day 1 Report turned off"
            if days == 0
            else f"Recurring Day 1 Report every {days} days"
        ),
    )
