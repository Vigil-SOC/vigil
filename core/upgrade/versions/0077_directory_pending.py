"""Pushes that collided with an existing account and wait for a link (#4507)."""

from core.upgrade.revision import run_sql

revision = "0077_directory_pending"
down_revision = "0076_merge_0075_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
