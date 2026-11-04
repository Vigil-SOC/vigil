"""Runs results on rule versions, and Kibana's disabled rules (#3704).

Also joins the two 0025 revisions, both written against 0024, into one head.
"""

from core.upgrade.revision import run_sql

revision = "0026_detection_rule_checks"
down_revision = ("0025_detection_rules", "0025_wazuh_tool_classes")


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
