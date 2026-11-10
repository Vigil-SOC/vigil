"""A person's security keys: one row per registered WebAuthn credential (#4234)."""

from core.upgrade.revision import run_sql

revision = "0054_security_keys"
down_revision = "0054_get_case_shell"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
