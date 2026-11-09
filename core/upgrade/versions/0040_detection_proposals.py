"""Detection proposals and their append-only step record (#4157)."""

from core.upgrade.revision import run_sql

revision = "0040_detection_proposals"
down_revision = "0039_custom_agent_persona_drop"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
