"""Telemetry events for the Logs page, every source in one schema.

Continues the single chain at ``0016_retention``.
"""

from core.upgrade.revision import run_sql

revision = "0017_log_events"
down_revision = "0016_retention"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
