"""The append-only audit trail, its held rows and anchor, and the audit.* grants."""

from core.upgrade.revision import run_sql

revision = "0002_audit_trail"
down_revision = "0001_baseline"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
