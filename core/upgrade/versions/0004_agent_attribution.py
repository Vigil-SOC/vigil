"""Principal ids beside the working actor text (#2763)."""

from core.upgrade.revision import run_sql

revision = "0004_agent_attribution"
down_revision = "0003_principals_sign_ins"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
