"""Record on a run's row why the model path stopped it."""

from core.upgrade.revision import run_sql

revision = "0020_workflow_runs_stop_cause"
down_revision = "0019_finding_ip_addresses"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
