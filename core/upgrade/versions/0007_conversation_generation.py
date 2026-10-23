"""A deleted conversation keeps its row, and each life gets its own ledger run."""

from core.upgrade.revision import run_sql

revision = "0007_conversation_generation"
down_revision = "0006_audit_feed"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
