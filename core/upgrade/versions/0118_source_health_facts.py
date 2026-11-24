"""What a collector reads from a source itself, on its Integration Health record (#5071)."""

from core.upgrade.revision import run_sql

revision = "0118_source_health_facts"
down_revision = "0117_reopen_unrun_containment"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
