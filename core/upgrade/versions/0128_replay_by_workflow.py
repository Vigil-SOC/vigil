"""Per-workflow tallies on the investigation replay row (#5223).

Chains after ``0127_wazuh_list_groups_class``.
"""

from core.upgrade.revision import run_sql

revision = "0128_replay_by_workflow"
down_revision = "0127_wazuh_list_groups_class"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
