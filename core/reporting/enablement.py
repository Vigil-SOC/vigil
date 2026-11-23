"""Which report modules are turned off: one SystemConfig row.

Every module in ``MODULES`` runs unless its id is listed. No row, or a read
that failed, means every module runs. Unknown ids in the list are harmless.
"""

from typing import Set

from core.reporting.checks import MODULES
from core.storage.config_service import get_config_service

DISABLED_MODULES_KEY = "day1_report.modules.disabled"


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
