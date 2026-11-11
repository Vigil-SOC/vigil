"""Single-use links: invites to set up an account (#4226)."""

from core.upgrade.revision import run_sql

revision = "0063_auth_links"
down_revision = "0062_service_accounts"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
