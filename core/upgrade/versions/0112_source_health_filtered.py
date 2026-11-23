"""Cumulative floor skips on the Integration Health record (#4891).

Adds ``source_health.filtered_total``. Existing rows start at 0. A reset keeps
the total, as it keeps ``dropped_total``.
"""

from core.upgrade.revision import run_sql

revision = "0112_source_health_filtered"
down_revision = "0111_merge_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
