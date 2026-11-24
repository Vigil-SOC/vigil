"""One row per file-imported alert, and the column names that were not stored (#5091).

``history_alerts`` sits beside ``history_tickets``. A re-upload replaces its
rows with the file. An import already stored gets no rows.
"""

from core.upgrade.revision import run_sql

revision = "0120_history_alerts"
down_revision = "0119_first_verdict_at"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
