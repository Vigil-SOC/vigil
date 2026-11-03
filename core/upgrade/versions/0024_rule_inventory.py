"""The rule inventory: rules under a stable id, and their version history."""

from core.upgrade.revision import run_sql

revision = "0024_rule_inventory"
down_revision = "0023_runtime_role_no_create"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
