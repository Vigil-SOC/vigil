"""What a run's report stands on, for the case's choice of run (#4962).

Adds ``workflow_runs.capped``, ``citation_count`` and ``grounded``, written from the
agent's terminal. Null is unsaid: every run before this step reads as unread.
"""

from core.upgrade.revision import run_sql

revision = "0110_workflow_run_standing"
down_revision = "0109_source_health_window_counts"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
