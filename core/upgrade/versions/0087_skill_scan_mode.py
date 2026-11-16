"""Record the scan mode on each skill verdict."""

from core.upgrade.revision import run_sql

revision = "0087_skill_scan_mode"
down_revision = "0086_join_break_glass"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
