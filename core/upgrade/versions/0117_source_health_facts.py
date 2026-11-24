"""What a collector reads from a source itself, on its Integration Health record (#5071)."""

from core.upgrade.revision import run_sql

revision = "0117_source_health_facts"
down_revision = "0116_emulation_hosts"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
