"""Effect class for the Amazon Security Lake server's read-only query tool (#4701)."""

from core.upgrade.revision import run_sql

revision = "0092_security_lake_tool_classes"
down_revision = "0091_case_finding_removals"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
