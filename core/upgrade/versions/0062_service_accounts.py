"""Service accounts for integrations: no password, a purpose, an accountable person (#4224)."""

from core.upgrade.revision import run_sql

revision = "0062_service_accounts"
down_revision = "0061_users_cutoff_drop"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
