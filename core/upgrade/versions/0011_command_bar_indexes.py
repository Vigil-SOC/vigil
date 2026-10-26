"""Trigram indexes for the command bar's case and finding search (#3202)."""

from core.upgrade.revision import run_sql

revision = "0011_command_bar_indexes"
down_revision = "0010_autonomy_ceiling"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
