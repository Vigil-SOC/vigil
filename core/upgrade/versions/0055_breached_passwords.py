"""A table for breached passwords imported on top of the shipped list (#4175)."""

from core.upgrade.revision import run_sql

revision = "0055_breached_passwords"
down_revision = "0054_security_keys"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
