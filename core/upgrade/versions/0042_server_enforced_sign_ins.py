"""Sign-ins the server enforces: the row is the revocation record (#4169)."""

from core.upgrade.revision import run_sql

revision = "0042_server_enforced_sign_ins"
down_revision = "0041_auth_keys"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
