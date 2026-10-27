"""A missing ATT&CK confidence is null, not a score (#3221)."""

from core.upgrade.revision import run_sql

revision = "0014_mitre_confidence_nullable"
down_revision = "0013_merge_0012_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
