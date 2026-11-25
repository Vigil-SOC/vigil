"""Effect classes for the Defender, Carbon Black and Entra lookup and undo tools (#5058)."""

from core.upgrade.revision import run_sql

revision = "0115_containment_undo_tools"
down_revision = "0114_day1_recurring_trigger"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
