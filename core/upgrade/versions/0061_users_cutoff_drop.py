"""Drop users.tokens_valid_after: the sign_ins row is the revocation record."""

from core.upgrade.revision import run_sql

revision = "0061_users_cutoff_drop"
down_revision = "0060_auth_keys_wrapped_at"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
