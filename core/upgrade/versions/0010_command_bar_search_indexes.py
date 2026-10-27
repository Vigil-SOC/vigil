"""Trigram indexes for the command bar's case and finding search (#3202)."""

from core.upgrade.revision import run_sql

revision = "0010_command_bar_search_indexes"
down_revision = "0009_drop_origin_run_ids"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
