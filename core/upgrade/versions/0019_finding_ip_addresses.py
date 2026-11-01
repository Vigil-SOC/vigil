"""Store each finding's addresses in an indexed column for the IP-exclusion
filter (#3494)."""

from core.upgrade.revision import run_sql

revision = "0019_finding_ip_addresses"
down_revision = "0019_workflow_runs_case_id_index"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
