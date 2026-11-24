"""Day 1 Report recurring trigger (#5045).

``recurring`` is the Day 1 Report run again on its cadence. No new column.
"""

from core.upgrade.revision import run_sql

revision = "0114_day1_recurring_trigger"
down_revision = "0113_production_report_triggers"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
