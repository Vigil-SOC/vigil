"""A reader list on every case: restricted cases (#4354).

Two nullable arrays on ``cases``. Empty or NULL is open, so every existing
case stays open and nothing is backfilled.
"""

from core.upgrade.revision import run_sql

revision = "0066_case_readers"
down_revision = "0065_end_unscoped_runs"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
