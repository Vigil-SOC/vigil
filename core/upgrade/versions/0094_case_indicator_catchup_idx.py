"""Partial indexes for the shared IOC index's catch-up loop and its bell lines (#4734).

No row is read or changed; the indexes only serve the loop's batch order and the
two live counts.
"""

from core.upgrade.revision import run_sql

revision = "0094_case_indicator_catchup_idx"
# Also joins 0093_merge_0092_heads, which sits beside 0093_case_indicator_index.
down_revision = ("0093_case_indicator_index", "0093_merge_0092_heads")


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
