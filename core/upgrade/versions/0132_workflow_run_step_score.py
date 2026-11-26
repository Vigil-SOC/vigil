"""The step score on a run's row (#5275).

Chains after ``0131_site_context_principal``.
"""

from core.upgrade.revision import run_sql

revision = "0132_workflow_run_step_score"
down_revision = "0131_site_context_principal"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
