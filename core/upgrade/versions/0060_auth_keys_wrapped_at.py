"""Record when each ring key was last wrapped under the wrapping secret (#4181)."""

from core.upgrade.revision import run_sql

revision = "0060_auth_keys_wrapped_at"
down_revision = "0059_erased_sign_ins"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
