"""Capped re-check batches (#5093).

Chains after ``0121_history_ticket_alert_ids``. One batch and its tickets.
The driver fills the run id after insert; the draw does not change.
"""

from core.upgrade.revision import run_sql

revision = "0122_capped_recheck"
down_revision = "0121_history_ticket_alert_ids"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
