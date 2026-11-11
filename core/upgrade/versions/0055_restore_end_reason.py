"""A restore ends every open sign-in with the reason 'restore' (#4241)."""

from core.upgrade.revision import run_sql

revision = "0055_restore_end_reason"
down_revision = "0054_security_keys"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
