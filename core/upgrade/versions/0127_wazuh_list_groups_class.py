"""Effect class for the Wazuh tool server's group read (#5220).

Chains after ``0126_supervisory_worker_state``.
"""

from core.upgrade.revision import run_sql

revision = "0127_wazuh_list_groups_class"
down_revision = "0126_supervisory_worker_state"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
