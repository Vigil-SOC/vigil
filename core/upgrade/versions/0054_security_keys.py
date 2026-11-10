"""A person's security keys: one row per registered WebAuthn credential (#4234).

Follows 0053_erased_assignees; 0055_current_owners joins the 0054 heads.
"""

from core.upgrade.revision import run_sql

revision = "0054_security_keys"
down_revision = "0053_erased_assignees"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
