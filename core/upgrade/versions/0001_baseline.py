"""Baseline: the schema and reference rows of a fresh pre-revision install."""

from core.upgrade.revision import run_sql

revision = "0001_baseline"
down_revision = None


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
