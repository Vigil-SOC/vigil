"""Detection rules from every configured repo, and their append-only versions (#3703)."""

from core.upgrade.revision import run_sql

revision = "0025_detection_rules"
down_revision = "0024_triage_intake_indexes"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
