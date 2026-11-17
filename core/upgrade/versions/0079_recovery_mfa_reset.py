"""A recovery-code sign-in and an MFA reset end sign-ins as their own reasons (#4449)."""

from core.upgrade.revision import run_sql

revision = "0079_recovery_mfa_reset"
down_revision = "0078_merge_0077_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
