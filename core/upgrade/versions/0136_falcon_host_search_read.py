"""Falcon's host search is a read, as the containment lookups are.

Chains after ``0135_tool_capability_binding``.
"""

from core.upgrade.revision import run_sql

revision = "0136_falcon_host_search_read"
down_revision = "0135_tool_capability_binding"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
