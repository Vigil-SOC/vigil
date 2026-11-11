"""When each key ring row was last wrapped, for the wrapping key's age (#4181)."""

from core.upgrade.revision import run_sql

revision = "0059_key_wrapped_at"
down_revision = "0058_sso_links"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
