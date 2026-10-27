"""Pause writes after a feed-on restore until its entry is off the box (#3191)."""

from core.upgrade.revision import run_sql

revision = "0012_audit_feed_pause"
down_revision = "0011_merge_0010_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
