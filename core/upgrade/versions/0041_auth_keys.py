"""The key ring's table (#4180). The first app start fills it."""

from core.upgrade.revision import run_sql

revision = "0041_auth_keys"
down_revision = "0040_password_scheme_stamp"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
