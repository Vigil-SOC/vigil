"""The session policy's permission, and the sign-in limit's end reason (#4186)."""

from core.upgrade.revision import run_sql

revision = "0043_session_policy"
down_revision = "0042_server_enforced_sign_ins"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
