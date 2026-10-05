"""Which agents are turned off: one SystemConfig row, ``agents.disabled``.

Every agent is on unless its id is listed. Unknown or deleted ids in the list
are harmless: nothing looks them up.
"""

import logging
from typing import Set

from core.storage.config_service import get_config_service

logger = logging.getLogger(__name__)

DISABLED_AGENTS_KEY = "agents.disabled"


def disabled_agent_ids() -> Set[str]:
    """The ids turned off. Reads fail open: a DB error means everything is on."""
    value = get_config_service().get_system_config(DISABLED_AGENTS_KEY)
    ids = value.get("ids") if isinstance(value, dict) else value
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def set_agent_enabled(agent_id: str, enabled: bool, user_id: str) -> bool:
    """Turn an agent on or off, audited as ``user_id``. False if the write failed."""
    disabled = disabled_agent_ids()
    if (agent_id not in disabled) == enabled:
        return True  # already in that state
    disabled = disabled - {agent_id} if enabled else disabled | {agent_id}
    return get_config_service(user_id=user_id).set_system_config(
        key=DISABLED_AGENTS_KEY,
        value={"ids": sorted(disabled)},
        description="Agent ids turned off",
        config_type="agents",
        change_reason=f"Agent {agent_id} turned {'on' if enabled else 'off'}",
    )


def disabled_message(agent_id: str) -> str:
    return f"agent {agent_id} is turned off"
