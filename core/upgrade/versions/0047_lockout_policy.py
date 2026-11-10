"""When and where an account was locked, and failure counts reset (#4176)."""

from core.upgrade.revision import run_sql

revision = "0047_lockout_policy"
down_revision = "0046_run_sign_in"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
