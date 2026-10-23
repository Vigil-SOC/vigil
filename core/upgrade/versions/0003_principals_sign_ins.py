"""Principals, sign-ins, and credentials that outlive the account (#2758)."""

from core.upgrade.revision import run_sql

revision = "0003_principals_sign_ins"
down_revision = "0002_audit_trail"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
