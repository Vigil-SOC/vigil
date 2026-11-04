"""A run's status as rates and the console read it."""

from __future__ import annotations

from sqlalchemy import and_, case, exists

from core.storage.models import WorkflowRun, WorkflowRunPhase

# A run a person stopped by rejecting a step is stored failed, but that is the review
# working, not the workflow failing. Rates read it as "rejected", so it is a run but
# not in the rate, as a rejected step is for its agent (core/agents/run_stats.py).
RATED_STATUS = case(
    (
        and_(
            WorkflowRun.status == "failed",
            exists().where(
                WorkflowRunPhase.run_id == WorkflowRun.run_id,
                WorkflowRunPhase.status == "failed",
                WorkflowRunPhase.approval_state == "rejected",
            ),
        ),
        "rejected",
    ),
    else_=WorkflowRun.status,
)
