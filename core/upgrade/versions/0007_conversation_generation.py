"""Keep a deleted conversation's row and advance its ledger generation (#2856)."""

from core.upgrade.revision import run_sql

revision = "0007_conversation_generation"
down_revision = ("0006_audit_feed", "0006_canonical_finding_ips")


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
