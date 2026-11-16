"""Directory sync: the join key, how an account was deactivated, and a SCIM version (#4423)."""

from core.upgrade.revision import run_sql

revision = "0074_directory_sync"
down_revision = "0073_medic_fix_notices"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
