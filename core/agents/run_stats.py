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
    window, or one that never started and finished in it (a step a person
    rejected), or an assistant chat turn created in it. ``success_rate`` is a
    fraction 0..1, None when there are no runs. One grouped query per source.
    """
    since = utcnow() - WINDOW
    runs: Counter = Counter()
    done: Counter = Counter()
    # a step a person rejected is a decision, not a failure: it counts as a run but
    # stays out of the success rate
    rejected: Counter = Counter()
    with get_db_manager().session_scope() as session:
        phases = (
            session.query(
                WorkflowRunPhase.agent_id,
                WorkflowRunPhase.status,
                WorkflowRunPhase.approval_state,
                func.count(),
            )
            .filter(
                WorkflowRunPhase.status.in_(("completed", "failed")),
                # a rejected step never ran, so it has no start, only its finish
                func.coalesce(WorkflowRunPhase.started_at, WorkflowRunPhase.finished_at)
                >= since,
            )
            .group_by(
                WorkflowRunPhase.agent_id,
                WorkflowRunPhase.status,
                WorkflowRunPhase.approval_state,
            )
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
    for agent_id, status, approval, n in phases:
        if status == "failed" and approval == "rejected":
            rejected[agent_id] += n
            continue
        runs[agent_id] += n
        done[agent_id] += n if status == "completed" else 0
    for agent_id, complete, n in chats:
        runs[agent_id] += n
        done[agent_id] += n if complete else 0
    return {
        agent_id: {
            "runs_7d": runs[agent_id] + rejected[agent_id],
            "success_rate": done[agent_id] / runs[agent_id] if runs[agent_id] else None,
            "success_level": (
                completion_level(runs[agent_id], done[agent_id])
                if runs[agent_id]
                else None
            ),
        }
        for agent_id in runs.keys() | rejected.keys()
    }
