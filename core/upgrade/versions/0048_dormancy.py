"""The dormancy clock's floor and the warning it records (#4176)."""

from core.upgrade.revision import run_sql

revision = "0048_dormancy"
down_revision = "0047_lockout_policy"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
