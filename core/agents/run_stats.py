"""Seven-day runs and success per agent, for the Agents list."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Dict

from sqlalchemy import func

from core.findings.overview import completion_level
from core.storage.connection import get_db_manager
from core.storage.models import ChatMessage, Conversation, WorkflowRunPhase
from core.time import utcnow

WINDOW = timedelta(days=7)


def agent_run_stats() -> Dict[str, dict]:
    """``{agent_id: {runs_7d, success_rate, success_level}}`` for agents with runs.

    A run is a finished (completed or failed) workflow phase started in the
    window, or an assistant chat turn created in it. ``success_rate`` is a
    fraction 0..1, None when there are no runs. One grouped query per source.
    """
    since = utcnow() - WINDOW
    runs: Counter = Counter()
    done: Counter = Counter()
    with get_db_manager().session_scope() as session:
        phases = (
            session.query(
                WorkflowRunPhase.agent_id,
                WorkflowRunPhase.status,
                func.count(),
            )
            .filter(
                WorkflowRunPhase.status.in_(("completed", "failed")),
                WorkflowRunPhase.started_at >= since,
            )
            .group_by(WorkflowRunPhase.agent_id, WorkflowRunPhase.status)
            .all()
        )
        chats = (
            session.query(Conversation.agent_id, ChatMessage.complete, func.count())
            .join(Conversation, Conversation.id == ChatMessage.conversation_id)
            .filter(
                ChatMessage.role == "assistant",
                ChatMessage.created_at >= since,
                Conversation.agent_id.isnot(None),
            )
            .group_by(Conversation.agent_id, ChatMessage.complete)
            .all()
        )
    for agent_id, status, n in phases:
        runs[agent_id] += n
        done[agent_id] += n if status == "completed" else 0
    for agent_id, complete, n in chats:
        runs[agent_id] += n
        done[agent_id] += n if complete else 0
    return {
        agent_id: {
            "runs_7d": n,
            "success_rate": done[agent_id] / n,
            "success_level": completion_level(n, done[agent_id]),
        }
        for agent_id, n in runs.items()
    }
