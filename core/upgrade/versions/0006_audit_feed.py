"""The syslog feed's cursor, outside the audit chain (#2767)."""

from core.upgrade.revision import run_sql

revision = "0006_audit_feed"
down_revision = "0006_canonical_finding_ips"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
