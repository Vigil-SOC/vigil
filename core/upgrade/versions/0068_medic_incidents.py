"""The incident half of the Medic watch (#4366).

Adds the open count to the watch row, plus one sent row per incident event.
The two 0065 heads are already joined by ``0066_merge_0065_heads``. No init SQL.
"""

from core.upgrade.revision import run_sql

revision = "0068_medic_incidents"
down_revision = "0067_case_readers"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
