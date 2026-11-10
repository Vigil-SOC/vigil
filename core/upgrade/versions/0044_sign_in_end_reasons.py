"""End reasons for sign-ins a person or an admin ends (#4172)."""

from core.upgrade.revision import run_sql

revision = "0044_sign_in_end_reasons"
down_revision = "0043_session_policy"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
