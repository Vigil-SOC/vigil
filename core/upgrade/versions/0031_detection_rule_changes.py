"""Every change of a detection rule's check results, append-only (#3729)."""

from core.upgrade.revision import run_sql

revision = "0031_detection_rule_changes"
down_revision = "0030_drop_settings_keys"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
