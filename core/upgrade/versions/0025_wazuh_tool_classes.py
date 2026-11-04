"""Effect classes for the Wazuh tool server's read-only tools (#3701)."""

from core.upgrade.revision import run_sql

revision = "0025_wazuh_tool_classes"
down_revision = "0024_triage_intake_indexes"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
