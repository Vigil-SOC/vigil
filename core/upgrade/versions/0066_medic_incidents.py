"""The incident half of the Medic watch (#4366).

Joins the two 0065 heads and adds the open count to the watch row, plus one
sent row per incident event. No init SQL.
"""

from core.upgrade.revision import run_sql

revision = "0066_medic_incidents"
down_revision = ("0065_medic_watch", "0065_end_unscoped_runs")


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
