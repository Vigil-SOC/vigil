"""End reasons for sign-ins a person or an admin ends (#4172)."""

from core.upgrade.revision import run_sql

revision = "0043_sign_in_end_reasons"
down_revision = "0042_server_enforced_sign_ins"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
