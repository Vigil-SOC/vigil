"""Known activity: what an analyst recorded as expected of one entity."""

from core.upgrade.revision import run_sql

revision = "0020_known_activity"
down_revision = "0019_finding_ip_addresses"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
