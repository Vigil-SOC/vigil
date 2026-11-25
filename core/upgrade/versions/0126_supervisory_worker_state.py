"""When the supervisory model last checked, and when it checks next.

Chains after ``0125_supervisory_suggestions``. One row, rewritten each check.
"""

from core.upgrade.revision import run_sql

revision = "0126_supervisory_worker_state"
down_revision = "0125_supervisory_suggestions"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
