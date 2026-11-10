"""Single-use links: invites now, password resets later (#4226)."""

from core.upgrade.revision import run_sql

revision = "0051_account_links"
down_revision = "0050_step_up"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
