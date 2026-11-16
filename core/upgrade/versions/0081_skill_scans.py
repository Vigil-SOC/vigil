"""Scan verdicts for operator skills, one row per skill digest."""

from core.upgrade.revision import run_sql

revision = "0081_skill_scans"
down_revision = "0080_merge_0079_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
