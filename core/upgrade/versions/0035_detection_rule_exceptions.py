"""Customer-specific exceptions, kept apart from the rule body (#3794)."""

from core.upgrade.revision import run_sql

revision = "0035_detection_rule_exceptions"
down_revision = "0034_siem_searches"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
