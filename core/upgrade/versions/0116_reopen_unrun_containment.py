"""Approved isolations, disables and Cloudflare blocks no tool ran go back to pending (#5059)."""

from core.upgrade.revision import run_sql

revision = "0116_reopen_unrun_containment"
down_revision = "0115_containment_undo_tools"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
