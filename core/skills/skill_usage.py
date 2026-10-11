"""Recording and reading skill usage (#1560).

One row in ``skill_reads`` per successful read of a skill body, written from
``/internal/tools/invoke`` — the one place a read is known to have
succeeded — and read back by ``GET /api/skills`` as a 7-day window. Plain
functions, not a service: the writer and the reader are both one query.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, Optional, Tuple

from sqlalchemy import distinct, func

from core.storage.connection import get_db_manager
from core.storage.models import SkillRead
from core.time import utcnow

logger = logging.getLogger(__name__)

# The console's window. The recorder (not a scheduler) keeps a day more than
# this so a read cannot expire mid-window; see 40_skill_reads.sql.
WINDOW = timedelta(days=7)
RETENTION = timedelta(days=8)


def record_skill_read(skill_name: str, agent_id: Optional[str]) -> None:
    """Insert one read, and prune rows past retention in the same write.

    Pruning on write keeps the table at 8 days with no new scheduler. The
    caller (the tool router) treats any failure here as non-fatal: a usage
    log must never fail the tool call it is counting.
    """
    now = utcnow()
    with get_db_manager().session_scope() as session:
        session.add(SkillRead(skill_name=skill_name, agent_id=agent_id, read_at=now))
        session.query(SkillRead).filter(SkillRead.read_at < now - RETENTION).delete(
            synchronize_session=False
        )


def skill_usage() -> Dict[str, Tuple[int, int]]:
    """``{skill_name: (reads_7d, agents_7d)}`` from one grouped query.

    ``agents_7d`` counts distinct non-null agent ids: unattributed reads
    (hunt, plain chat, …) raise the read count without inventing an agent.
    """
    since = utcnow() - WINDOW
    with get_db_manager().session_scope() as session:
        rows = (
            session.query(
                SkillRead.skill_name,
                func.count(),
                func.count(distinct(SkillRead.agent_id)),
            )
            .filter(SkillRead.read_at >= since)
            .group_by(SkillRead.skill_name)
            .all()
        )
    return {name: (reads, agents) for name, reads, agents in rows}
