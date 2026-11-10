"""A run records the sign-in it started from (#4173)."""

from core.upgrade.revision import run_sql

revision = "0046_run_sign_in"
down_revision = "0045_detection_proposals"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
