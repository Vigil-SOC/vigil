"""Integration Health: one stored record per source (#4869, epic #4866).

Adds ``source_health`` and copies the health values from
``federation_sources`` and ``integration_configs.last_test_*`` into it. The
old columns are left in place, no longer written; a later release drops them.
"""

from core.upgrade.revision import run_sql

revision = "0104_source_health"
down_revision = "0103_merge_0102_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
