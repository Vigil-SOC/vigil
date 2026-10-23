"""Which retention and erasure announcements have not left the box (#2769).

Follows ``0008_permission_catalog``. One chain: main has a single Alembic head.
"""

from core.upgrade.revision import run_sql

revision = "0009_retention"
down_revision = "0008_permission_catalog"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
