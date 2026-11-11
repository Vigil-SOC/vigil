"""Single sign-on links: an account's issuer and subject at the provider (#4284)."""

from core.upgrade.revision import run_sql

revision = "0058_sso_links"
down_revision = "0057_current_owners"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
