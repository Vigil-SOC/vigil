"""Detection proposals and their append-only step record (#4157)."""

from core.upgrade.revision import run_sql

revision = "0045_detection_proposals"
down_revision = "0044_sign_in_end_reasons"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
