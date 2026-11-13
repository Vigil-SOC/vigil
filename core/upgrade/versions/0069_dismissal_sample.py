"""Sampled dismissals: when a close was drawn, and whether evidence was opened.

Two nullable columns on ``case_closure_info``. Nothing is backfilled: a close
from before the draw was not sampled, and nobody recorded the flag.
"""

from core.upgrade.revision import run_sql

revision = "0069_dismissal_sample"
down_revision = "0068_medic_incidents"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
