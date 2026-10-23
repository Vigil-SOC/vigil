"""Drop the covered-runs array: a hunt investigation is exactly one run (#2892)."""

from core.upgrade.revision import run_sql

revision = "0009_drop_origin_run_ids"
down_revision = "0008_permission_catalog"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
