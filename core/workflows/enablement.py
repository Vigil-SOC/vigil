"""Which workflows are turned off: one SystemConfig row, ``workflows.disabled``.

Every workflow is on unless its id is listed, except those in ``DEFAULT_DISABLED``
while no row exists. Once the row exists, even as an empty list, it replaces the
default. Unknown ids in the list are harmless: nothing looks them up.
"""

import logging
from typing import Set

from core.storage.config_service import get_config_service
from core.workflows.routing import SHADOW_WORKFLOW_ID, can_disable

logger = logging.getLogger(__name__)

DISABLED_WORKFLOWS_KEY = "workflows.disabled"
# Off until someone turns it on: shadow runs spend money beside every finding.
DEFAULT_DISABLED = frozenset({SHADOW_WORKFLOW_ID})


def disabled_workflow_ids() -> Set[str]:
    """The ids turned off. No row, or a read that failed, is ``DEFAULT_DISABLED``."""
    value = get_config_service().get_system_config(DISABLED_WORKFLOWS_KEY)
    if value is None:
        return set(DEFAULT_DISABLED)
    ids = value.get("ids") if isinstance(value, dict) else value
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def is_enabled(workflow_id: str) -> bool:
    return workflow_id not in disabled_workflow_ids()


def set_workflow_enabled(workflow_id: str, enabled: bool, user_id: str) -> bool:
    """Turn a workflow on or off, audited as ``user_id``. False if the write failed.

    Raises ``ValueError`` with the reason for a workflow that cannot be turned off.
    """
    if not enabled and not can_disable(workflow_id):
        raise ValueError(
            f"{workflow_id} is where alerts land when nothing else fits, "
            "so it cannot be turned off"
        )
    disabled = disabled_workflow_ids()
    if (workflow_id not in disabled) == enabled:
        return True  # already in that state
    disabled = disabled - {workflow_id} if enabled else disabled | {workflow_id}
    return get_config_service(user_id=user_id).set_system_config(
        key=DISABLED_WORKFLOWS_KEY,
        value={"ids": sorted(disabled)},
        description="Workflow ids turned off",
        config_type="workflows",
        change_reason=f"Workflow {workflow_id} turned {'on' if enabled else 'off'}",
    )


def disabled_message(workflow_id: str) -> str:
    return f"workflow {workflow_id} is turned off"
