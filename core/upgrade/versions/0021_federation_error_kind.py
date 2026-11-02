"""Record on a federation source's row whose fault its last failure was."""

from core.upgrade.revision import run_sql

revision = "0021_federation_error_kind"
down_revision = "0020_workflow_runs_stop_cause"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
