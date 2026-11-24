"""When triage first recorded a verdict on a finding (#4999).

Nullable. A row triaged before this revision stays null, and the Production
Start Report reads that as not recorded. A later triage does not move it.
"""

from core.upgrade.revision import run_sql

revision = "0119_first_verdict_at"
down_revision = "0118_source_health_facts"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
