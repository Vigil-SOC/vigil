"""A run's model, read the same way by the run judge and by agent agreement (#5222).

A run's model is the one its spend events on the agent ledger record. For
investigate and hunt, only the run's own agent counts (role ``agent``; the
reviewer's and critic's calls are not the run's answer). Other kinds spend under
the name of each move or phase, so every role counts for them except ``reader``,
the guard that reads tool output, and a spend with no role counts for none. A
run that used more than one model, or recorded none, is under no model.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Integer, String, and_, case, cast, column, func, select, table
from sqlalchemy.dialects.postgresql import JSONB, UUID

# Kinds whose answer is one agent's: other roles on their ledger are reviewers.
AGENT_ONLY = ("investigate", "hunt")
# The guard that reads tool output for the model; its spend is not the answer.
SIDE_ROLES = ("reader",)
UUID_PATTERN = "^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$"

events = table(
    "agent_events",
    column("run_id", UUID(as_uuid=False)),
    column("seq", Integer),
    column("kind", String),
    column("payload", JSONB),
)


def ledger_of(run_id):
    """The run's ledger id; a run id that is not a uuid has none."""
    return case(
        (run_id.op("~*")(UUID_PATTERN), cast(run_id, UUID(as_uuid=False))),
        else_=None,
    )


def run_model(kind, ledger):
    """``(model, models)`` scalar subqueries on the outer run: the lowest model id
    its own spend records, and how many distinct ones. Read through :func:`attributed`.
    """
    spend = events.alias("run_spend")
    role = spend.c.payload["role"].astext
    its_spend = and_(
        spend.c.run_id == ledger,
        spend.c.kind == "spend",
        case(
            (kind.in_(AGENT_ONLY), role == "agent"),
            else_=role.is_distinct_from(None) & role.notin_(SIDE_ROLES),
        ),
    )
    model_of = spend.c.payload["model_id"].astext
    models = (
        select(func.count(func.distinct(model_of))).where(its_spend).scalar_subquery()
    )
    model = select(func.min(model_of)).where(its_spend).scalar_subquery()
    return model, models


def attributed(model: Optional[str], models: Optional[int]) -> Optional[str]:
    """The run's model, or None when it used more than one or recorded none."""
    return model if models == 1 and model is not None else None
