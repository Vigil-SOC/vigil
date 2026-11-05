"""Day-one cold start: a run, its alert snapshots and its filtered tickets."""

from core.upgrade.revision import run_sql

revision = "0035_cold_start"
down_revision = "0034_siem_searches"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
