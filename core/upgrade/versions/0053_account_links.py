"""Single-use links: invites now, password resets later (#4226)."""

from core.upgrade.revision import run_sql

revision = "0053_account_links"
down_revision = "0052_api_tokens"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
