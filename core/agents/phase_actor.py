"""The agent a run's playbook names for its running phase.

Kept apart from ``core.agents.attribution`` so who-did-it lookups
(``core.cases.agent_closure``) do not pull in the autonomy ceiling.
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text

from core.storage.connection import get_db_session


def phase_actor(run_id: str) -> Optional[str]:
    """The agent the playbook stored at start names for the running phase.

    The phase row's own agent text is not read: the body that reported the
    phase is not the source of the actor.
    """
    if not run_id:
        return None
    try:
        session = get_db_session()
    except Exception:  # noqa: BLE001 - no database is today's path
        return None
    try:
        context = session.execute(
            text("SELECT trigger_context FROM workflow_runs WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar()
        if isinstance(context, str):
            context = json.loads(context)
        agents = context.get("phase_agents") if isinstance(context, dict) else None
        if not isinstance(agents, dict) or not agents:
            return None
        phase = session.execute(
            text(
                "SELECT phase_id FROM workflow_run_phases "
                "WHERE run_id = :run_id AND status = 'running' "
                "ORDER BY phase_order DESC LIMIT 1"
            ),
            {"run_id": run_id},
        ).scalar()
        if isinstance(phase, str) and isinstance(agents.get(phase), str):
            return agents[phase]
        if phase is None and len(agents) == 1:
            only = next(iter(agents.values()))
            return only if isinstance(only, str) else None
        return None
    except Exception:  # noqa: BLE001 - a missing row is not a phase agent
        return None
    finally:
        session.close()
