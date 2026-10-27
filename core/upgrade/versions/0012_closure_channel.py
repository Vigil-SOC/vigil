"""Closure channel and principal ids on case closes (#3192)."""

from core.upgrade.revision import run_sql

revision = "0012_closure_channel"
down_revision = "0011_merge_0010_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
