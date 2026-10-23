"""Record which announcements have not left the box.

``0006_audit_feed`` already follows ``0006_canonical_finding_ips``, so this
revision continues that one chain.
"""

from core.upgrade.revision import run_sql

revision = "0007_retention"
down_revision = "0006_audit_feed"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
