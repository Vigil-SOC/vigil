"""One coverage entry per key per check (#5293).

Chains after ``0132_workflow_run_step_score``.
"""

from core.upgrade.revision import run_sql

revision = "0133_episodic_coverage_entries"
down_revision = "0132_workflow_run_step_score"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
