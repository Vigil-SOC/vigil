"""Finding ids an agent's exception cited (#4397)."""

from core.upgrade.revision import run_sql

revision = "0071_exception_citations"
down_revision = "0070_investigation_replay"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
