"""Pin a remote server's classified tool to the definition a person saw (#5300).

Chains after ``0133_episodic_coverage_entries``.
"""

from core.upgrade.revision import run_sql

revision = "0134_tool_definition_pin"
down_revision = "0133_episodic_coverage_entries"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
