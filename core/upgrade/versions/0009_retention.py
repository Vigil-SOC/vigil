"""Which retention and erasure announcements have not left the box (#2769).

Continues the single chain at ``0007_conversation_generation``.
"""

from core.upgrade.revision import run_sql

revision = "0009_retention"
down_revision = "0007_conversation_generation"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
