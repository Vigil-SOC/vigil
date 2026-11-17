"""The agent layer's own login, vigil_agent, granted only the agent tables."""

from core.upgrade.revision import run_sql

revision = "0081_agent_role"
down_revision = "0080_merge_0079_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
