"""Rule links: inventory rules that are copies of one rule in different places."""

from core.upgrade.revision import run_sql

revision = "0025_rule_links"
down_revision = "0024_rule_inventory"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
