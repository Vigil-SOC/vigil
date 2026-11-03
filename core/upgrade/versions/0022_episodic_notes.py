"""Analyst notes on an entity, read back by every recall that names it (#3605)."""

from core.upgrade.revision import run_sql

revision = "0022_episodic_notes"
down_revision = "0021_federation_error_kind"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
