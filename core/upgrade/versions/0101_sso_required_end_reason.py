"""Require single sign-on ends an open non-SSO sign-in as its own reason (#4841)."""

from core.upgrade.revision import run_sql

revision = "0101_sso_required_end_reason"
down_revision = "0100_shared_ioc_case_only"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
