"""What capability an outside tool answers, on its class row (#5312).

Chains after ``0134_tool_definition_pin``.
"""

from core.upgrade.revision import run_sql

revision = "0135_tool_capability_binding"
down_revision = "0134_tool_definition_pin"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
