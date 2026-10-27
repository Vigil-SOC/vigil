"""Which retention and erasure announcements have not left the box (#2769).

Continues the single chain at ``0015_clear_invented_mitre_conf``.
"""

from core.upgrade.revision import run_sql

revision = "0009_retention"
down_revision = "0015_clear_invented_mitre_conf"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
