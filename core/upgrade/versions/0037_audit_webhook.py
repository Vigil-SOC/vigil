"""The audit webhook's cursor, beside the syslog feed's (#4019)."""

from core.upgrade.revision import run_sql

revision = "0037_audit_webhook"
down_revision = "0036_imported_history"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
