"""Append-only rollout stages for the rules in Vigil's overlay (#3723)."""

from core.upgrade.revision import run_sql

revision = "0029_detection_rule_stages"
down_revision = "0028_detection_rule_origin"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
