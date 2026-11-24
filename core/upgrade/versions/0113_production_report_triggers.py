"""Production Start Report triggers on the Day 1 tables (#4997).

``production`` is the one automatic run. ``production_on_demand`` is a
person's run and does not count as that one. No new column.
"""

from core.upgrade.revision import run_sql

revision = "0113_production_report_triggers"
down_revision = "0112_source_health_filtered"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
