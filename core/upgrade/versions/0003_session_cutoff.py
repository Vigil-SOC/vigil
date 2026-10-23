"""Per-user session cutoff, enforced when Redis did not store its copy."""

from core.upgrade.revision import run_sql

revision = "0003_session_cutoff"
down_revision = "0002_audit_trail"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
