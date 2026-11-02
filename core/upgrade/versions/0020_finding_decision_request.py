"""Hold one analyst decision per request id on the trail, and find it by index."""

from core.upgrade.revision import run_sql

revision = "0020_finding_decision_request"
down_revision = "0019_finding_ip_addresses"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
