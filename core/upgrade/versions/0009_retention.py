"""Which retention and erasure announcements have not left the box (#2769).

Continues the single chain at ``0006_audit_feed``.
"""

from core.upgrade.revision import run_sql

revision = "0009_retention"
down_revision = "0006_audit_feed"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
