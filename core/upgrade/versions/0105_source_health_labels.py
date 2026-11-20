"""Sender labels on a push source's Integration Health record (#4870).

Adds ``source_health.labels``. Follows ``0105_merge_0104_heads``, which joins
the two 0104 revisions. The API refuses to start on more than one head.
"""

from core.upgrade.revision import run_sql

revision = "0105_source_health_labels"
down_revision = "0105_merge_0104_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
