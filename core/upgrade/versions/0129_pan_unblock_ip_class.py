"""Effect class for the Palo Alto unblock tool (#5232).

Chains after ``0128_replay_by_workflow``.
"""

from core.upgrade.revision import run_sql

revision = "0129_pan_unblock_ip_class"
down_revision = "0128_replay_by_workflow"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
