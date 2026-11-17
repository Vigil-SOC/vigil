"""Pushed groups and their members (#4424)."""

from core.upgrade.revision import run_sql

revision = "0075_directory_groups"
down_revision = "0074_directory_sync"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
