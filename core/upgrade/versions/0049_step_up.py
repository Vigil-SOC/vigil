"""When a sign-in last confirmed it's you, and its failed confirmations (#4179)."""

from core.upgrade.revision import run_sql

revision = "0049_step_up"
down_revision = "0048_dormancy"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
