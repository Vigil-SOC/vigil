"""Alert ids a file-imported ticket points at (#5092).

Chains after ``0120_history_alerts``. The importer fills the column from the
refs it already reads. An import already stored is left empty.
"""

from core.upgrade.revision import run_sql

revision = "0121_history_ticket_alert_ids"
down_revision = "0120_history_alerts"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
