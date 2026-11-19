"""Sender labels on a push source's Integration Health record (#4870).

Adds ``source_health.labels``. Also joins ``0104_source_health`` and
``0104_erased_notices``, which landed side by side: the API refuses to start
on more than one head. Neither 0104 revision is changed.
"""

from core.upgrade.revision import run_sql

revision = "0105_source_health_labels"
down_revision = ("0104_erased_notices", "0104_source_health")


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
