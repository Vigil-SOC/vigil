"""When and where an account was locked, and failure counts reset (#4176)."""

from core.upgrade.revision import run_sql

revision = "0046_lockout_policy"
down_revision = "0045_detection_proposals"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
