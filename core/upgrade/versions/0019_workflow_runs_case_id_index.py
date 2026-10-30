"""Index a run's case id for the case queue's run-only refs (#3495)."""

from core.upgrade.revision import run_sql

revision = "0019_workflow_runs_case_id_index"
down_revision = "0018_legacy_audit_copy"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
