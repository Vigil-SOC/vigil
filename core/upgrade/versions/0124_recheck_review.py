"""Blind re-check reviews, and the Case mark they open from (#5095).

Chains after ``0123_capped_recheck``. One column on cases, and an insert-only
review table. The queue itself is a read of the capped batch.
"""

from core.upgrade.revision import run_sql

revision = "0124_recheck_review"
down_revision = "0123_capped_recheck"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
