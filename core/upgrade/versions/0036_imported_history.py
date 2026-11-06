"""Closed tickets and alerts from a file become episodic memory (#3965)."""

from core.upgrade.revision import run_sql

revision = "0036_imported_history"
down_revision = "0035_detection_rule_exceptions"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
