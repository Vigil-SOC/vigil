"""One autonomy ceiling row, and the old force-manual flag is gone (#2918)."""

from core.upgrade.revision import run_sql

revision = "0010_autonomy_ceiling"
down_revision = "0009_drop_origin_run_ids"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
