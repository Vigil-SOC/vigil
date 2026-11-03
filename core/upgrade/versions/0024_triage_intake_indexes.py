"""Indexes for Triage and Overview intake_triggers and findings lookups (#3664)."""

from core.upgrade.revision import run_sql

revision = "0024_triage_intake_finding_indexes"
down_revision = "0023_runtime_role_no_create"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
