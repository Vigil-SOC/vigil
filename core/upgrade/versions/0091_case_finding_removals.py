"""Remember which findings a person removed from a Case."""

from core.upgrade.revision import run_sql

revision = "0091_case_finding_removals"
down_revision = "0090_skill_installs"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
