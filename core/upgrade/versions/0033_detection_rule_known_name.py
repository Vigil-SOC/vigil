"""A Splunk ESCU saved search's link to its repo rule by name (#3717)."""

from core.upgrade.revision import run_sql

revision = "0033_detection_rule_known_name"
down_revision = "0032_opencti_tool_classes"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
