"""Runs results on rule versions, and Kibana's disabled rules (#3704)."""

from core.upgrade.revision import run_sql

revision = "0027_detection_rule_checks"
down_revision = "0026_detection_rules"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
