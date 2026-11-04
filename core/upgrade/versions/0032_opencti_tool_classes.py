"""Effect classes for the OpenCTI tool server's read-only tools (#3756)."""

from core.upgrade.revision import run_sql

revision = "0032_opencti_tool_classes"
down_revision = "0031_detection_rule_changes"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
