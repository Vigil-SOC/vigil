"""Atomic Red Team's own host list moves to the one ``emulation`` row (#5061)."""

from core.upgrade.revision import run_sql

revision = "0116_emulation_hosts"
down_revision = "0115_containment_undo_tools"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
