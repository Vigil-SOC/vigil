"""Keep an erased person's username and email as a keyed hash."""

from core.upgrade.revision import run_sql

revision = "0088_identifier_reservations"
down_revision = "0087_skill_scan_mode"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
